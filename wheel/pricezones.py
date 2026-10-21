"""Historical price zones from daily swing points: support (swing lows the
price bounced up from) for CSP TO ENTER, resistance (swing highs the price
pulled back down from) for CC TO EXIT. One method for both sides, so the two
can't drift apart; every rule below is the support rule, mirrored for
resistance.

Pure: no network, no clock. Input is ascending daily bars; nothing after a
bar is used to judge it except the fixed windows named below, and the last
``SWING_PIVOT_BARS`` sessions can't be swing points yet (their right side
hasn't formed).

1. Swing points, from the last ~12 months. Support: a daily low strictly
   below the 5 prior lows and no higher than the 5 after; resistance: a
   daily high strictly above the 5 prior highs and no lower than the 5
   after (a flat double top/bottom counts once, at its first day). Either
   must be followed within 20 sessions by a move away of at least max(3%,
   1.5 ATR): the highest high after a low, the lowest low after a high.
2. Zones: points within one zone width (1.5 x ATR(14), kept between 2.5%
   and 6%) of a zone's outermost level join it. Support grows up from its
   floor, resistance down from its ceiling, so a staircase can't chain into
   one wide zone.
3. Score 0-100: touches 30% (full at 4), recency 20% (half-life 63
   sessions), the move away 20% (each capped at max(4 ATR, 2 x the minimum
   move)), moving-average confluence 15% (21-day EMA, 50/100/200-day MA
   within one zone width; full at 2), touch-day volume vs the median day 10%
   (0.5x -> 0, 2x -> full) and a round number 5%. A factor with no data
   drops out and the rest re-normalize. x 0.5 for every close clearly
   through the zone (more than one zone width past it) after the first
   touch; x (0.5 + 0.5 x sessions / 126) under ~6 months of history.

The score is a relative ranking of past evidence, never a probability that
a level holds.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import date
from typing import Sequence

from wheel.marketdata import PricePoint

SUPPORT = "support"
RESISTANCE = "resistance"
SIDES = (SUPPORT, RESISTANCE)

LOOKBACK_BARS = 252  # ~12 months of sessions
FULL_CONFIDENCE_BARS = 126  # ~6 months; less scales the score down
# With less history the score is scaled by 0.5 + 0.5 x (sessions / 126): thin
# history weakens the evidence but never zeroes it on its own.
HISTORY_CONFIDENCE_FLOOR = 0.5
MIN_BARS = 40  # below this there is too little to find swing points at all
ATR_PERIOD = 14
SWING_PIVOT_BARS = 5  # the extreme within this many sessions either side
REACTION_WINDOW_BARS = 20  # the move away is measured over the following ~month
# A swing point needs a move away of at least max(3%, 1.5 ATR) to count -- a
# fixed 3% alone would accept noise on a volatile name and reject real turns
# on a calm one.
SWING_MIN_REACTION_PCT = 3.0
SWING_MIN_REACTION_ATR = 1.5
# Zone width (how far inside a zone's outermost level another point may sit
# and still join it): 1.5 ATR, kept between 2.5% and 6% of price (2.5% so a
# $140-$143 shelf on a $150 stock reads as one zone even on a calm name).
ZONE_WIDTH_ATR_MULTIPLE = 1.5
ZONE_WIDTH_PCT_BOUNDS = (2.5, 6.0)
TOUCHES_FOR_FULL_CREDIT = 4
RECENCY_HALF_LIFE_BARS = 63  # a touch ~3 months old counts half as much as today's
# A move away of max(4 ATR, 2x the minimum swing move) earns full credit; each
# touch is capped there before averaging so one outsized move can't carry the
# zone. The second term keeps a very calm stock's bar meaningful.
REACTION_FULL_CREDIT_ATR = 4.0
REACTION_FULL_CREDIT_MIN_MULTIPLE = 2.0
MA_NEAR_ZONE_FOR_FULL_CREDIT = 2
# Touch-day volume / median daily volume: 0.5x or less earns nothing, 2x or more full.
VOLUME_RATIO_RANGE = (0.5, 2.0)
# Each close-through episode after the first touch halves the zone's score.
BREAK_PENALTY_FACTOR = 0.5

# "bounce" is the move away from the zone after a touch: up off support,
# down off resistance.
ZONE_WEIGHTS = {
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


@dataclass(frozen=True)
class PriceZone:
    side: str
    low: float
    high: float
    touch_levels: tuple[float, ...]  # the swing lows (support) or highs (resistance), oldest first
    touch_days: tuple[date, ...]
    sessions_since_last_touch: int
    avg_reaction_pct: float  # average move away after a touch, % of the touch level
    breaks: int
    moving_averages_near: tuple[str, ...]
    round_level: float | None
    volume_ratio: float | None
    # Each factor's 0-1 credit; a factor with no data is absent, not zero.
    components: dict[str, float]
    score: float  # 0-100, after the break penalty and history scaling

    @property
    def touches(self) -> int:
        return len(self.touch_levels)


@dataclass(frozen=True)
class StrikeGuidance:
    """Where one option strike sits against a zone, and what to consider:
    CSP TO ENTER's support guidance and CC TO EXIT's resistance guidance
    share this shape. Advice only; it never rejects or re-ranks a strike."""

    strike: float
    position: str  # the strategy's own position key, e.g. "just_below"
    label: str  # short, for the strike table's column
    title: str  # bold lead-in of the advice line
    text: str  # one sentence: what it means and what to consider


@dataclass(frozen=True)
class ZoneAnalysis:
    side: str
    sessions_analyzed: int
    atr_pct: float | None
    zone_width_pct: float | None
    moving_averages: dict[str, float]
    zones: tuple[PriceZone, ...]  # strongest first
    history_confidence: float  # 1.0 with >= ~6 months of bars
    limitations: tuple[str, ...]


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


# ---- swing points and zones ------------------------------------------------


@dataclass(frozen=True)
class _SwingPoint:
    index: int
    level: float
    reaction_pct: float
    volume: float | None


def _swing_points(bars: Sequence[PricePoint], start: int, min_reaction_pct: float, side: str) -> list[_SwingPoint]:
    """Swing lows (support) or highs (resistance) inside ``bars[start:]``:
    strictly beyond the prior ``SWING_PIVOT_BARS`` extremes, at least as
    extreme as the next ``SWING_PIVOT_BARS`` (so a flat double bottom/top
    counts once, at its first day), and followed by a move away of at least
    ``min_reaction_pct`` within ``REACTION_WINDOW_BARS``."""
    points: list[_SwingPoint] = []
    for i in range(start, len(bars) - SWING_PIVOT_BARS):
        left = bars[max(0, i - SWING_PIVOT_BARS) : i]
        right = bars[i + 1 : i + 1 + SWING_PIVOT_BARS]
        after = bars[i + 1 : i + 1 + REACTION_WINDOW_BARS]
        if side == SUPPORT:
            level = bars[i].low
            if level <= 0 or (left and level >= min(bar.low for bar in left)) or level > min(bar.low for bar in right):
                continue
            reaction_pct = 100.0 * (max(bar.high for bar in after) - level) / level
        else:
            level = bars[i].high
            if level <= 0 or (left and level <= max(bar.high for bar in left)) or level < max(bar.high for bar in right):
                continue
            reaction_pct = 100.0 * (level - min(bar.low for bar in after)) / level
        if reaction_pct >= min_reaction_pct:
            points.append(_SwingPoint(i, level, reaction_pct, bars[i].volume))
    return points


def _cluster(points: Sequence[_SwingPoint], width_pct: float, side: str) -> list[list[_SwingPoint]]:
    """Greedy from the outermost level inward: a point joins the current zone
    while it sits within ``width_pct`` of that zone's anchor (support: its
    lowest low; resistance: its highest high); otherwise it starts the next
    zone. Anchoring to the outermost level (not the previous point) keeps a
    staircase from chaining into one wide zone."""
    clusters: list[list[_SwingPoint]] = []
    if side == SUPPORT:
        for point in sorted(points, key=lambda p: (p.level, p.index)):
            if clusters and point.level <= clusters[-1][0].level * (1 + width_pct / 100):
                clusters[-1].append(point)
            else:
                clusters.append([point])
    else:
        for point in sorted(points, key=lambda p: (-p.level, p.index)):
            if clusters and point.level >= clusters[-1][0].level * (1 - width_pct / 100):
                clusters[-1].append(point)
            else:
                clusters.append([point])
    return clusters


def _break_episodes(
    bars: Sequence[PricePoint], since: int, zone_low: float, zone_high: float, width_pct: float, side: str
) -> int:
    """Times price closed clearly through the zone (more than one zone width
    past it: under support's bottom, over resistance's top) after its first
    touch. One episode per stretch beyond, however many sessions it lasts."""
    episodes, beyond = 0, False
    if side == SUPPORT:
        line = zone_low * (1 - width_pct / 100)
        for bar in bars[since:]:
            if bar.close < line and not beyond:
                episodes += 1
            beyond = bar.close < zone_low if beyond else bar.close < line
    else:
        line = zone_high * (1 + width_pct / 100)
        for bar in bars[since:]:
            if bar.close > line and not beyond:
                episodes += 1
            beyond = bar.close > zone_high if beyond else bar.close > line
    return episodes


def _volume_baseline(bars: Sequence[PricePoint]) -> float | None:
    """Median daily volume, or ``None`` when fewer than half the sessions
    carry a positive volume -- too sparse to compare touches against."""
    volumes = [bar.volume for bar in bars if bar.volume is not None and bar.volume > 0]
    if len(volumes) < len(bars) / 2:
        return None
    return statistics.median(volumes)


def _score_zone(
    members: Sequence[_SwingPoint],
    *,
    side: str,
    bars: Sequence[PricePoint],
    width_pct: float,
    atr_pct: float,
    min_reaction_pct: float,
    moving_averages: dict[str, float],
    volume_baseline: float | None,
    history_confidence: float,
) -> PriceZone:
    levels = [m.level for m in members]
    zone_low, zone_high = min(levels), max(levels)
    by_time = sorted(members, key=lambda m: m.index)
    sessions_since = len(bars) - 1 - by_time[-1].index

    components: dict[str, float] = {}
    components["touches"] = min(1.0, len(members) / TOUCHES_FOR_FULL_CREDIT)
    components["recency"] = 0.5 ** (sessions_since / RECENCY_HALF_LIFE_BARS)

    full_reaction = max(REACTION_FULL_CREDIT_ATR * atr_pct, REACTION_FULL_CREDIT_MIN_MULTIPLE * min_reaction_pct)
    avg_reaction = sum(m.reaction_pct for m in members) / len(members)
    components["bounce"] = sum(min(m.reaction_pct, full_reaction) for m in members) / len(members) / full_reaction

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

    weight_total = sum(ZONE_WEIGHTS[name] for name in components)
    weighted = sum(ZONE_WEIGHTS[name] * credit for name, credit in components.items()) / weight_total
    breaks = _break_episodes(bars, by_time[0].index, zone_low, zone_high, width_pct, side)
    score = 100.0 * weighted * (BREAK_PENALTY_FACTOR**breaks) * history_confidence

    return PriceZone(
        side=side,
        low=zone_low,
        high=zone_high,
        touch_levels=tuple(m.level for m in by_time),
        touch_days=tuple(bars[m.index].day for m in by_time),
        sessions_since_last_touch=sessions_since,
        avg_reaction_pct=avg_reaction,
        breaks=breaks,
        moving_averages_near=near,
        round_level=round_level,
        volume_ratio=volume_ratio,
        components=components,
        score=score,
    )


def analyze(bars: Sequence[PricePoint], side: str) -> ZoneAnalysis:
    """Every support or resistance zone in the last ``LOOKBACK_BARS``
    sessions of ``bars`` (ascending daily bars; ones without a high/low are
    skipped), strongest first. Short history degrades rather than fails: the
    score is scaled by sessions / ``FULL_CONFIDENCE_BARS`` and the shortfall
    is listed in ``limitations``."""
    if side not in SIDES:
        raise ValueError(f"side must be one of {SIDES}, not {side!r}")
    ranged = [bar for bar in bars if bar.has_range and bar.low > 0]
    closes = [bar.close for bar in bars]
    limitations: list[str] = []
    if len(ranged) < len(bars):
        limitations.append(f"{len(bars) - len(ranged)} session(s) had no daily high/low and were skipped.")
    if len(ranged) < MIN_BARS:
        limitations.append(
            f"Only {len(ranged)} sessions of daily high/low history; at least {MIN_BARS} are needed to find {side}."
        )
        return ZoneAnalysis(side, len(ranged), None, None, {}, (), 0.0, tuple(limitations))

    start = max(0, len(ranged) - LOOKBACK_BARS)
    sessions = len(ranged) - start
    coverage = min(1.0, sessions / FULL_CONFIDENCE_BARS)
    history_confidence = HISTORY_CONFIDENCE_FLOOR + (1 - HISTORY_CONFIDENCE_FLOOR) * coverage
    if history_confidence < 1.0:
        limitations.append(
            f"Limited history: {sessions} sessions (about {sessions / 21:.0f} months) of the "
            f"{FULL_CONFIDENCE_BARS}+ wanted, so strength is scaled to {history_confidence:.0%}."
        )

    atr_pct = average_true_range_pct(ranged)
    width_pct = _clamp(ZONE_WIDTH_ATR_MULTIPLE * atr_pct, *ZONE_WIDTH_PCT_BOUNDS)
    min_reaction_pct = max(SWING_MIN_REACTION_PCT, SWING_MIN_REACTION_ATR * atr_pct)

    moving_averages: dict[str, float] = {}
    for name, period, kind in MOVING_AVERAGES:
        value = exponential_moving_average(closes, period) if kind == "ema" else simple_moving_average(closes, period)
        if value is not None:
            moving_averages[name] = value
    missing = [name for name, _, _ in MOVING_AVERAGES if name not in moving_averages]
    if missing:
        limitations.append(f"Not enough history for the {', '.join(missing)}.")

    volume_baseline = _volume_baseline(ranged[start:])
    if volume_baseline is None:
        limitations.append("No reliable volume data; strength uses the other factors.")

    points = _swing_points(ranged, start, min_reaction_pct, side)
    zones = [
        _score_zone(
            members,
            side=side,
            bars=ranged,
            width_pct=width_pct,
            atr_pct=atr_pct,
            min_reaction_pct=min_reaction_pct,
            moving_averages=moving_averages,
            volume_baseline=volume_baseline,
            history_confidence=history_confidence,
        )
        for members in _cluster(points, width_pct, side)
    ]
    # Strongest first; on a tie, the level nearer the middle of the range
    # first (support: the higher zone; resistance: the lower one).
    if side == SUPPORT:
        zones.sort(key=lambda zone: (-zone.score, -zone.high))
    else:
        zones.sort(key=lambda zone: (-zone.score, zone.low))
    return ZoneAnalysis(
        side, sessions, atr_pct, width_pct, moving_averages, tuple(zones), history_confidence, tuple(limitations)
    )
