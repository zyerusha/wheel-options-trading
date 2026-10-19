"""CSP TO ENTER: the put strike to sell to start (or restart) a wheel on a
ticker. The one implementation every table, card and tooltip reads (via the
dashboard payload's per-ticker ``csp_to_enter`` map in ``wheel/api.py``);
nothing else computes it, and the frontend only displays it.

Pure: no network, no clock. The caller supplies the daily price history,
the put side of one expiration's chain (this module's own
``pick_expiration``: closest to 35 DTE inside 30-45, chosen independently of
CC TO EXIT's pick) and today's date.

Pipeline, in order:

1. Support zones (``analyze_support``), from the last ~12 months of daily
   bars. A swing low is a daily low that is the lowest within 5 sessions
   either side and is followed by a material bounce. Swing lows within a
   volatility-scaled band (ATR-based) are clustered into one zone, and each
   zone is scored 0-100 on touches (30%), recency (20%), bounce size (20%),
   moving-average confluence (15%), volume at the touches (10%) and a nearby
   round number (5%); a missing factor (no volume data, too little history
   for any moving average) drops out and the rest are re-normalized. The
   score is halved for every time price has since closed clearly through
   the zone, and scaled down when there is less than ~6 months of history.
   It is a relative strength ranking, not a probability that support holds.
2. The relevant zone (``select_support_zone``): its bottom below the current
   price, at most 25% under it, scoring at least 40, and best on strength
   x a distance decay, so strong but remote support can't win on score alone.
3. Candidate puts: every listed strike below the current price, priced at
   the bid/ask mid less the per-contract fee, with a Black-Scholes delta
   (``wheel.optionsmath``; Yahoo carries no Greeks). Hard filters: a
   two-sided quote with a bid and a spread no wider than half the mid; a
   delta between 0.10 and 0.40; annualized return on strike collateral of
   at least 30% (the same net premium / (strike x 100) x 365 / days
   definition as the open-position Ann. Yield column); a positive effective
   purchase price; and a strike close enough to the zone.
4. Survivors are ranked 0-100: support strength 35%, delta closeness to
   0.25 20%, annualized return 20%, effective-price discount 15%, strike
   position vs. the zone 10%. None survive -> NO QUALIFYING CSP, with the
   reason. Never a fallback to a percentage of the price or an invented strike.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Sequence

from wheel import optionsmath
from wheel import tooltipcard as tc
from wheel.ccexit import OPTION_FEE_PER_CONTRACT, OPTION_MULTIPLIER
from wheel.marketdata import PricePoint
from wheel.optionsmath import PUT, bid_ask_mid, black_scholes_greeks
from wheel.statmath import DAYS_PER_YEAR

# ---- wheel return rule -----------------------------------------------------

# The wheel's minimum annualized return on collateral for a new short put.
# Also the bar a roll has to clear (wheel/api.py _ROLL_TARGET_ANNUALIZED_YIELD
# reads this constant), so the two can't drift apart.
MIN_ANNUALIZED_RETURN_PCT = 30.0
# Annualized return that earns the full "return" share of the candidate score.
FULL_CREDIT_ANNUALIZED_RETURN_PCT = 2 * MIN_ANNUALIZED_RETURN_PCT

# ---- support detection -----------------------------------------------------

SUPPORT_LOOKBACK_BARS = 252  # ~12 months of sessions
SUPPORT_FULL_CONFIDENCE_BARS = 126  # ~6 months; less scales the score down
# With less history the score is scaled by 0.5 + 0.5 x (sessions / 126): thin
# history weakens the evidence but never zeroes it on its own.
HISTORY_CONFIDENCE_FLOOR = 0.5
SUPPORT_MIN_BARS = 40  # below this there is too little to find swing lows at all
ATR_PERIOD = 14
SWING_PIVOT_BARS = 5  # lowest low within this many sessions either side
BOUNCE_WINDOW_BARS = 20  # bounce measured over the following ~month
# A swing low needs a bounce of at least max(3%, 1.5 ATR) to count -- a fixed
# 3% alone would accept noise on a volatile name and reject real turns on a
# calm one.
SWING_MIN_BOUNCE_PCT = 3.0
SWING_MIN_BOUNCE_ATR = 1.5
# Zone width (how far above a zone's lowest low another low may sit and still
# join it): 1.5 ATR, kept between 2.5% and 6% of price (2.5% so a $140-$143
# shelf on a $150 stock reads as one zone even on a calm name).
ZONE_WIDTH_ATR_MULTIPLE = 1.5
ZONE_WIDTH_PCT_BOUNDS = (2.5, 6.0)
TOUCHES_FOR_FULL_CREDIT = 4
RECENCY_HALF_LIFE_BARS = 63  # a touch ~3 months old counts half as much as today's
# A bounce of max(4 ATR, 2x the minimum swing bounce) earns full bounce credit;
# each touch is capped there before averaging so one outsized rally can't
# carry the zone. The second term keeps a very calm stock's bar meaningful.
BOUNCE_FULL_CREDIT_ATR = 4.0
BOUNCE_FULL_CREDIT_MIN_BOUNCES = 2.0
MA_NEAR_ZONE_FOR_FULL_CREDIT = 2
# Touch-day volume / median daily volume: 0.5x or less earns nothing, 2x or more full.
VOLUME_RATIO_RANGE = (0.5, 2.0)
# Each close-through episode after the first touch halves the zone's score.
BREAK_PENALTY_FACTOR = 0.5

SUPPORT_WEIGHTS = {
    "touches": 30.0,
    "recency": 20.0,
    "bounce": 20.0,
    "moving_averages": 15.0,
    "volume": 10.0,
    "round_number": 5.0,
}

MOVING_AVERAGES = (
    ("21-day EMA", 21, "ema"),
    ("50-day MA", 50, "sma"),
    ("100-day MA", 100, "sma"),
    ("200-day MA", 200, "sma"),
)

# ---- relevant zone ---------------------------------------------------------

MIN_SUPPORT_SCORE = 40.0
MAX_SUPPORT_DISTANCE_PCT = 25.0
# Relevance = strength x exp(-(distance / 15%)^2): ~0.80 at 7% below, ~0.37 at 15%.
SUPPORT_DISTANCE_SCALE_PCT = 15.0

# ---- candidates ------------------------------------------------------------

# Expiration: the listed date closest to the target inside the window, or,
# when nothing is listed inside it, the date closest to the target overall.
# Its own constants so CSP TO ENTER's choice never depends on CC TO EXIT's;
# today they hold the same values, so the two share one chain fetch.
CSP_TARGET_DTE = 35
CSP_DTE_WINDOW = (30, 45)

# The delta the ranking aims for, the band the explanation calls "preferred"
# (scoring only, nothing is rejected for leaving it), and the hard limits a
# put must sit inside to qualify at all.
TARGET_DELTA = 0.25
PREFERRED_DELTA_RANGE = (0.20, 0.30)
ACCEPTABLE_DELTA_RANGE = (0.10, 0.40)
DELTA_SCORE_SPAN = 0.15  # |delta - 0.25| at or past this earns no delta credit
# A spread wider than half the mid means the mid is not a price anyone fills at.
MAX_SPREAD_PCT_OF_MID = 50.0
# How far (as % of the current price) a strike may sit outside the zone:
# twice the zone width. Past that it isn't anchored to this support any more.
STRIKE_REACH_ZONE_WIDTHS = 2.0
# An effective purchase price this far (%) under the current price earns full credit.
FULL_CREDIT_EFFECTIVE_DISCOUNT_PCT = 15.0

CANDIDATE_WEIGHTS = {
    "support": 35.0,
    "delta": 20.0,
    "return": 20.0,
    "effective_price": 15.0,
    "zone_position": 10.0,
}

STATUS_RECOMMENDED = "recommended"
STATUS_NO_QUALIFYING = "no_qualifying"
STATUS_LABELS = {STATUS_NO_QUALIFYING: "NO QUALIFYING CSP"}

INSIDE, ABOVE, BELOW = "inside", "above", "below"

_EPS = 1e-9


# ---- data ------------------------------------------------------------------


@dataclass(frozen=True)
class PutQuote:
    strike: float
    bid: float | None
    ask: float | None
    implied_volatility: float | None
    # Contract liquidity when the chain reports it; shown, never filtered on
    # (see docs/DESIGN.md, CSP TO ENTER).
    open_interest: int | None = None
    volume: int | None = None


@dataclass(frozen=True)
class EntryChain:
    """The market inputs for one ticker: the chosen expiration's put quotes,
    or ``puts=None`` with the reason they couldn't be loaded."""

    today: date
    expiry: date | None
    puts: tuple[PutQuote, ...] | None
    unavailable_reason: str | None = None


@dataclass(frozen=True)
class SupportZone:
    low: float
    high: float
    touch_lows: tuple[float, ...]
    touch_days: tuple[date, ...]
    sessions_since_last_touch: int
    avg_bounce_pct: float
    breaks: int
    moving_averages_near: tuple[str, ...]
    round_level: float | None
    volume_ratio: float | None
    # Each factor's 0-1 credit; a factor with no data is absent, not zero.
    components: dict[str, float]
    score: float  # 0-100, after the break penalty and history scaling

    @property
    def touches(self) -> int:
        return len(self.touch_lows)


@dataclass(frozen=True)
class SupportAnalysis:
    sessions_analyzed: int
    atr_pct: float | None
    zone_width_pct: float | None
    moving_averages: dict[str, float]
    zones: tuple[SupportZone, ...]
    history_confidence: float  # 1.0 with >= ~6 months of bars
    limitations: tuple[str, ...]


@dataclass(frozen=True)
class PutCandidate:
    strike: float
    bid: float | None
    ask: float | None
    delta: float | None  # absolute (a put's delta is negative)
    mid_premium: float | None  # per share
    net_premium: float | None  # per share, after the per-contract fee
    annualized_return_pct: float | None
    downside_to_strike_pct: float
    effective_purchase_price: float | None
    # (current price - effective purchase price) / current price, %.
    effective_discount_pct: float | None
    zone_position: str | None  # INSIDE / ABOVE / BELOW
    zone_gap_pct: float | None  # how far outside the zone, % of current price (0 inside)
    components: dict[str, float]
    score: float | None  # 0-100, qualifying candidates only
    rejected_reason: str | None
    open_interest: int | None = None
    volume: int | None = None

    @property
    def qualifies(self) -> bool:
        return self.rejected_reason is None

    @property
    def spread_pct_of_mid(self) -> float | None:
        return spread_pct_of_mid(self.bid, self.ask, self.mid_premium)

    @property
    def spread_check(self) -> str:
        """The spread rule on its own: PASS, FAIL (no bid, or wider than
        ``MAX_SPREAD_PCT_OF_MID``) or UNAVAILABLE (no usable quote at all)."""
        if self.mid_premium is None:
            return "FAIL" if self.ask and not self.bid else "UNAVAILABLE"
        if not self.bid:
            return "FAIL"
        return "PASS" if self.spread_pct_of_mid <= MAX_SPREAD_PCT_OF_MID else "FAIL"


def spread_pct_of_mid(bid: float | None, ask: float | None, mid: float | None) -> float | None:
    """(ask - bid) / mid, %: the one spread definition the filter and the
    tooltip both use. ``None`` without a usable mid."""
    if mid is None or mid <= 0 or bid is None or ask is None:
        return None
    return 100.0 * (ask - bid) / mid


@dataclass(frozen=True)
class CspEntry:
    status: str
    reason: str
    current_price: float | None
    support: SupportAnalysis | None = None
    zone: SupportZone | None = None
    zone_distance_pct: float | None = None
    expiry: date | None = None
    days_to_expiry: int | None = None
    pick: PutCandidate | None = None
    candidates: tuple[PutCandidate, ...] = field(default_factory=tuple)
    # The one chain snapshot and calculation behind this result: Yahoo's
    # market time on the chain, when this app fetched it, and when it ran.
    quotes_as_of: datetime | None = None
    fetched_at: datetime | None = None
    calculated_at: datetime | None = None
    # True when a market input the calculation needed was unavailable (no
    # price, too little daily history, no listed expiration, the chain
    # didn't load), as opposed to every input loading and no put passing.
    # A cross-account merge prefers an entry that had all its data.
    market_data_missing: bool = False
    # The calendar date DTE is counted from (the calculation date, which can
    # be after the quote time over a weekend or holiday).
    today: date | None = None

    @property
    def strike(self) -> float | None:
        return self.pick.strike if self.pick else None

    @property
    def expiry_in_window(self) -> bool | None:
        """Whether the expiration sits inside the 30-45 DTE window. ``False``:
        none was listed there, so the date closest to 35 overall was used."""
        if self.days_to_expiry is None:
            return None
        low, high = CSP_DTE_WINDOW
        return low <= self.days_to_expiry <= high


def pick_expiration(expirations: Sequence[date], today: date) -> date | None:
    """CSP TO ENTER's expiration: closest to ``CSP_TARGET_DTE`` inside
    ``CSP_DTE_WINDOW``, else closest to the target overall."""
    return optionsmath.pick_expiration(expirations, today, target_dte=CSP_TARGET_DTE, dte_window=CSP_DTE_WINDOW)


# ---- indicators ------------------------------------------------------------


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def average_true_range_pct(bars: Sequence[PricePoint], period: int = ATR_PERIOD) -> float | None:
    """Simple average of the last ``period`` true ranges, as % of the last
    close. True range = max(high, prior close) - min(low, prior close)."""
    if len(bars) < period + 1 or bars[-1].close <= 0:
        return None
    ranges = [
        max(bar.high, prev.close) - min(bar.low, prev.close)
        for prev, bar in zip(bars[-period - 1 : -1], bars[-period:])
    ]
    return 100.0 * (sum(ranges) / period) / bars[-1].close


def simple_moving_average(closes: Sequence[float], period: int) -> float | None:
    return sum(closes[-period:]) / period if len(closes) >= period else None


def exponential_moving_average(closes: Sequence[float], period: int) -> float | None:
    """Seeded with the simple average of the first ``period`` closes, then
    the standard smoothing 2 / (period + 1) over every close after them."""
    if len(closes) < period:
        return None
    smoothing = 2.0 / (period + 1)
    ema = sum(closes[:period]) / period
    for close in closes[period:]:
        ema += smoothing * (close - ema)
    return ema


def round_number_step(price: float) -> float:
    """Spacing of "round" prices near ``price``: a quarter of its order of
    magnitude -- $25 steps around $150 ($125, $150, $175), $2.50 around $45."""
    return 10 ** math.floor(math.log10(price)) * 0.25


def _round_level_in(band_low: float, band_high: float, near: float) -> float | None:
    step = round_number_step(near)
    first = math.ceil(round(band_low / step, 6))
    last = math.floor(round(band_high / step, 6))
    if first > last:
        return None
    return min((n * step for n in range(first, last + 1)), key=lambda level: abs(level - near))


# ---- support ---------------------------------------------------------------


@dataclass(frozen=True)
class _SwingLow:
    index: int
    low: float
    bounce_pct: float
    volume: float | None


def _swing_lows(bars: Sequence[PricePoint], start: int, min_bounce_pct: float) -> list[_SwingLow]:
    """Lows inside ``bars[start:]`` that are strictly below the prior
    ``SWING_PIVOT_BARS`` lows, no higher than the next ``SWING_PIVOT_BARS``
    (so a flat double-bottom counts once, at its first day), and followed by
    a bounce of at least ``min_bounce_pct`` within ``BOUNCE_WINDOW_BARS``.
    The last few sessions can't qualify yet: their right side hasn't formed."""
    pivots: list[_SwingLow] = []
    for i in range(start, len(bars) - SWING_PIVOT_BARS):
        low = bars[i].low
        left = bars[max(0, i - SWING_PIVOT_BARS) : i]
        right = bars[i + 1 : i + 1 + SWING_PIVOT_BARS]
        if low <= 0 or (left and low >= min(bar.low for bar in left)) or low > min(bar.low for bar in right):
            continue
        peak = max(bar.high for bar in bars[i + 1 : i + 1 + BOUNCE_WINDOW_BARS])
        bounce_pct = 100.0 * (peak - low) / low
        if bounce_pct >= min_bounce_pct:
            pivots.append(_SwingLow(i, low, bounce_pct, bars[i].volume))
    return pivots


def _cluster(pivots: Sequence[_SwingLow], width_pct: float) -> list[list[_SwingLow]]:
    """Greedy, lowest first: a low joins the current zone while it sits
    within ``width_pct`` of that zone's lowest low; otherwise it starts the
    next zone. Anchoring to the zone's floor (not the previous low) keeps a
    staircase of lows from chaining into one wide zone."""
    clusters: list[list[_SwingLow]] = []
    for pivot in sorted(pivots, key=lambda p: (p.low, p.index)):
        if clusters and pivot.low <= clusters[-1][0].low * (1 + width_pct / 100):
            clusters[-1].append(pivot)
        else:
            clusters.append([pivot])
    return clusters


def _break_episodes(bars: Sequence[PricePoint], since: int, zone_low: float, width_pct: float) -> int:
    """Times price closed clearly through the zone (more than one zone width
    under its bottom) after its first touch. One episode per stretch below,
    however many sessions it lasts."""
    line = zone_low * (1 - width_pct / 100)
    episodes, below = 0, False
    for bar in bars[since:]:
        if bar.close < line and not below:
            episodes += 1
        below = bar.close < zone_low if below else bar.close < line
    return episodes


def _volume_baseline(bars: Sequence[PricePoint]) -> float | None:
    """Median daily volume, or ``None`` when fewer than half the sessions
    carry a positive volume -- too sparse to compare touches against."""
    volumes = [bar.volume for bar in bars if bar.volume is not None and bar.volume > 0]
    if len(volumes) < len(bars) / 2:
        return None
    return statistics.median(volumes)


def _score_zone(
    members: Sequence[_SwingLow],
    *,
    bars: Sequence[PricePoint],
    width_pct: float,
    atr_pct: float,
    min_bounce_pct: float,
    moving_averages: dict[str, float],
    volume_baseline: float | None,
    history_confidence: float,
) -> SupportZone:
    lows = [m.low for m in members]
    zone_low, zone_high = min(lows), max(lows)
    by_time = sorted(members, key=lambda m: m.index)
    last_index = len(bars) - 1
    sessions_since = last_index - by_time[-1].index

    components: dict[str, float] = {}
    components["touches"] = min(1.0, len(members) / TOUCHES_FOR_FULL_CREDIT)
    components["recency"] = 0.5 ** (sessions_since / RECENCY_HALF_LIFE_BARS)

    full_bounce = max(BOUNCE_FULL_CREDIT_ATR * atr_pct, BOUNCE_FULL_CREDIT_MIN_BOUNCES * min_bounce_pct)
    avg_bounce = sum(m.bounce_pct for m in members) / len(members)
    components["bounce"] = sum(min(m.bounce_pct, full_bounce) for m in members) / len(members) / full_bounce

    # A moving average is confluence only when it sits within one zone width
    # of the zone itself; one far above or below adds nothing.
    band_low, band_high = zone_low * (1 - width_pct / 100), zone_high * (1 + width_pct / 100)
    near = tuple(name for name, value in moving_averages.items() if band_low <= value <= band_high)
    if moving_averages:
        components["moving_averages"] = min(1.0, len(near) / MA_NEAR_ZONE_FOR_FULL_CREDIT)

    volume_ratio = None
    touch_volumes = [m.volume for m in members if m.volume is not None and m.volume > 0]
    if volume_baseline and touch_volumes:
        volume_ratio = (sum(touch_volumes) / len(touch_volumes)) / volume_baseline
        low_ratio, high_ratio = VOLUME_RATIO_RANGE
        components["volume"] = _clamp((volume_ratio - low_ratio) / (high_ratio - low_ratio), 0.0, 1.0)

    round_level = _round_level_in(
        zone_low * (1 - width_pct / 200), zone_high * (1 + width_pct / 200), (zone_low + zone_high) / 2
    )
    components["round_number"] = 1.0 if round_level is not None else 0.0

    weight_total = sum(SUPPORT_WEIGHTS[name] for name in components)
    weighted = sum(SUPPORT_WEIGHTS[name] * credit for name, credit in components.items()) / weight_total
    breaks = _break_episodes(bars, by_time[0].index, zone_low, width_pct)
    score = 100.0 * weighted * (BREAK_PENALTY_FACTOR**breaks) * history_confidence

    return SupportZone(
        low=zone_low,
        high=zone_high,
        touch_lows=tuple(m.low for m in by_time),
        touch_days=tuple(bars[m.index].day for m in by_time),
        sessions_since_last_touch=sessions_since,
        avg_bounce_pct=avg_bounce,
        breaks=breaks,
        moving_averages_near=near,
        round_level=round_level,
        volume_ratio=volume_ratio,
        components=components,
        score=score,
    )


def analyze_support(bars: Sequence[PricePoint]) -> SupportAnalysis:
    """Every support zone in the last ``SUPPORT_LOOKBACK_BARS`` sessions of
    ``bars`` (ascending daily bars; ones without a high/low are skipped),
    strongest first. Short history degrades rather than fails: the score is
    scaled by sessions / ``SUPPORT_FULL_CONFIDENCE_BARS`` and the shortfall
    is listed in ``limitations``."""
    ranged = [bar for bar in bars if bar.has_range and bar.low > 0]
    closes = [bar.close for bar in bars]
    limitations: list[str] = []
    if len(ranged) < len(bars):
        limitations.append(f"{len(bars) - len(ranged)} session(s) had no daily high/low and were skipped.")
    if len(ranged) < SUPPORT_MIN_BARS:
        limitations.append(
            f"Only {len(ranged)} sessions of daily high/low history; at least {SUPPORT_MIN_BARS} are needed to find support."
        )
        return SupportAnalysis(len(ranged), None, None, {}, (), 0.0, tuple(limitations))

    start = max(0, len(ranged) - SUPPORT_LOOKBACK_BARS)
    sessions = len(ranged) - start
    coverage = min(1.0, sessions / SUPPORT_FULL_CONFIDENCE_BARS)
    history_confidence = HISTORY_CONFIDENCE_FLOOR + (1 - HISTORY_CONFIDENCE_FLOOR) * coverage
    if history_confidence < 1.0:
        limitations.append(
            f"Limited history: {sessions} sessions (about {sessions / 21:.0f} months) of the "
            f"{SUPPORT_FULL_CONFIDENCE_BARS}+ wanted, so strength is scaled to {history_confidence:.0%}."
        )

    atr_pct = average_true_range_pct(ranged)
    width_pct = _clamp(ZONE_WIDTH_ATR_MULTIPLE * atr_pct, *ZONE_WIDTH_PCT_BOUNDS)
    min_bounce_pct = max(SWING_MIN_BOUNCE_PCT, SWING_MIN_BOUNCE_ATR * atr_pct)

    moving_averages: dict[str, float] = {}
    for name, period, kind in MOVING_AVERAGES:
        value = exponential_moving_average(closes, period) if kind == "ema" else simple_moving_average(closes, period)
        if value is not None:
            moving_averages[name] = value
    missing = [name for name, _, _ in MOVING_AVERAGES if name not in moving_averages]
    if missing:
        limitations.append(f"Not enough history for the {', '.join(missing)}.")

    window = ranged[start:]
    volume_baseline = _volume_baseline(window)
    if volume_baseline is None:
        limitations.append("No reliable volume data; strength uses the other factors.")

    pivots = _swing_lows(ranged, start, min_bounce_pct)
    zones = [
        _score_zone(
            members,
            bars=ranged,
            width_pct=width_pct,
            atr_pct=atr_pct,
            min_bounce_pct=min_bounce_pct,
            moving_averages=moving_averages,
            volume_baseline=volume_baseline,
            history_confidence=history_confidence,
        )
        for members in _cluster(pivots, width_pct)
    ]
    zones.sort(key=lambda zone: (-zone.score, -zone.high))
    return SupportAnalysis(
        sessions, atr_pct, width_pct, moving_averages, tuple(zones), history_confidence, tuple(limitations)
    )


def support_distance_pct(zone: SupportZone, current_price: float) -> float:
    """How far the top of the zone sits under the price, % of the price
    (0 when the price is already inside the zone)."""
    return 100.0 * (current_price - min(zone.high, current_price)) / current_price


def select_support_zone(analysis: SupportAnalysis, current_price: float) -> tuple[SupportZone | None, str | None]:
    """``(zone, None)`` for the strongest *relevant* zone, or ``(None, why)``."""
    if not analysis.zones:
        if analysis.sessions_analyzed < SUPPORT_MIN_BARS:
            return None, "Not enough daily price history to find support."
        return None, f"No swing lows with a real bounce in the last {analysis.sessions_analyzed} sessions."
    below = [zone for zone in analysis.zones if zone.low < current_price]
    if not below:
        return None, "Price is under every support zone found; that support has already broken."
    in_reach = [zone for zone in below if support_distance_pct(zone, current_price) <= MAX_SUPPORT_DISTANCE_PCT]
    strong = [zone for zone in in_reach if zone.score >= MIN_SUPPORT_SCORE]
    if not strong:
        return None, (
            f"No support zone within {MAX_SUPPORT_DISTANCE_PCT:g}% below the price scores at least "
            f"{MIN_SUPPORT_SCORE:g}/100."
        )

    def relevance(zone: SupportZone) -> float:
        distance = support_distance_pct(zone, current_price)
        return zone.score * math.exp(-((distance / SUPPORT_DISTANCE_SCALE_PCT) ** 2))

    return max(strong, key=lambda zone: (round(relevance(zone), 6), zone.high)), None


# ---- candidates ------------------------------------------------------------


def _zone_position(strike: float, zone: SupportZone, current_price: float) -> tuple[str, float]:
    if strike > zone.high + _EPS:
        return ABOVE, 100.0 * (strike - zone.high) / current_price
    if strike < zone.low - _EPS:
        return BELOW, 100.0 * (zone.low - strike) / current_price
    return INSIDE, 0.0


def _evaluate(
    quote: PutQuote,
    *,
    current_price: float,
    days_to_expiry: int,
    zone: SupportZone,
    reach_pct: float,
    risk_free_rate: float,
) -> PutCandidate:
    downside = 100.0 * (current_price - quote.strike) / current_price
    position, gap = _zone_position(quote.strike, zone, current_price)
    mid = bid_ask_mid(quote.bid, quote.ask)
    greeks = black_scholes_greeks(
        spot=current_price,
        strike=quote.strike,
        days_to_expiry=days_to_expiry,
        implied_volatility=quote.implied_volatility,
        right=PUT,
        risk_free_rate=risk_free_rate,
    )
    delta = abs(greeks.delta) if greeks else None
    net = mid - OPTION_FEE_PER_CONTRACT / OPTION_MULTIPLIER if mid is not None else None
    annualized = (
        100.0 * (net / quote.strike) * (DAYS_PER_YEAR / days_to_expiry) if net is not None and quote.strike > 0 else None
    )
    effective = quote.strike - net if net is not None else None
    effective_discount = 100.0 * (current_price - effective) / current_price if effective is not None else None

    reason = None
    low_delta, high_delta = ACCEPTABLE_DELTA_RANGE
    # Distance from the zone first: a strike that isn't anchored to this
    # support is out whatever its quote, and reporting it as "no bid" would
    # hide the reasons that matter for the strikes that are.
    if gap > reach_pct + _EPS:
        reason = f"{gap:.1f}% {position} the support zone, past the {reach_pct:.1f}% reach"
    elif mid is None or not quote.bid:
        reason = "no two-sided quote (no bid)"
    elif spread_pct_of_mid(quote.bid, quote.ask, mid) > MAX_SPREAD_PCT_OF_MID:
        reason = f"bid/ask spread wider than {MAX_SPREAD_PCT_OF_MID:g}% of the mid"
    elif delta is None:
        reason = "no implied volatility, so no delta"
    elif net <= 0:
        reason = "premium doesn't cover the fee"
    elif not low_delta <= delta <= high_delta:
        reason = f"delta outside {low_delta:.2f} to {high_delta:.2f}"
    elif annualized < MIN_ANNUALIZED_RETURN_PCT:
        reason = f"{annualized:.1f}% annualized, under the {MIN_ANNUALIZED_RETURN_PCT:g}% minimum"
    elif effective <= 0:
        reason = "effective purchase price isn't positive"

    components: dict[str, float] = {}
    score = None
    if reason is None:
        components = {
            "support": zone.score / 100.0,
            "delta": max(0.0, 1.0 - abs(delta - TARGET_DELTA) / DELTA_SCORE_SPAN),
            "return": min(1.0, annualized / FULL_CREDIT_ANNUALIZED_RETURN_PCT),
            "effective_price": _clamp(effective_discount / FULL_CREDIT_EFFECTIVE_DISCOUNT_PCT, 0.0, 1.0),
            "zone_position": 1.0 if reach_pct <= 0 else max(0.0, 1.0 - gap / reach_pct),
        }
        score = sum(CANDIDATE_WEIGHTS[name] * credit for name, credit in components.items())
    return PutCandidate(
        strike=quote.strike,
        bid=quote.bid,
        ask=quote.ask,
        delta=delta,
        mid_premium=mid,
        net_premium=net,
        annualized_return_pct=annualized,
        downside_to_strike_pct=downside,
        effective_purchase_price=effective,
        effective_discount_pct=effective_discount,
        zone_position=position,
        zone_gap_pct=gap,
        components=components,
        score=score,
        rejected_reason=reason,
        open_interest=quote.open_interest,
        volume=quote.volume,
    )


def _no_candidate_reason(candidates: Sequence[PutCandidate], zone: SupportZone, reach_pct: float) -> str:
    """Names the commonest rejections among the strikes near the zone, so
    the reason says what failed where it matters, not just that nothing passed."""
    near = [c for c in candidates if c.zone_gap_pct <= reach_pct + _EPS]
    where = f"the {zone_text(zone)} support zone"
    if not near:
        return f"No listed put strike within {reach_pct:.1f}% of {where}."
    counts: dict[str, int] = {}
    for candidate in near:
        key = candidate.rejected_reason or ""
        # Group the per-strike numbers out of the message ("12.3% annualized ...").
        if "annualized" in key:
            key = f"under the {MIN_ANNUALIZED_RETURN_PCT:g}% annualized minimum"
        counts[key] = counts.get(key, 0) + 1
    top = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:3]
    detail = "; ".join(f"{n} {why}" for why, n in top)
    return f"No put near {where} passes every check ({detail})."


def calculate_csp_to_enter(
    *,
    current_price: float | None,
    bars: Sequence[PricePoint],
    expiry: date | None,
    today: date,
    puts: Sequence[PutQuote] | None,
    chain_unavailable_reason: str | None = None,
    risk_free_rate: float = 0.0,
    quotes_as_of: datetime | None = None,
    fetched_at: datetime | None = None,
    calculated_at: datetime | None = None,
) -> CspEntry:
    """The canonical CSP TO ENTER for one ticker. ``bars``: its daily
    history, oldest first. ``puts``: the put side of the chain for
    ``expiry``; ``None`` when it couldn't be loaded (``chain_unavailable_reason``
    says why)."""
    if current_price is None or current_price <= 0:
        return CspEntry(
            STATUS_NO_QUALIFYING,
            "No current stock price.",
            current_price,
            calculated_at=calculated_at,
            market_data_missing=True,
            today=today,
        )
    return recommend_put(
        current_price=current_price,
        support=analyze_support(bars),
        expiry=expiry,
        today=today,
        puts=puts,
        chain_unavailable_reason=chain_unavailable_reason,
        risk_free_rate=risk_free_rate,
        quotes_as_of=quotes_as_of,
        fetched_at=fetched_at,
        calculated_at=calculated_at,
    )


def recommend_put(
    *,
    current_price: float,
    support: SupportAnalysis,
    expiry: date | None,
    today: date,
    puts: Sequence[PutQuote] | None,
    chain_unavailable_reason: str | None = None,
    risk_free_rate: float = 0.0,
    quotes_as_of: datetime | None = None,
    fetched_at: datetime | None = None,
    calculated_at: datetime | None = None,
) -> CspEntry:
    """Steps 2-4 of the module docstring, given an already-built support
    analysis: pick the relevant zone, then the best qualifying put near it.
    ``quotes_as_of`` / ``fetched_at`` / ``calculated_at`` are carried onto the
    result unchanged, so every display shows the same snapshot time."""
    stamps = {"quotes_as_of": quotes_as_of, "fetched_at": fetched_at, "calculated_at": calculated_at, "today": today}
    zone, why_not = select_support_zone(support, current_price)
    if zone is None:
        return CspEntry(
            STATUS_NO_QUALIFYING,
            why_not,
            current_price,
            support=support,
            market_data_missing=support.sessions_analyzed < SUPPORT_MIN_BARS,
            **stamps,
        )
    found = {
        **stamps,
        "current_price": current_price,
        "support": support,
        "zone": zone,
        "zone_distance_pct": support_distance_pct(zone, current_price),
    }

    if expiry is None:
        return CspEntry(
            STATUS_NO_QUALIFYING,
            chain_unavailable_reason or "No listed option expiration to check.",
            market_data_missing=True,
            **found,
        )
    days_to_expiry = (expiry - today).days
    found = {**found, "expiry": expiry, "days_to_expiry": days_to_expiry}
    if puts is None:
        return CspEntry(
            STATUS_NO_QUALIFYING,
            chain_unavailable_reason or "Option prices couldn't be loaded.",
            market_data_missing=True,
            **found,
        )
    if days_to_expiry < 1:
        return CspEntry(STATUS_NO_QUALIFYING, "The chosen expiration has no time left.", **found)

    reach_pct = STRIKE_REACH_ZONE_WIDTHS * support.zone_width_pct
    candidates = tuple(
        _evaluate(
            quote,
            current_price=current_price,
            days_to_expiry=days_to_expiry,
            zone=zone,
            reach_pct=reach_pct,
            risk_free_rate=risk_free_rate,
        )
        for quote in sorted(puts, key=lambda q: q.strike)
        if quote.strike < current_price - _EPS
    )
    if not candidates:
        return CspEntry(STATUS_NO_QUALIFYING, "No listed put strike below the current price.", **found)
    qualifying = [c for c in candidates if c.qualifies]
    if not qualifying:
        return CspEntry(STATUS_NO_QUALIFYING, _no_candidate_reason(candidates, zone, reach_pct), candidates=candidates, **found)

    # Scores compared at 4 decimals so float noise can't break a genuine tie;
    # a tie goes to the delta nearer the target, then the lower strike.
    pick = max(
        qualifying, key=lambda c: (round(c.score, 4), -round(abs(c.delta - TARGET_DELTA), 4), -c.strike)
    )
    return CspEntry(STATUS_RECOMMENDED, recommendation_reason(pick, zone), pick=pick, candidates=candidates, **found)


# ---- explanation -----------------------------------------------------------


def _money(value: float) -> str:
    return tc.money(value)


def zone_text(zone: SupportZone) -> str:
    return f"{_money(zone.low)} to {_money(zone.high)}"


def _zone_position_text(pick: PutCandidate, zone: SupportZone) -> str:
    if pick.zone_position == INSIDE:
        return f"{_money(pick.strike)} is inside the {zone_text(zone)} support zone"
    side = "above" if pick.zone_position == ABOVE else "below"
    return f"{_money(pick.strike)} is {pick.zone_gap_pct:.1f}% {side} the {zone_text(zone)} support zone"


def recommendation_reason(pick: PutCandidate, zone: SupportZone) -> str:
    low, high = PREFERRED_DELTA_RANGE
    delta_note = "near" if low <= pick.delta <= high else "outside the preferred range around"
    return (
        f"{_zone_position_text(pick, zone)} (strength {zone.score:.0f}/100), its delta {pick.delta:.2f} is "
        f"{delta_note} the {TARGET_DELTA:.2f} target, and it returns {pick.annualized_return_pct:.1f}% "
        f"annualized (minimum {MIN_ANNUALIZED_RETURN_PCT:g}%)."
    )


def _nearby_candidates(entry: CspEntry) -> list[PutCandidate]:
    """Up to four strikes closest to the zone (the pick always among them)."""
    ranked = sorted(entry.candidates, key=lambda c: (c.zone_gap_pct or 0.0, -c.strike))
    shown = sorted(ranked[:4], key=lambda c: -c.strike)
    if entry.pick and entry.pick not in shown:
        shown = sorted(shown[:3] + [entry.pick], key=lambda c: -c.strike)
    return shown


# ---- tooltip card ----------------------------------------------------------

STRATEGY = "CSP TO ENTER"
SUPPORT_CAVEAT = (
    "Historical price levels are estimates based on past data. They do not guarantee that the "
    "stock will stop falling."
)
SCORE_NOTE = (
    "The support score ranks past evidence (swing lows, how recent, bounce size, moving averages, "
    "volume, round numbers) from 0 to 100. It is not a probability that support holds."
)


def card_status(entry: CspEntry) -> str:
    if entry.status == STATUS_RECOMMENDED:
        return tc.STATUS_QUALIFIES
    return tc.STATUS_UNVERIFIED if entry.market_data_missing else tc.STATUS_DOES_NOT_QUALIFY


def _per_share(value: float | None) -> str:
    """Per-share premium to 4 decimals, so the formulas below reproduce exactly."""
    return "Unavailable" if value is None else f"${value:,.4f}"


def _per_contract(per_share: float | None) -> str:
    return "Unavailable" if per_share is None else _money(per_share * OPTION_MULTIPLIER)


def _why(entry: CspEntry) -> list[str]:
    pick, zone = entry.pick, entry.zone
    low, high = PREFERRED_DELTA_RANGE
    hard_low, hard_high = ACCEPTABLE_DELTA_RANGE
    if low <= pick.delta <= high:
        delta_line = f"Delta {pick.delta:.2f} is inside the preferred {low:.2f} to {high:.2f} range."
    else:
        delta_line = (
            f"Delta {pick.delta:.2f} is outside the preferred {low:.2f} to {high:.2f} range, but inside "
            f"the allowed {hard_low:.2f} to {hard_high:.2f}."
        )
    return [
        f"{pick.annualized_return_pct:.1f}% annualized screening return, above the "
        f"{MIN_ANNUALIZED_RETURN_PCT:g}% minimum.",
        delta_line,
        f"Strike {_zone_position_text(pick, zone)} (support score {zone.score:.0f}/100).",
    ]


def _strike_table(entry: CspEntry) -> dict | None:
    rows = []
    for candidate in _nearby_candidates(entry):
        picked = candidate is entry.pick
        if picked:
            reason = None
        elif candidate.qualifies:
            reason = f"Ranked lower: {candidate.score:.0f}/100 vs the pick's {entry.pick.score:.0f}/100"
        else:
            reason = candidate.rejected_reason[:1].upper() + candidate.rejected_reason[1:]
        rows.append(
            tc.table_row(
                [
                    _money(candidate.strike),
                    tc.delta(candidate.delta),
                    _per_contract(candidate.net_premium),
                    tc.pct(candidate.annualized_return_pct),
                    candidate.spread_check,
                ],
                picked=picked,
                qualifies=candidate.qualifies,
                reason=reason,
            )
        )
    return tc.table(
        ["Strike", "Delta", "Net / contract", "Ann. return", "Spread"],
        [True, True, True, True, False],
        rows,
    )


def _moving_average_rows(entry: CspEntry) -> list[tuple[str, str]]:
    rows = []
    for name, value in entry.support.moving_averages.items():
        gap = 100.0 * (value - entry.current_price) / entry.current_price
        side = "above" if gap >= 0 else "below"
        rows.append((name, f"{_money(value)}, {abs(gap):.1f}% {side} the price"))
    return rows


def _support_section(entry: CspEntry) -> dict | None:
    support, zone = entry.support, entry.zone
    if support is None:
        return None
    rows: list[tuple[str, str]] = []
    if zone is not None:
        last_day = zone.touch_days[-1] if zone.touch_days else None
        rows += [
            ("Support zone", zone_text(zone)),
            ("Zone top vs price", f"{entry.zone_distance_pct:.1f}% below"),
            ("Support score", f"{zone.score:.0f}/100"),
            ("Swing lows", f"{zone.touches}: " + ", ".join(tc.day(d) for d in zone.touch_days)),
            ("Last touch", f"{tc.day(last_day)}, {zone.sessions_since_last_touch} sessions ago"),
            ("Average bounce", f"{zone.avg_bounce_pct:.1f}% (highest high within {BOUNCE_WINDOW_BARS} sessions)"),
            (
                "Touch-day volume",
                f"{zone.volume_ratio:.1f}x the median day" if zone.volume_ratio is not None else "Unavailable",
            ),
            ("Closed through since", f"{zone.breaks}x (each halves the score)" if zone.breaks else "Never"),
            ("Round number", _money(zone.round_level) if zone.round_level is not None else "None in the zone"),
        ]
        if entry.pick is not None:
            rows.insert(2, ("Strike vs zone", _zone_position_text(entry.pick, zone)))
    rows += _moving_average_rows(entry)
    notes = [
        SCORE_NOTE,
        SUPPORT_CAVEAT,
        f"Based on the last {support.sessions_analyzed} daily sessions. A swing low is the lowest daily low "
        f"within {SWING_PIVOT_BARS} sessions either side, followed by a bounce of at least "
        f"max({SWING_MIN_BOUNCE_PCT:g}%, {SWING_MIN_BOUNCE_ATR:g} ATR). Moving averages use daily closes.",
        *support.limitations,
    ]
    return tc.section("Historical support", rows=rows, notes=notes)


def _quote_section(entry: CspEntry) -> dict | None:
    pick = entry.pick
    if pick is None:
        return None
    spread = pick.spread_pct_of_mid
    spread_text = (
        f"{_money(pick.ask - pick.bid)} ({spread:.1f}% of the midpoint): {pick.spread_check}, "
        f"limit {MAX_SPREAD_PCT_OF_MID:g}%"
        if spread is not None
        else pick.spread_check
    )
    rows = [
        ("Bid", _money(pick.bid) if pick.bid is not None else "Unavailable"),
        ("Ask", _money(pick.ask) if pick.ask is not None else "Unavailable"),
        ("Midpoint", f"{_per_share(pick.mid_premium)} (an estimate, not a guaranteed fill)"),
        ("Spread", spread_text),
        ("Open interest", tc.count(pick.open_interest)),
        ("Volume today", tc.count(pick.volume)),
        ("Prices as of", tc.when(entry.quotes_as_of)),
        ("Fetched", tc.when(entry.fetched_at)),
    ]
    notes = ["Open interest and volume are shown for judgment only; they are not filters."]
    return tc.section("Option quote and liquidity", rows=rows, notes=notes)


def _calculation_section(entry: CspEntry) -> dict | None:
    rows: list[tuple[str, str]] = []
    pick = entry.pick
    if entry.expiry is not None and entry.days_to_expiry is not None:
        low_dte, high_dte = CSP_DTE_WINDOW
        rule = (
            f"closest to {CSP_TARGET_DTE} inside {low_dte} to {high_dte}"
            if entry.expiry_in_window
            else f"none listed {low_dte} to {high_dte} days out, so the closest to {CSP_TARGET_DTE}"
        )
        rows.append(
            ("DTE", f"{entry.days_to_expiry} calendar days, {tc.day(entry.today)} to {tc.day(entry.expiry)} ({rule})")
        )
    if pick is not None:
        rows += [
            (
                "Net premium / share",
                f"{_per_share(pick.mid_premium)} - (${OPTION_FEE_PER_CONTRACT:.2f} fee ÷ 100) = "
                f"{_per_share(pick.net_premium)}",
            ),
            ("Net premium / contract", f"{_per_share(pick.net_premium)} × 100 = {_per_contract(pick.net_premium)}"),
            (
                "Effective cost / share",
                f"{_money(pick.strike)} - {_per_share(pick.net_premium)} = {_money(pick.effective_purchase_price)}",
            ),
            (
                "Annualized return",
                f"{_per_share(pick.net_premium)} ÷ {_money(pick.strike)} × 365 ÷ {entry.days_to_expiry} = "
                f"{pick.annualized_return_pct:.1f}%",
            ),
            (
                "Price to strike",
                f"({_money(entry.current_price)} - {_money(pick.strike)}) ÷ {_money(entry.current_price)} = "
                f"{pick.downside_to_strike_pct:.1f}%",
            ),
            ("Ranking score", f"{pick.score:.0f}/100 (support, delta, return, discount, zone position)"),
        ]
    low, high = PREFERRED_DELTA_RANGE
    hard_low, hard_high = ACCEPTABLE_DELTA_RANGE
    rows.append(
        (
            "Delta rule",
            f"target {TARGET_DELTA:.2f}; preferred {low:.2f} to {high:.2f}; allowed {hard_low:.2f} to {hard_high:.2f}",
        )
    )
    rows.append(("Calculated", tc.when(entry.calculated_at)))
    return tc.section("Calculation details", rows=rows)


def tooltip_card(entry: CspEntry, ticker: str | None = None) -> dict:
    """The CSP TO ENTER tooltip every table and card shows, built from this
    one result (layout: ``wheel/tooltipcard.py``)."""
    status = card_status(entry)
    common = {
        "strategy": STRATEGY,
        "ticker": ticker,
        "status": status,
        "subtitle_text": tc.subtitle(entry.current_price, entry.expiry, entry.days_to_expiry, entry.today),
        "quoted_text": tc.quoted(entry.quotes_as_of, entry.fetched_at),
        "footer": (
            "Premium uses the bid/ask midpoint, an estimate, not a guaranteed fill. Annualized return is a "
            "screening figure for comparing puts, not a forecast."
        ),
    }
    strikes = tc.section(
        "Strikes near the support zone",
        table=_strike_table(entry) if entry.candidates else None,
        open_=status != tc.STATUS_QUALIFIES,
    )
    sections = [strikes, _support_section(entry), _quote_section(entry), _calculation_section(entry)]
    if entry.status != STATUS_RECOMMENDED:
        return tc.card(**common, summary=entry.reason, sections=sections)

    pick = entry.pick
    low, high = PREFERRED_DELTA_RANGE
    qualifying = sum(1 for c in entry.candidates if c.qualifies)
    who = ticker or "the stock"
    return tc.card(
        **common,
        headline=tc.item(
            "Recommended put",
            _money(pick.strike),
            f"Ranked first of {qualifying} qualifying put{'s' if qualifying != 1 else ''}",
        ),
        headline_side=tc.item(
            "Delta", tc.delta(pick.delta), f"Target {TARGET_DELTA:.2f} · preferred {low:.2f} to {high:.2f}"
        ),
        metrics=[
            tc.item(
                "Net premium / contract",
                _per_contract(pick.net_premium),
                f"Midpoint less the ${OPTION_FEE_PER_CONTRACT:.2f} fee",
            ),
            tc.item("Effective cost / share", _money(pick.effective_purchase_price), "If assigned"),
            tc.item(
                "Annualized screening return",
                tc.pct(pick.annualized_return_pct),
                f"Minimum {MIN_ANNUALIZED_RETURN_PCT:g}%",
            ),
            tc.item("Cash collateral / contract", _money(pick.strike * OPTION_MULTIPLIER)),
        ],
        why=_why(entry),
        risk={
            "title": "If assigned",
            "text": (
                f"You must buy 100 shares per contract at {_money(pick.strike)} "
                f"({_money(pick.strike * OPTION_MULTIPLIER)} per contract). The premium lowers your cost to "
                f"about {_money(pick.effective_purchase_price)} a share, but it does not limit losses if {who} "
                "keeps falling."
            ),
            "stats": [
                tc.item("Strike purchase price", _money(pick.strike)),
                tc.item("Effective cost after premium", _money(pick.effective_purchase_price)),
                tc.item("Price to strike", f"{pick.downside_to_strike_pct:.1f}% lower"),
                tc.item("Effective cost vs price", f"{pick.effective_discount_pct:.1f}% lower"),
            ],
        },
        sections=sections,
    )
