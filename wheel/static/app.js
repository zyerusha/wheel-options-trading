/* Wheel dashboard front end.
 *
 * Charts are hand-built SVG: no external library, no network beyond /api.
 * Conventions held throughout, per the dataviz brief:
 *   - one filter row scopes every stat, chart and table on the page;
 *   - categorical hues are assigned by entity in fixed slot order, never by rank;
 *   - sign uses the diverging pair (blue gain / red loss), never a rainbow;
 *   - every chart has a table twin, so no value is reachable only by hovering;
 *   - all data-derived text is inserted with textContent, never innerHTML.
 */

'use strict';

const SVG_NS = 'http://www.w3.org/2000/svg';

// The Combined-aggregate-view sentinel -- matches wheel/accounts.py's
// COMBINED_ACCOUNT_ID. A single shared constant rather than the raw string
// repeated at every comparison site, so a typo'd comparison fails fast
// (ReferenceError) instead of silently falling through to per-account
// behavior.
const COMBINED_ACCOUNT_ID = 'combined';

const state = {
  data: null,
  tickers: new Set(),
  statuses: new Set(),
  start: null,
  end: null,
  expanded: new Set(),
  cycleSort: { key: 'net_realized_pl', dir: -1 },
  // How the capital chart expresses its bands: 'value' (dollars) or 'share' (%
  // of the day's total). A view of one chart, not a filter -- it changes no data.
  capitalMode: 'value',
  // Which data/<account>/ folder is active, or COMBINED_ACCOUNT_ID for every
  // account aggregated without merging their cycles. Affects every chart and
  // table on the page, not just Net worth & benchmark.
  account: COMBINED_ACCOUNT_ID,
  // Top-level view: 'dashboard' (every analytics card) or 'tradelog' (one
  // wheel's transaction ledger). `tradeLogCycleId` is which wheel it shows;
  // `tradeLogTicker` (null = all) narrows the wheel list to one ticker.
  activeTab: 'dashboard',
  tradeLogCycleId: null,
  tradeLogTicker: null,
};

/* ---------------------------------------------------------------- utilities */

const $ = (id) => document.getElementById(id);

function el(tag, attrs = {}, text = null) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined) continue;
    if (key === 'class') node.className = value;
    else node.setAttribute(key, value);
  }
  if (text !== null) node.textContent = String(text);
  return node;
}

function svgEl(tag, attrs = {}, text = null) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined) continue;
    node.setAttribute(key, String(value));
  }
  if (text !== null) node.textContent = String(text);
  return node;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

function money(value, { cents = false, sign = false } = {}) {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  const options = cents
    ? { minimumFractionDigits: 2, maximumFractionDigits: 2 }
    : { maximumFractionDigits: 0 };
  const text = Math.abs(value).toLocaleString('en-US', options);
  const prefix = value < 0 ? '-$' : sign ? '+$' : '$';
  return value === 0 ? '$0' : prefix + text;
}

function compactMoney(value) {
  const abs = Math.abs(value);
  const unit = abs >= 1e6 ? [1e6, 'M'] : abs >= 1e3 ? [1e3, 'k'] : [1, ''];
  const scaled = value / unit[0];
  const digits = unit[0] === 1 ? 0 : Math.abs(scaled) < 10 ? 1 : 0;
  return (value < 0 ? '-$' : '$') + Math.abs(scaled).toFixed(digits) + unit[1];
}

function pct(value, digits = 1) {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return value.toFixed(digits) + '%';
}

/* ------------------------------------------------------ calculation tooltips
 *
 * Every calculated number on the page must be able to show the reader the
 * real formula and inputs behind it -- not a generic description, and not an
 * invented approximation. `formula([...lines])` just joins lines for a
 * native title-attribute tooltip (used on tiles and table cells); the richer
 * hover/keyboard tooltip (`showTooltip`/`attachTip`, for chart marks) takes
 * the same kind of string as its `formula` argument and renders it in a
 * dedicated block below the row readout. Raw, directly-sourced values (a
 * strike, a contract count, a broker-reported price) get no formula: there is
 * nothing to explain.
 */
function formula(lines) {
  return lines.join('\n');
}

/** Native hover tooltip carrying the calculation behind `node`'s value. */
function setFormula(node, text) {
  if (text) node.title = text;
}

/**
 * The CSP/covered-call vs. hedge breakdown behind an `option_realized_pl`
 * figure -- works on a portfolio, cycle, or ticker-row object alike, since
 * all three carry the same three fields (wheel/metrics.py: `option_realized_pl
 * = wheel_core_realized_pl + hedge_realized_pl`). "Hedge" is protective puts
 * and the long leg of any credit spread -- every option leg that is not a
 * plain CSP or covered call; the model has no notion of "this hedges that".
 */
function wheelOptionPlFormula(row, title) {
  return formula([
    `${title} =`,
    '  Cash-secured-put + covered-call profit/loss',
    '  + Hedge profit/loss (protective puts, credit-spread legs)',
    '',
    `= ${money(row.wheel_core_realized_pl, { cents: true })} + ${money(row.hedge_realized_pl, { cents: true })}`,
    `= ${money(row.option_realized_pl, { cents: true })}`,
  ]);
}

// Dates are parsed at local midnight, so they must be formatted locally too --
// toISOString() would shift them a day back in any negative-offset timezone.
const parseDay = (iso) => new Date(iso + 'T00:00:00');
const dayLabel = (iso) =>
  parseDay(iso).toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
const localIso = (time) => {
  const date = new Date(time);
  const pad = (value) => String(value).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
};

function niceTicks(min, max, count = 5) {
  if (min === max) return [min];
  const span = max - min;
  const raw = span / count;
  const magnitude = Math.pow(10, Math.floor(Math.log10(Math.abs(raw))));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * magnitude).find((s) => s >= raw) || magnitude * 10;
  const ticks = [];
  for (let value = Math.ceil(min / step) * step; value <= max + 1e-9; value += step) {
    ticks.push(Math.abs(value) < step / 1e6 ? 0 : value);
  }
  return ticks;
}

const DAY_MS = 24 * 60 * 60 * 1000;
// Fixed "nice" day-count ladder for evenly time-spaced x-axis ticks -- the
// same snap-to-a-round-step idea as niceTicks() above, just for a day span
// instead of a numeric one.
const NICE_DAY_STEPS = [1, 2, 3, 5, 7, 10, 14, 21, 30, 45, 60, 90, 120, 182, 365];

/** Smallest step off the ladder that fits `count` labels across `spanDays`;
 * beyond the ladder (a many-year chart), rounds up to a whole number of years
 * rather than falling off the end. */
function niceDayStep(spanDays, count) {
  const raw = spanDays / count;
  return NICE_DAY_STEPS.find((step) => step >= raw) || Math.ceil(raw / 365) * 365;
}

/* ------------------------------------------------------------------ tooltip */

const tooltip = $('tooltip');

// `formula`, when given, is the actual equation behind the row(s) above --
// e.g. "Annualized Wheel ROC =\n  (...) \n= $300 / $10,000 × 365/30\n= 36.5%".
// Every calculated (non-raw) value shown anywhere in the dashboard carries one,
// per the calculation-transparency rule: a reader must be able to see the real
// formula and reproduce the number, not just a label describing it in words.
function showTooltip(event, title, rows, formula) {
  clear(tooltip);
  tooltip.appendChild(el('div', { class: 'tt-title' }, title));
  for (const row of rows) {
    const line = el('div', { class: 'tt-row' });
    const key = el('span', { class: 'tt-key' });
    if (row.color) {
      const swatch = el('i');
      swatch.style.background = row.color;
      key.appendChild(swatch);
    }
    key.appendChild(document.createTextNode(row.label));
    line.appendChild(key);
    // Value leads: it is the strong element, the series name is secondary.
    line.appendChild(el('span', { class: 'tt-val' }, row.value));
    tooltip.appendChild(line);
  }
  if (formula) {
    tooltip.appendChild(el('div', { class: 'tt-formula' }, formula));
  }
  tooltip.classList.add('on');
  moveTooltip(event);
}

function moveTooltip(event) {
  const box = tooltip.getBoundingClientRect();
  let x = event.clientX + 14;
  let y = event.clientY + 14;
  if (x + box.width > window.innerWidth - 8) x = event.clientX - box.width - 14;
  if (y + box.height > window.innerHeight - 8) y = event.clientY - box.height - 14;
  tooltip.style.left = Math.max(8, x) + 'px';
  tooltip.style.top = Math.max(8, y) + 'px';
}

const hideTooltip = () => tooltip.classList.remove('on');

/** Attach hover + keyboard focus to a mark, with the same readout for both. */
function attachTip(node, title, rows, formula) {
  const show = (event) => showTooltip(event, title, rows, formula);
  node.addEventListener('pointerenter', show);
  node.addEventListener('pointermove', moveTooltip);
  node.addEventListener('pointerleave', hideTooltip);
  node.setAttribute('tabindex', '0');
  node.addEventListener('focus', () => {
    const box = node.getBoundingClientRect();
    showTooltip({ clientX: box.left + box.width / 2, clientY: box.top }, title, rows, formula);
  });
  node.addEventListener('blur', hideTooltip);
}

/* ------------------------------------------------------------- chart frame */

/** Build the plot frame: sized svg, y gridlines with labels, x baseline. */
function frame(svg, { width, height, margin, yMin, yMax, yFormat = compactMoney }) {
  clear(svg);
  svg.setAttribute('width', width);
  svg.setAttribute('height', height);
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);

  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const span = yMax - yMin || 1;
  const y = (value) => margin.top + plotHeight - ((value - yMin) / span) * plotHeight;

  const group = svgEl('g');
  svg.appendChild(group);

  for (const tick of niceTicks(yMin, yMax, 5)) {
    const yPos = y(tick);
    group.appendChild(
      svgEl('line', {
        class: 'grid-line',
        x1: margin.left,
        x2: margin.left + plotWidth,
        y1: yPos,
        y2: yPos,
      })
    );
    group.appendChild(
      svgEl(
        'text',
        { class: 'tick-label', x: margin.left - 8, y: yPos + 3.5, 'text-anchor': 'end' },
        yFormat(tick)
      )
    );
  }

  return { group, plotWidth, plotHeight, y, margin };
}

/**
 * Draw x-axis labels for a time scale, spaced at even TIME intervals rather
 * than every Nth data point. The two coincide only when the series has one
 * entry per calendar day (e.g. Capital deployed, explicitly zero-filled for
 * every day). A sparse, event-driven series (e.g. Cumulative P/L, which only
 * gets a row on a day something actually closed) would otherwise cluster
 * labels during busy stretches and leave uneven gaps during quiet ones, even
 * though the line itself is always plotted at each point's true chronological
 * x position -- only the label *placement* was ever tied to the data density.
 */
function timeAxis(group, dates, x, yBase, plotWidth) {
  group.appendChild(
    svgEl('line', {
      class: 'axis-line',
      x1: x(dates[0]),
      x2: x(dates[dates.length - 1]),
      y1: yBase,
      y2: yBase,
    })
  );

  const first = parseDay(dates[0]);
  const last = parseDay(dates[dates.length - 1]);
  const spanDays = (last.getTime() - first.getTime()) / DAY_MS;
  const maxLabels = Math.max(2, Math.floor(plotWidth / 78));
  const stepDays = spanDays > 0 ? niceDayStep(spanDays, maxLabels) : 1;

  for (let t = first.getTime(); t <= last.getTime(); t += stepDays * DAY_MS) {
    const iso = localIso(t);
    group.appendChild(
      svgEl(
        'text',
        { class: 'tick-label', x: x(iso), y: yBase + 16, 'text-anchor': 'middle' },
        dayLabel(iso)
      )
    );
  }
}

const chartWidth = (svg, min = 520) => Math.max(min, svg.parentElement.clientWidth || min);

const longDate = (iso) =>
  parseDay(iso).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });

/**
 * Crosshair, hit overlay and keyboard stepping for a time-series chart.
 *
 * The caller owns the readout: `onIndex` receives the focused index plus a
 * {clientX, clientY} anchor for the tooltip, and does whatever that chart needs.
 * When the move came from the keyboard the anchor is synthesised from the
 * crosshair's own box, so focus shows exactly what hover shows.
 *
 * The overlay is a single tab stop that then steps with the arrow keys -- 500+
 * daily points cannot each be their own tab stop, so `attachTip` (right for a
 * discrete mark) is the wrong tool here.
 */
function crosshairLayer(svg, group, geom, rows, xOf, { onIndex, onLeave, label }) {
  const { margin, plotWidth, plotHeight, width } = geom;

  const crosshair = svgEl('line', {
    class: 'axis-line',
    y1: margin.top,
    y2: margin.top + plotHeight,
    opacity: 0,
  });
  group.appendChild(crosshair);

  const overlay = svgEl('rect', {
    class: 'hit',
    x: margin.left,
    y: margin.top,
    width: plotWidth,
    height: plotHeight,
    tabindex: '0',
    'aria-label': label,
  });
  group.appendChild(overlay);

  let current = rows.length - 1; // the latest day is what a reader wants first

  const nearest = (px) => {
    let best = Infinity;
    let index = 0;
    rows.forEach((row, i) => {
      const distance = Math.abs(xOf(row) - px);
      if (distance < best) {
        best = distance;
        index = i;
      }
    });
    return index;
  };

  const place = (index, event) => {
    current = Math.max(0, Math.min(rows.length - 1, index));
    const at = xOf(rows[current]);
    crosshair.setAttribute('x1', at);
    crosshair.setAttribute('x2', at);
    crosshair.setAttribute('opacity', 1);

    let anchor = event;
    if (!anchor) {
      const box = crosshair.getBoundingClientRect();
      anchor = { clientX: box.left, clientY: box.top + 24 };
    }
    onIndex(current, anchor);
  };

  const leave = () => {
    crosshair.setAttribute('opacity', 0);
    hideTooltip();
    if (onLeave) onLeave();
  };

  overlay.addEventListener('pointermove', (event) => {
    const box = svg.getBoundingClientRect();
    // The svg is visually downscaled by `max-width: 100%`, so client pixels have
    // to be converted back into user units before hit-testing.
    place(nearest((event.clientX - box.left) * (width / box.width)), event);
  });
  overlay.addEventListener('pointerleave', leave);
  overlay.addEventListener('focus', () => place(current));
  overlay.addEventListener('blur', leave);
  overlay.addEventListener('keydown', (event) => {
    const step = { ArrowLeft: -1, ArrowRight: 1, PageDown: -7, PageUp: 7 }[event.key];
    let next;
    if (step !== undefined) next = current + step;
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = rows.length - 1;
    else if (event.key === 'Escape') return overlay.blur();
    else return;
    event.preventDefault(); // arrows and page keys must not scroll the page
    place(next);
  });

  return { crosshair, overlay };
}

/* ------------------------------------------------- chart: capital deployed */

// Stack order, bottom -> top: largest and steadiest sits on the baseline, so the
// bands above it don't inherit its wobble. Colour is bound to `varName`, never to
// position -- reordering this array or filtering a band away must never repaint a
// survivor, or a reader who learned "shares are orange" is misled.
const CAPITAL_BANDS = [
  { key: 'stock', label: 'Shares held', varName: '--series-2' },
  { key: 'put', label: 'Cash secured (puts)', varName: '--series-1' },
  // Shares backing covered calls that were bought before this export begins, so
  // their cost basis is not visible and the strike stands in for it.
  { key: 'call', label: 'Covered-call shares (estimated)', varName: '--series-3' },
];

// Counted in the total and carried by the tooltip, table and legend note, but
// never given a band. Long-option debit peaks at 0.4% of committed capital,
// about one pixel, so a swatch for it would point at nothing findable; spread
// collateral (netted credit-spread margin) is new and can be far larger, but
// giving it its own band risks the same weak-color-pair problem the existing
// three bands were validated all-pairs against (see docs/DESIGN.md) -- so it
// gets the same "counted, not banded" treatment rather than a fifth color.
const CAPITAL_EXCLUDED = [
  { key: 'long', label: 'Long-option debit' },
  { key: 'spread', label: 'Spread collateral' },
];

// A fourth, fundamentally different quantity: idle cash and non-wheel
// holdings -- the rest of the account. Unlike the bands above, there is no
// daily history for "total account value" the way there is for committed
// capital: it is only known on the (usually one) day a Portfolio Positions
// snapshot was taken. So it is never zero-filled between snapshots -- it
// shows up as an isolated marker on the exact dates it's known, drawn above
// the total line rather than folded into the stack, since it explicitly
// isn't part of capital deployed (see notDeployedByDate below).
const NOT_DEPLOYED_COLOR = '--text-muted';

const CAPITAL_TABLE_HEAD = [
  'Date',
  'Shares held',
  'Put collateral',
  'Short calls',
  'Long debit',
  'Spread collateral',
  { text: 'Total', title: 'Total = Shares held + Put collateral + Short calls + Long debit + Spread collateral' },
  {
    text: 'Not deployed',
    title: 'Idle cash and non-wheel holdings -- blank except on a Positions snapshot date.',
  },
  { text: 'Total value', title: 'Total committed + Not deployed -- the whole account, cash and all.' },
];

/** The last entry in a `.timeline`-shaped (ascending, sorted) array whose
 * `as_of` is on or before `asOf`, or null if none is that early yet --
 * forward-fills one account's own last-known reading across a date only
 * some OTHER account happened to snapshot. */
function lastSnapshotAtOrBefore(timeline, asOf) {
  let found = null;
  for (const snapshot of timeline) {
    if (snapshot.as_of > asOf) break;
    found = snapshot;
  }
  return found;
}

/**
 * capital_series date -> dollars sitting outside the wheel that day (total
 * account value minus that day's committed capital), for every Positions
 * snapshot date any account has, falling inside the currently-charted date
 * range. Dates before any account's first snapshot are left out of the map
 * entirely -- there is no way to know a mid-history account value without a
 * snapshot taken by then.
 *
 * A single account's `net_worth` carries its own `.timeline`; the Combined
 * view nests one per account under `.accounts` instead (see
 * `renderNetWorthTiles`'s same `.combined || netWorth` split). Each
 * account's own timeline only has a point on the dates *it* happened to
 * snapshot -- summing by exact date match would silently drop (or
 * understate) every day the accounts' Positions exports weren't taken on
 * the same day, which is the common case, not the exception. Each account's
 * last-known total_value is forward-filled across the *union* of every
 * account's snapshot dates instead, so the combined total on any given day
 * reflects every account's most recent real reading, not just whichever
 * accounts happened to snapshot that exact day.
 */
function notDeployedByDate(points, netWorth) {
  const map = new Map();
  if (!netWorth || !netWorth.available) return map;
  const pointByDate = new Map(points.map((p) => [p.date, p]));
  const timelines = (netWorth.timeline ? [netWorth.timeline] : (netWorth.accounts || []).map((a) => a.timeline || []))
    .map((timeline) => timeline.slice().sort((a, b) => (a.as_of < b.as_of ? -1 : a.as_of > b.as_of ? 1 : 0)))
    .filter((timeline) => timeline.length);

  const knownDates = new Set();
  for (const timeline of timelines) for (const snapshot of timeline) knownDates.add(snapshot.as_of);

  for (const asOf of knownDates) {
    const point = pointByDate.get(asOf);
    if (!point) continue;
    let totalValue = 0;
    let anyKnown = false;
    for (const timeline of timelines) {
      const latest = lastSnapshotAtOrBefore(timeline, asOf);
      if (latest) {
        totalValue += latest.total_value;
        anyKnown = true;
      }
    }
    if (!anyKnown) continue;
    const amount = totalValue - point.total;
    if (amount > 1e-9) map.set(asOf, amount);
  }
  return map;
}

const BAND_WASH = 0.28; // a wash, not a saturated block
const LABEL_MIN_BAND = 14; // an 11px glyph plus breathing room
const LABEL_MIN_GAP = 13; // below this two end labels touch

/** Stack the bands once, in the requested unit. */
function stackCapital(points, share) {
  // In share mode a zero-total day (a real gap day) divides to a flat zero
  // rather than NaN, so it drops cleanly to the baseline.
  const scale = points.map((p) => (share ? (p.total > 0 ? p.total / 100 : 0) : 1));
  const running = points.map(() => 0);
  return CAPITAL_BANDS.map((band) => {
    const lower = running.slice();
    points.forEach((p, i) => {
      running[i] += scale[i] ? (p[band.key] || 0) / scale[i] : 0;
    });
    return { band, lower, upper: running.slice() };
  });
}

/** Contiguous index runs where a band actually has height. */
function bandRuns(lower, upper) {
  const runs = [];
  let run = null;
  for (let i = 0; i < upper.length; i += 1) {
    if (upper[i] - lower[i] > 1e-9) {
      if (!run) runs.push((run = []));
      run.push(i);
    } else {
      run = null;
    }
  }
  return runs.filter((r) => r.length > 1);
}

/**
 * Place end labels top-down, dropping any that won't fit rather than nudging.
 * Nudging detaches a label from its band and reads as noise; a dropped value is
 * still in the legend, the tooltip and the table, so nothing is gated.
 */
function placeEndLabels(group, entries, anchorX, maxX) {
  const placed = [];
  const seen = new Set();
  for (const entry of entries) {
    // `always` marks a label anchored to a line rather than a band -- it has no
    // height to qualify with, so only collision and clipping can drop it.
    if (!entry.always && entry.bottom - entry.top < LABEL_MIN_BAND) continue;
    // When one band carries the whole total the two labels read the same; the
    // total is placed first, so the band's copy is the one that goes.
    if (seen.has(entry.text)) continue;
    const mid = entry.always ? entry.top : (entry.top + entry.bottom) / 2;
    if (placed.some((at) => Math.abs(at - mid) < LABEL_MIN_GAP)) continue;
    const node = svgEl(
      'text',
      {
        x: anchorX,
        y: mid + 3.5,
        fill: entry.strong ? 'var(--text-primary)' : 'var(--text-secondary)',
        'font-weight': 600,
      },
      entry.text
    );
    group.appendChild(node);
    // Measure for real rather than estimating from character count -- a clipped
    // label is worse than no label.
    if (anchorX + node.getComputedTextLength() > maxX) {
      node.remove();
      continue;
    }
    placed.push(mid);
    seen.add(entry.text);
  }
}

function drawCapital(points, netWorth) {
  const svg = $('chart-capital');
  const legend = $('legend-capital');
  clear(legend);
  if (!points.length) {
    clear(svg);
    svg.removeAttribute('aria-label');
    buildTable('capital-table', CAPITAL_TABLE_HEAD, []);
    return;
  }

  const share = state.capitalMode === 'share';
  // Share mode's y-axis is 0-100% of that day's *committed* capital -- there
  // is no room in that scale for a quantity measured against total account
  // value instead, so the overlay is $-mode only (see the legend note below).
  const notDeployed = share ? new Map() : notDeployedByDate(points, netWorth);
  const last = points[points.length - 1];
  const margin = { top: 12, right: 64, bottom: 30, left: 62 };
  const width = chartWidth(svg);
  const height = 288;
  const yMax = share
    ? 100
    : Math.max(...points.map((p) => p.total + (notDeployed.get(p.date) || 0))) * 1.06 || 1;

  const { group, plotWidth, plotHeight, y } = frame(svg, {
    width,
    height,
    margin,
    yMin: 0,
    yMax,
    yFormat: share ? (v) => pct(v, 0) : compactMoney,
  });

  const times = points.map((p) => parseDay(p.date).getTime());
  const [tMin, tMax] = [times[0], times[times.length - 1]];
  const x = (iso) =>
    margin.left +
    (tMax === tMin ? plotWidth / 2 : ((parseDay(iso).getTime() - tMin) / (tMax - tMin)) * plotWidth);

  const colors = CAPITAL_BANDS.map((s) => cssVar(s.varName));
  const surface = cssVar('--surface-1');
  const bands = stackCapital(points, share);

  // 1. Fills, with no stroke of any kind. A stroke on a closed band traces the
  //    baseline and both side edges too, which is an outline, not a separator.
  bands.forEach(({ lower, upper }, index) => {
    // A band present on a single isolated day has no run to cap (a cap needs two
    // points), but it still has a fill, so the two are gated separately.
    if (!upper.some((value, i) => value - lower[i] > 1e-9)) return;
    const runs = bandRuns(lower, upper);
    const top = points.map((p, i) => `${x(p.date)},${y(upper[i])}`);
    const bottom = points.map((p, i) => `${x(p.date)},${y(lower[i])}`).reverse();
    group.appendChild(
      svgEl('path', {
        d: `M${top.join('L')}L${bottom.join('L')}Z`,
        fill: colors[index],
        'fill-opacity': BAND_WASH,
        stroke: 'none',
      })
    );
    // 2. A 2px cap in the band's own hue along its top edge -- an edge, not an
    //    outline, and it keeps a band that thins to a pixel still visible.
    //    Restricted to the runs where this band has height: drawn full width,
    //    every band's cap would land on the same line wherever the bands above
    //    are empty, and the last one painted would misreport what is on top.
    if (!runs.length) return;
    group.appendChild(
      svgEl('path', {
        d: runs
          .map((run) => 'M' + run.map((i) => `${x(points[i].date)},${y(upper[i])}`).join('L'))
          .join(''),
        fill: 'none',
        stroke: colors[index],
        'stroke-width': 2,
        'stroke-linejoin': 'round',
        'stroke-linecap': 'butt',
      })
    );
  });

  // 3. The 2px surface gap, on interior boundaries only, and only across the
  //    runs where the band above actually has height -- otherwise it would carve
  //    a notch across the stack on every day that band is absent.
  for (let i = 0; i < bands.length - 1; i += 1) {
    const boundary = bands[i].upper;
    const above = bands[i + 1];
    const d = bandRuns(above.lower, above.upper)
      .map((run) => 'M' + run.map((j) => `${x(points[j].date)},${y(boundary[j])}`).join('L'))
      .join('');
    if (!d) continue;
    group.appendChild(
      svgEl('path', {
        d,
        fill: 'none',
        stroke: surface,
        'stroke-width': 2,
        'stroke-linejoin': 'round',
        'stroke-linecap': 'butt',
      })
    );
  }

  // 4. The total. In dollars this is load-bearing, not decoration: the gap
  //    between the top cap and this line is the excluded long-option debit, and
  //    on a day whose capital is entirely long debit it is the only mark drawn.
  //    In share mode it would be a flat line at ~100% carrying no level, so the
  //    axis does the job instead and the shortfall to 100% shows the same thing.
  if (!share) {
    group.appendChild(
      svgEl('path', {
        d: 'M' + points.map((p) => `${x(p.date)},${y(p.total)}`).join('L'),
        fill: 'none',
        stroke: cssVar('--text-muted'),
        'stroke-width': 1.5,
        'stroke-linejoin': 'round',
      })
    );
  }

  // 5. Not deployed, floating above the total line. Almost always an
  //    isolated single day (one Positions snapshot), so -- like an isolated
  //    band above -- it gets a fill but no top cap (a cap needs a run of at
  //    least two points); a small ringed marker at the peak makes it
  //    findable even when that fill is only a couple of pixels wide.
  if (notDeployed.size) {
    const ndColor = cssVar(NOT_DEPLOYED_COLOR);
    const ndLower = points.map((p) => p.total);
    const ndUpper = points.map((p) => p.total + (notDeployed.get(p.date) || 0));
    const top = points.map((p, i) => `${x(p.date)},${y(ndUpper[i])}`);
    const bottom = points.map((p, i) => `${x(p.date)},${y(ndLower[i])}`).reverse();
    group.appendChild(
      svgEl('path', {
        d: `M${top.join('L')}L${bottom.join('L')}Z`,
        fill: ndColor,
        'fill-opacity': BAND_WASH,
        stroke: 'none',
      })
    );
    for (const [date, amount] of notDeployed) {
      const point = points.find((p) => p.date === date);
      if (!point) continue;
      group.appendChild(
        svgEl('circle', {
          cx: x(date),
          cy: y(point.total + amount),
          r: 4,
          fill: ndColor,
          stroke: surface,
          'stroke-width': 2,
        })
      );
    }
  }

  timeAxis(group, points.map((p) => p.date), x, margin.top + plotHeight, plotWidth);

  // End labels, top of the stack downward so the total wins any collision.
  const shareOf = (key) => (last.total > 0 ? (100 * last[key]) / last.total : 0);
  const entries = [
    {
      // Anchored to the stack top. In share mode the bands reach ~100%, so the
      // dollar total still gets a home here rather than vanishing with the line.
      top: y(share ? 100 : last.total),
      bottom: y(share ? 100 : last.total),
      text: compactMoney(last.total),
      strong: true,
      always: true,
    },
    ...bands
      .map(({ band, lower, upper }, index) => ({
        top: y(upper[upper.length - 1]),
        bottom: y(lower[lower.length - 1]),
        text: share ? pct(shareOf(band.key), 0) : compactMoney(last[band.key]),
        index,
      }))
      .filter((entry) => entry.bottom > entry.top),
  ];
  placeEndLabels(group, entries, margin.left + plotWidth + 7, width - 4);

  // Crosshair layer: the reader aims at a date, never at a 2px line.
  crosshairLayer(svg, group, { margin, plotWidth, plotHeight, width }, points, (p) => x(p.date), {
    label:
      'Committed capital by day, stacked. Arrow keys step through dates, Home and End jump to either edge.',
    onIndex: (index, at) => {
      const point = points[index];
      const asShare = (key) => (point.total > 0 ? (100 * point[key]) / point.total : 0);
      const readout = (key) =>
        share
          ? `${pct(asShare(key), 1)}  ·  ${money(point[key])}`
          : money(point[key]);
      const nd = notDeployed.get(point.date);
      const rows = [
        ...CAPITAL_BANDS.map((band, i) => ({
          label: band.label,
          value: readout(band.key),
          color: colors[i],
        })),
        ...CAPITAL_EXCLUDED.map((excluded) => ({ label: excluded.label, value: readout(excluded.key) })),
        { label: 'Total committed', value: money(point.total) },
      ];
      if (nd !== undefined) {
        rows.push(
          { label: 'Not deployed (cash & other holdings)', value: money(nd), color: cssVar(NOT_DEPLOYED_COLOR) },
          { label: 'Total value', value: money(point.total + nd) }
        );
      }
      showTooltip(
        at,
        longDate(point.date),
        rows,
        formula([
          'Total committed = Σ of every band above + excluded (unbanded) figures',
          `= ${money(point.stock)} + ${money(point.put)} + ${money(point.call)} + ${money(point.long)} + ${money(point.spread)}`,
          `= ${money(point.total)}`,
          ...(nd !== undefined
            ? [
                '',
                'Total value (Positions snapshot only) = Total committed + Not deployed',
                `= ${money(point.total)} + ${money(nd)} = ${money(point.total + nd)}`,
              ]
            : []),
        ])
      );
    },
  });

  svg.setAttribute(
    'aria-label',
    `Committed capital by day, stacked: ${CAPITAL_BANDS.map((b) => b.label).join(', ')}. ` +
      `Latest total ${money(last.total)} on ${longDate(last.date)}.`
  );

  // Legend carries the current value and share for every band -- it is the
  // channel that stays complete when an end label has to be dropped.
  legend.appendChild(el('span', { class: 'legend-caption' }, `as of ${longDate(last.date)}`));
  CAPITAL_BANDS.forEach((band, i) => {
    const item = el('span');
    const swatch = el('i');
    swatch.style.background = colors[i];
    item.appendChild(swatch);
    item.appendChild(document.createTextNode(band.label));
    item.appendChild(
      el(
        'b',
        { class: 'legend-value' },
        `${compactMoney(last[band.key])} · ${pct(shareOf(band.key), 0)}`
      )
    );
    legend.appendChild(item);
  });

  const totalKey = el('span');
  const totalSwatch = el('i', { class: 'line' });
  totalSwatch.style.background = cssVar('--text-muted');
  totalKey.appendChild(totalSwatch);
  totalKey.appendChild(document.createTextNode('Total committed'));
  totalKey.appendChild(el('b', { class: 'legend-value' }, compactMoney(last.total)));
  legend.appendChild(totalKey);

  // No swatch on these, deliberately: neither is a band.
  CAPITAL_EXCLUDED.forEach((excluded) => {
    legend.appendChild(
      el(
        'span',
        { class: 'legend-note' },
        `${excluded.label} is counted in the total but not banded ` +
          `— ${money(last[excluded.key])} today. See the table.`
      )
    );
  });

  // Not deployed gets its own legend line, pinned to the most recent
  // snapshot date in view (not necessarily `last.date` -- the chart can run
  // a few days past the Positions export if the transaction history was
  // refreshed more recently). Explicitly not folded into the bands above:
  // it is the answer to "where's the rest of my total value," not a fourth
  // kind of committed capital.
  if (share) {
    legend.appendChild(
      el('span', { class: 'legend-note' }, 'Switch to $ to see funds not deployed alongside committed capital.')
    );
  } else if (notDeployed.size) {
    const latestDate = [...notDeployed.keys()].sort().pop();
    const amount = notDeployed.get(latestDate);
    const point = points.find((p) => p.date === latestDate);
    const item = el('span');
    const swatch = el('i');
    swatch.style.background = cssVar(NOT_DEPLOYED_COLOR);
    item.appendChild(swatch);
    item.appendChild(document.createTextNode('Not deployed (cash & other holdings)'));
    item.appendChild(el('b', { class: 'legend-value' }, compactMoney(amount)));
    legend.appendChild(item);
    legend.appendChild(
      el(
        'span',
        { class: 'legend-note' },
        `As of the ${longDate(latestDate)} snapshot only. + Total committed = Total value (${money(point.total + amount)}).`
      )
    );
  } else if (netWorth && netWorth.available) {
    legend.appendChild(
      el(
        'span',
        { class: 'legend-note' },
        "No Positions snapshot date falls inside the current view, so funds not deployed can't be shown here."
      )
    );
  }

  buildTable(
    'capital-table',
    CAPITAL_TABLE_HEAD,
    points.map((point) => {
      const nd = notDeployed.get(point.date);
      return [
        point.date,
        money(point.stock),
        money(point.put),
        money(point.call),
        money(point.long),
        money(point.spread),
        money(point.total),
        nd === undefined ? '—' : money(nd),
        nd === undefined ? '—' : money(point.total + nd),
      ];
    })
  );
}

/* ----------------------------------------- chart: wheel-state snapshot donut */

// Reuses this app's own already-validated categorical slots 1-3 (see
// docs/DESIGN.md: "the only subset validated all-pairs in both modes") --
// same palette, same fixed order, no new colors introduced. "Hedged / Other"
// (slot 4) is protective puts, long calls, and netted credit-spread
// collateral -- capital that isn't plain put or stock exposure. Ordering
// keeps it non-adjacent to slot 2 in the ring, the one documented weak pair.
const WHEEL_STATE_BUCKETS = [
  { key: 'puts', label: 'Cash-Secured Puts', varName: '--series-1' },
  { key: 'calls', label: 'Covered Calls', varName: '--series-2' },
  { key: 'holding', label: 'Holding Shares', varName: '--series-3' },
  { key: 'other', label: 'Hedged / Other', varName: '--series-4' },
];

/**
 * Donut: current capital, split by wheel phase, right now. Part-to-whole at
 * one moment in time -- the snapshot complement to "Capital deployed"'s time
 * series above.
 *
 * Consumes ``data.wheel_state`` verbatim rather than computing it from the
 * `cycles` payload key: `cycles` is P&L-scoped (rebuilt from whatever date
 * window the filters select), so a position opened before the window but
 * still active today would be invisible to it -- the same "capital is a
 * state, not an event count" bug docs/DESIGN.md's "Filtering" section
 * already documents and solves for the Capital deployed chart via
 * `capital_cycles`. `wheel_state_breakdown()` (wheel/metrics.py) is computed
 * server-side from that same capital-scoped set, so this chart's total
 * always matches the portfolio tile's "Capital deployed" figure regardless
 * of the active date filter.
 *
 * Splits by capital component within each cycle (see wheel_state_breakdown's
 * own docstring), so one cycle can contribute to more than one bucket --
 * e.g. shares held from an earlier assignment (Holding Shares) plus a
 * freshly-sold CSP on the same ticker (Cash-Secured Puts) are two real,
 * simultaneous commitments, not one or the other.
 *
 * Each phase's name, dollar amount and share are stated exactly once, in the
 * legend below -- the ring itself carries no per-wedge text. The previous
 * stacked-bar version printed "Label · pct%" directly under each segment
 * *and* the same label/amount/pct again in the legend right below it; a
 * wedge's own geometry already encodes its share, so that text only ever
 * repeated what the legend already said. The center callout (total capital
 * deployed) is the one number that ISN'T already any single wedge's value,
 * which is what earns it a second appearance -- the legend's own note is
 * trimmed to the active-cycle count so it doesn't restate that dollar figure
 * a third time.
 */
function drawWheelState(wheelState) {
  const svg = $('chart-wheel-state');
  const legend = $('legend-wheel-state');
  clear(legend);

  const rawBuckets = (wheelState && wheelState.buckets) || {};
  const activeCycles = (wheelState && wheelState.active_cycles) || 0;
  const totals = {};
  const counts = {};
  const tickersByBucket = {};
  for (const bucket of WHEEL_STATE_BUCKETS) {
    const entry = rawBuckets[bucket.key] || { amount: 0, cycles: 0, tickers: [] };
    totals[bucket.key] = entry.amount || 0;
    counts[bucket.key] = entry.cycles || 0;
    tickersByBucket[bucket.key] = entry.tickers || [];
  }

  const grandTotal = totals.puts + totals.calls + totals.holding + totals.other;
  const tableHead = ['Phase', 'Capital', 'Share of total', 'Cycles with capital here', 'Tickers'];

  if (!activeCycles || grandTotal <= 1e-9) {
    clear(svg);
    svg.removeAttribute('aria-label');
    buildTable('wheel-state-table', tableHead, []);
    legend.appendChild(el('span', { class: 'legend-note' }, 'No active capital deployed in this slice.'));
    return;
  }

  const buckets = WHEEL_STATE_BUCKETS.filter((bucket) => totals[bucket.key] > 1e-9);

  const width = chartWidth(svg);
  const rOuter = 100;
  const rInner = 58;
  const height = rOuter * 2 + 24;
  const cx = width / 2;
  const cy = height / 2;
  svg.setAttribute('width', width);
  svg.setAttribute('height', height);
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  clear(svg);
  const group = svgEl('g');
  svg.appendChild(group);

  const surface = cssVar('--surface-1');
  const arcPoint = (angle, r) => [cx + r * Math.cos(angle), cy + r * Math.sin(angle)];
  const wedgePath = (start, end) => {
    const [x1, y1] = arcPoint(start, rOuter);
    const [x2, y2] = arcPoint(end, rOuter);
    const [x3, y3] = arcPoint(end, rInner);
    const [x4, y4] = arcPoint(start, rInner);
    const largeArc = end - start > Math.PI ? 1 : 0;
    return (
      `M${x1},${y1} A${rOuter},${rOuter} 0 ${largeArc} 1 ${x2},${y2} ` +
      `L${x3},${y3} A${rInner},${rInner} 0 ${largeArc} 0 ${x4},${y4} Z`
    );
  };

  const tableRows = [];
  let angle = -Math.PI / 2; // 12 o'clock, sweeping clockwise
  buckets.forEach((bucket) => {
    const amount = totals[bucket.key];
    const share = amount / grandTotal;
    const color = cssVar(bucket.varName);

    let path;
    if (buckets.length === 1) {
      // A full ring can't be described as a single arc -- SVG's arc command
      // degenerates when the start and end point coincide. Two concentric
      // circles with an even-odd fill draw the same annulus without that
      // edge case, for the (rare) one-phase-only slice.
      path = svgEl('path', {
        class: 'mark',
        'fill-rule': 'evenodd',
        d:
          `M${cx - rOuter},${cy} A${rOuter},${rOuter} 0 1 1 ${cx + rOuter},${cy} ` +
          `A${rOuter},${rOuter} 0 1 1 ${cx - rOuter},${cy} Z ` +
          `M${cx - rInner},${cy} A${rInner},${rInner} 0 1 0 ${cx + rInner},${cy} ` +
          `A${rInner},${rInner} 0 1 0 ${cx - rInner},${cy} Z`,
        fill: color,
      });
    } else {
      const end = angle + share * Math.PI * 2;
      path = svgEl('path', {
        class: 'mark',
        d: wedgePath(angle, end),
        fill: color,
        stroke: surface,
        'stroke-width': 2,
        'stroke-linejoin': 'round',
      });
      angle = end;
    }
    group.appendChild(path);

    attachTip(
      path,
      bucket.label,
      [
        { label: 'Capital', value: money(amount) },
        { label: 'Share of total', value: pct(share * 100, 1) },
        { label: 'Cycles with capital here', value: String(counts[bucket.key]) },
        { label: 'Tickers', value: [...tickersByBucket[bucket.key]].sort().join(', ') || '—' },
      ],
      formula([
        `${bucket.label} = this capital component, summed across active cycles`,
        '  (one cycle can count in more than one phase -- e.g. holding shares',
        '  while also running a fresh cash-secured put)',
        '',
        `= ${money(amount)} of ${money(grandTotal)} total = ${pct(share * 100, 1)}`,
      ])
    );

    tableRows.push([
      bucket.label,
      money(amount),
      pct(share * 100, 1),
      counts[bucket.key],
      [...tickersByBucket[bucket.key]].sort().join(', ') || '—',
    ]);
  });

  // Center callout: the only figure on the chart that isn't already one
  // wedge's own value.
  group.appendChild(
    svgEl(
      'text',
      {
        x: cx,
        y: cy - 6,
        'text-anchor': 'middle',
        style: 'fill: var(--text-primary); font-weight: 700; font-size: 18px;',
      },
      compactMoney(grandTotal)
    )
  );
  group.appendChild(
    svgEl('text', { x: cx, y: cy + 14, 'text-anchor': 'middle', class: 'tick-label' }, 'deployed now')
  );

  svg.setAttribute(
    'aria-label',
    `Current wheel capital by phase: ${buckets
      .map((bucket) => `${bucket.label} ${pct((totals[bucket.key] / grandTotal) * 100, 0)}`)
      .join(', ')}. Total ${money(grandTotal)} across ${activeCycles} active cycle(s).`
  );

  buckets.forEach((bucket) => {
    const item = el('span');
    const swatch = el('i');
    swatch.style.background = cssVar(bucket.varName);
    item.appendChild(swatch);
    item.appendChild(document.createTextNode(bucket.label));
    item.appendChild(
      el(
        'b',
        { class: 'legend-value' },
        `${money(totals[bucket.key])} · ${pct((totals[bucket.key] / grandTotal) * 100, 0)}`
      )
    );
    legend.appendChild(item);
  });
  legend.appendChild(
    el('span', { class: 'legend-note' }, `Across ${activeCycles} active cycle(s); the ring's center shows the total.`)
  );

  buildTable('wheel-state-table', tableHead, tableRows);
}

/* --------------------------------------------- chart: cumulative P/L lines */

function drawPnl(series) {
  const svg = $('chart-pnl');
  const legend = $('legend-pnl');
  clear(legend);
  if (!series.length) {
    clear(svg);
    return;
  }

  const lines = [
    { key: 'cum_option_pl', label: 'Premium collected (net)', varName: '--series-1' },
    { key: 'cum_stock_pl', label: 'Stock P/L', varName: '--series-3' },
    { key: 'cum_total_pl', label: 'Full wheel P/L', varName: '--series-2' },
  ];
  const colors = lines.map((line) => cssVar(line.varName));

  const values = series.flatMap((point) => lines.map((line) => point[line.key]));
  const margin = { top: 14, right: 58, bottom: 30, left: 62 };
  const width = chartWidth(svg);
  const height = 288;
  const yMin = Math.min(0, ...values) * 1.08;
  const yMax = Math.max(...values) * 1.08 || 1;

  const { group, plotWidth, plotHeight, y } = frame(svg, { width, height, margin, yMin, yMax });

  const times = series.map((point) => parseDay(point.date).getTime());
  const [tMin, tMax] = [times[0], times[times.length - 1]];
  const x = (iso) =>
    margin.left +
    (tMax === tMin ? plotWidth / 2 : ((parseDay(iso).getTime() - tMin) / (tMax - tMin)) * plotWidth);

  if (yMin < 0) {
    group.appendChild(
      svgEl('line', {
        class: 'axis-line',
        x1: margin.left,
        x2: margin.left + plotWidth,
        y1: y(0),
        y2: y(0),
      })
    );
  }

  lines.forEach((line, index) => {
    const path = series.map((point) => `${x(point.date)},${y(point[line.key])}`).join('L');
    group.appendChild(
      svgEl('path', {
        d: 'M' + path,
        fill: 'none',
        stroke: colors[index],
        'stroke-width': 2,
        'stroke-linejoin': 'round',
        'stroke-linecap': 'round',
      })
    );
    // Direct-label the endpoint only -- never a number on every point.
    const last = series[series.length - 1];
    group.appendChild(
      svgEl(
        'text',
        {
          x: x(last.date) + 7,
          y: y(last[line.key]) + 3.5,
          fill: 'var(--text-secondary)',
          'font-weight': 600,
        },
        compactMoney(last[line.key])
      )
    );
  });

  timeAxis(group, series.map((point) => point.date), x, margin.top + plotHeight, plotWidth);

  // Follower dots are appended before the crosshair layer so its overlay,
  // which must receive the pointer, stays on top.
  const dots = lines.map((_, index) =>
    group.appendChild(
      svgEl('circle', {
        r: 4,
        fill: colors[index],
        stroke: cssVar('--surface-1'),
        'stroke-width': 2,
        opacity: 0,
      })
    )
  );

  crosshairLayer(svg, group, { margin, plotWidth, plotHeight, width }, series, (p) => x(p.date), {
    label:
      'Cumulative net option premium, stock P/L, and full wheel P/L by day. Arrow keys step through dates.',
    onIndex: (index, at) => {
      const point = series[index];
      dots.forEach((dot, i) => {
        dot.setAttribute('cx', x(point.date));
        dot.setAttribute('cy', y(point[lines[i].key]));
        dot.setAttribute('opacity', 1);
      });
      showTooltip(
        at,
        longDate(point.date),
        [
          ...lines.map((line, i) => ({
            label: line.label,
            value: money(point[line.key], { cents: true }),
            color: colors[i],
          })),
          { label: 'Full wheel P/L that day', value: money(point.total_pl, { cents: true }) },
        ],
        formula([
          "That day's Full wheel P/L = Premium (net) that day + Stock P/L that day",
          `= ${money(point.option_pl, { cents: true })} + ${money(point.stock_pl, { cents: true })}`,
          `= ${money(point.total_pl, { cents: true })}`,
          '',
          'Cumulative Full wheel P/L = Σ premium to date + Σ stock P/L to date',
          `= ${money(point.cum_option_pl, { cents: true })} + ${money(point.cum_stock_pl, { cents: true })}`,
          `= ${money(point.cum_total_pl, { cents: true })}`,
        ])
      );
    },
    onLeave: () => dots.forEach((dot) => dot.setAttribute('opacity', 0)),
  });

  lines.forEach((line, i) => {
    const item = el('span');
    const swatch = el('i', { class: 'line' });
    swatch.style.background = colors[i];
    item.appendChild(swatch);
    item.appendChild(document.createTextNode(line.label));
    legend.appendChild(item);
  });

  buildTable(
    'pnl-table',
    ['Date', 'Premium that day', 'Stock P/L that day', 'Cum. premium', 'Cum. stock P/L', 'Cum. full wheel P/L'],
    series.map((point) => [
      point.date,
      money(point.option_pl, { cents: true }),
      money(point.stock_pl, { cents: true }),
      money(point.cum_option_pl, { cents: true }),
      money(point.cum_stock_pl, { cents: true }),
      money(point.cum_total_pl, { cents: true }),
    ])
  );
}

/* ----------------------------------------------- chart: monthly cash flow */

const CASHFLOW_TABLE_HEAD = [
  'Month',
  'Gross credits',
  'Gross debits',
  'Fees',
  {
    text: 'Net cash flow',
    title: formula([
      'Net cash flow = Gross credits - Gross debits - Fees',
      'Dated to when cash actually settles -- premium sold, dividends',
      '  received, a roll\'s debit -- not to when the underlying position',
      '  finally closes.',
    ]),
  },
  {
    text: 'Wheel realized P/L',
    title: formula([
      'This month\'s share of realized wheel P/L (profit/loss): option P/L',
      '  plus stock P/L, dated to when a leg actually closes or a share lot',
      '  is sold -- never to when the premium was originally collected.',
    ]),
  },
  'Avg collateral',
  'Yield %',
];

const monthLabel = (period) => {
  const [year, month] = period.split('-').map(Number);
  return new Date(year, month - 1, 1).toLocaleDateString('en-US', { month: 'short', year: 'numeric' });
};

/**
 * `pnl_series` (daily, dated to when a leg closes or a share lot is
 * disposed -- see `wheel.metrics.realized_pl_series`) bucketed into the same
 * calendar months `rows` (from `wheel.cashflow.monthly_cashflow_series`)
 * already walks, so the two series line up 1:1 even though they come from
 * different backends and different date semantics (settlement date vs.
 * close date -- see the "cash flow vs. wheel P/L" explanation this chart's
 * second bar exists to make visible).
 */
function monthlyWheelPl(rows, pnlSeries) {
  const byMonth = new Map();
  for (const point of pnlSeries || []) {
    const period = point.date.slice(0, 7);
    const slot = byMonth.get(period) || { option_pl: 0, stock_pl: 0, total_pl: 0 };
    slot.option_pl += point.option_pl;
    slot.stock_pl += point.stock_pl;
    slot.total_pl += point.total_pl;
    byMonth.set(period, slot);
  }
  return rows.map((row) => byMonth.get(row.period) || { option_pl: 0, stock_pl: 0, total_pl: 0 });
}

/**
 * Realized wheel cash flow by calendar month: two grouped bars per month --
 * net cash flow (blue above zero for a credit month, red below for a debit
 * one) beside realized wheel P/L (a fixed color, since it needs its own
 * identity distinct from the credit/debit coloring, and can itself be
 * positive or negative). Reuses `frame()` for the y-scale/gridlines, same as
 * every other dollar chart, but the x-axis is categorical (one slot per
 * month) rather than the continuous date scale `timeAxis()` assumes.
 *
 * The two bars answer the question this chart exists to make visible: cash
 * flow books a credit the moment premium is *sold* (STO), wheel P/L only
 * once the leg actually *closes* -- so a month that is heavy on new short
 * premium but light on closes shows a tall cash-flow bar next to a short
 * wheel-P/L one, and that gap is the point, not a bug. See the dashboard's
 * explanation for the full mechanics (also in docs/DESIGN.md).
 *
 * Two reference lines, averaged over every month `rows` shows (the same
 * selected-range summary `renderCashFlowTiles`'s "Avg monthly income" tile
 * uses -- see `wheel.cashflow.range_summary`): a dashed red line at the
 * range's avg monthly income, and a dotted orange line at its avg wheel
 * realized P/L. Both are plain averages, not clamped to zero, so a losing
 * range draws its line below the zero axis exactly like a losing month's bar
 * would -- no separate handling for a negative average anywhere here.
 */
function drawCashFlow(rows, trailing, pnlSeries) {
  const svg = $('chart-cashflow');
  const legend = $('legend-cashflow');
  clear(legend);
  if (!rows.length) {
    clear(svg);
    svg.removeAttribute('aria-label');
    buildTable('cashflow-table', CASHFLOW_TABLE_HEAD, []);
    return;
  }

  const wheelPl = monthlyWheelPl(rows, pnlSeries);
  const avgIncome = trailing && trailing.avg_monthly_income;
  // wheelPl has one entry per row (monthlyWheelPl walks `rows`), so this is a
  // plain average over the whole displayed range -- the same scope `avgIncome`
  // (range_summary's avg_monthly_income) already uses, so the two reference
  // lines are directly comparable. Averaging, not summing, so a negative
  // month pulls the line below zero exactly as it should -- no special-casing
  // for sign anywhere below.
  const avgWheelPl = wheelPl.length ? wheelPl.reduce((sum, w) => sum + w.total_pl / wheelPl.length, 0) : null;
  const margin = { top: 14, right: 20, bottom: 30, left: 62 };
  const width = chartWidth(svg);
  const height = 260;
  const values = [...rows.map((row) => row.net_cash_flow), ...wheelPl.map((w) => w.total_pl)];
  if (avgIncome !== null && avgIncome !== undefined) values.push(avgIncome);
  if (avgWheelPl !== null && avgWheelPl !== undefined) values.push(avgWheelPl);
  const yMin = Math.min(0, ...values) * 1.15;
  const yMax = Math.max(0, ...values, 1) * 1.15;

  const { group, plotWidth, plotHeight, y } = frame(svg, { width, height, margin, yMin, yMax });

  const slot = plotWidth / rows.length;
  const groupGap = 3;
  const groupWidth = Math.max(10, Math.min(46, slot * 0.62));
  const barWidth = Math.max(3, (groupWidth - groupGap) / 2);
  const positive = cssVar('--pos');
  const negative = cssVar('--neg');
  const wheelColor = cssVar('--series-2'); // same color drawPnl() uses for "Full wheel P/L"
  const zeroY = y(0);

  group.appendChild(
    svgEl('line', { class: 'axis-line', x1: margin.left, x2: margin.left + plotWidth, y1: zeroY, y2: zeroY })
  );

  rows.forEach((row, index) => {
    const cx = margin.left + slot * (index + 0.5);
    const cashX = cx - groupWidth / 2;
    const wheelX = cashX + barWidth + groupGap;

    const value = row.net_cash_flow;
    const barTop = value >= 0 ? y(value) : zeroY;
    const barHeight = Math.max(Math.abs(y(value) - zeroY), value === 0 ? 0 : 1.5);
    const rect = svgEl('rect', {
      class: 'mark',
      x: cashX,
      y: barTop,
      width: barWidth,
      height: barHeight,
      rx: 3,
      fill: value < 0 ? negative : positive,
    });
    group.appendChild(rect);

    attachTip(
      rect,
      monthLabel(row.period),
      [
        { label: 'Gross credits', value: money(row.gross_credits, { cents: true }) },
        { label: 'Gross debits', value: money(row.gross_debits, { cents: true }) },
        { label: 'Fees', value: money(row.fees, { cents: true }) },
        { label: 'Net cash flow', value: money(row.net_cash_flow, { cents: true }) },
        { label: 'Avg collateral', value: money(row.avg_collateral) },
        { label: 'Monthly yield', value: pct(row.monthly_yield_pct, 2) },
      ],
      formula([
        'Net cash flow = Gross credits - Gross debits - Fees',
        `= ${money(row.gross_credits, { cents: true })} - ${money(row.gross_debits, { cents: true })} - ${money(row.fees, { cents: true })}`,
        `= ${money(row.net_cash_flow, { cents: true })}`,
        '',
        'Monthly yield % = Net cash flow ÷ Avg allocated collateral × 100',
        `= ${money(row.net_cash_flow, { cents: true })} ÷ ${money(row.avg_collateral)} × 100`,
        `= ${row.monthly_yield_pct === null ? 'N/A -- no collateral committed this month' : pct(row.monthly_yield_pct, 2)}`,
      ])
    );

    const wp = wheelPl[index];
    const wheelTop = wp.total_pl >= 0 ? y(wp.total_pl) : zeroY;
    const wheelHeight = Math.max(Math.abs(y(wp.total_pl) - zeroY), wp.total_pl === 0 ? 0 : 1.5);
    const wheelRect = svgEl('rect', {
      class: 'mark',
      x: wheelX,
      y: wheelTop,
      width: barWidth,
      height: wheelHeight,
      rx: 3,
      fill: wheelColor,
    });
    group.appendChild(wheelRect);

    attachTip(
      wheelRect,
      `${monthLabel(row.period)} — wheel realized P/L`,
      [
        { label: 'Premium collected (net)', value: money(wp.option_pl, { cents: true }) },
        { label: 'Stock P/L', value: money(wp.stock_pl, { cents: true }) },
        { label: 'Wheel realized P/L', value: money(wp.total_pl, { cents: true }) },
        { label: 'Net cash flow (this month)', value: money(row.net_cash_flow, { cents: true }) },
      ],
      formula([
        'Wheel realized P/L = Premium collected (net) + Stock P/L',
        '  Dated to when each leg closes or a share lot is sold --',
        '  never to when premium was sold, unlike Net cash flow.',
        '',
        `= ${money(wp.option_pl, { cents: true })} + ${money(wp.stock_pl, { cents: true })}`,
        `= ${money(wp.total_pl, { cents: true })}`,
      ])
    );
  });

  if (avgIncome !== null && avgIncome !== undefined) {
    const avgY = y(avgIncome);
    const avgColor = cssVar('--neg');
    const line = svgEl('line', {
      class: 'avg-income-line',
      x1: margin.left,
      x2: margin.left + plotWidth,
      y1: avgY,
      y2: avgY,
      stroke: avgColor,
      'stroke-width': 1.5,
      'stroke-dasharray': '6,4',
    });
    group.appendChild(line);
    attachTip(
      line,
      'Avg monthly income (selected range)',
      [{ label: 'Avg monthly income', value: money(avgIncome, { cents: true }) }],
      formula([
        'Avg monthly income = Cash flow over the selected range ÷ months shown',
        `= ${money(trailing.cash_flow, { cents: true })} ÷ ${trailing.months_counted}`,
        `= ${money(avgIncome, { cents: true })}`,
      ])
    );
    // Clamp so the label never clips past the plot's top/bottom edge when
    // the line sits close to either one.
    const labelY = Math.min(Math.max(avgY - 4, margin.top + 10), margin.top + plotHeight - 4);
    group.appendChild(
      svgEl(
        'text',
        {
          class: 'tick-label',
          x: margin.left + plotWidth - 4,
          y: labelY,
          'text-anchor': 'end',
          fill: avgColor,
        },
        `Avg monthly income · ${money(avgIncome, { cents: true })}`
      )
    );
  }

  if (avgWheelPl !== null && avgWheelPl !== undefined) {
    // A dotted line, not dashed, and anchored on the left (avg income's
    // label sits on the right) so the two reference lines stay legible even
    // when they land close together -- and a plain linear y-scale means a
    // negative average needs no special handling: y(avgWheelPl) already
    // falls below the zero line exactly like a negative bar would.
    const wheelAvgY = y(avgWheelPl);
    const line = svgEl('line', {
      class: 'avg-wheel-pl-line',
      x1: margin.left,
      x2: margin.left + plotWidth,
      y1: wheelAvgY,
      y2: wheelAvgY,
      stroke: wheelColor,
      'stroke-width': 1.5,
      'stroke-dasharray': '2,3',
    });
    group.appendChild(line);
    attachTip(
      line,
      'Avg wheel realized P/L (selected range)',
      [{ label: 'Avg wheel realized P/L', value: money(avgWheelPl, { cents: true }) }],
      formula([
        'Avg wheel realized P/L = Σ(wheel realized P/L, every month shown) ÷ months shown',
        `= mean of the ${wheelPl.length} month(s) shown in the table`,
        `= ${money(avgWheelPl, { cents: true })}`,
        '',
        avgWheelPl < 0
          ? 'Negative: this range closed at a net loss on the wheel side.'
          : 'Positive: this range closed at a net gain on the wheel side.',
      ])
    );
    const labelY = Math.min(Math.max(wheelAvgY - 4, margin.top + 10), margin.top + plotHeight - 4);
    group.appendChild(
      svgEl(
        'text',
        {
          class: 'tick-label',
          x: margin.left + 4,
          y: labelY,
          'text-anchor': 'start',
          fill: wheelColor,
        },
        `Avg wheel P/L · ${money(avgWheelPl, { cents: true })}`
      )
    );
  }

  group.appendChild(
    svgEl('line', {
      class: 'axis-line',
      x1: margin.left,
      x2: margin.left + plotWidth,
      y1: margin.top + plotHeight,
      y2: margin.top + plotHeight,
    })
  );
  const maxLabels = Math.max(2, Math.floor(plotWidth / 60));
  const step = Math.max(1, Math.ceil(rows.length / maxLabels));
  rows.forEach((row, index) => {
    if (index % step !== 0 && index !== rows.length - 1) return;
    group.appendChild(
      svgEl(
        'text',
        {
          class: 'tick-label',
          x: margin.left + slot * (index + 0.5),
          y: margin.top + plotHeight + 16,
          'text-anchor': 'middle',
        },
        monthLabel(row.period)
      )
    );
  });

  svg.setAttribute(
    'aria-label',
    `Net monthly cash flow versus realized wheel P/L, ${rows.length} month(s) from ` +
      `${monthLabel(rows[0].period)} to ${monthLabel(rows[rows.length - 1].period)}. ` +
      'Hover or focus a bar for its breakdown.' +
      (avgIncome !== null && avgIncome !== undefined
        ? ` Dashed red line marks the avg monthly income of ${money(avgIncome, { cents: true })} over this range.`
        : '') +
      (avgWheelPl !== null && avgWheelPl !== undefined
        ? ` Dotted orange line marks the avg wheel realized P/L of ${money(avgWheelPl, { cents: true })} over this range.`
        : '')
  );

  const cashSwatchItem = el('span');
  const cashSwatch = el('i');
  cashSwatch.style.background = `linear-gradient(90deg, ${positive} 50%, ${negative} 50%)`;
  cashSwatchItem.appendChild(cashSwatch);
  cashSwatchItem.appendChild(document.createTextNode('Net cash flow (blue = credit, red = debit)'));
  legend.appendChild(cashSwatchItem);

  const wheelSwatchItem = el('span');
  const wheelSwatch = el('i');
  wheelSwatch.style.background = wheelColor;
  wheelSwatchItem.appendChild(wheelSwatch);
  wheelSwatchItem.appendChild(document.createTextNode('Wheel realized P/L (dated to when a leg closes)'));
  legend.appendChild(wheelSwatchItem);

  legend.appendChild(
    el(
      'span',
      { class: 'legend-note' },
      'Cash flow books a credit the moment premium is sold; wheel P/L only once the position closes — hover either bar for its breakdown.'
    )
  );
  if (avgIncome !== null && avgIncome !== undefined) {
    legend.appendChild(
      el(
        'span',
        { class: 'legend-note' },
        `Dashed red line = avg monthly income over this range (${money(avgIncome, { cents: true })}). Hover it for the formula.`
      )
    );
  }
  if (avgWheelPl !== null && avgWheelPl !== undefined) {
    legend.appendChild(
      el(
        'span',
        { class: 'legend-note' },
        `Dotted orange line = avg wheel realized P/L over this range (${money(avgWheelPl, { cents: true })}). Hover it for the formula.`
      )
    );
  }

  buildTable(
    'cashflow-table',
    CASHFLOW_TABLE_HEAD,
    rows.map((row, index) => [
      monthLabel(row.period),
      money(row.gross_credits, { cents: true }),
      money(row.gross_debits, { cents: true }),
      money(row.fees, { cents: true }),
      money(row.net_cash_flow, { cents: true }),
      money(wheelPl[index].total_pl, { cents: true }),
      money(row.avg_collateral),
      pct(row.monthly_yield_pct, 2),
    ])
  );
}

/* --------------------------------------- chart: cash-flow vs. wheel P/L gap */

const GAP_TABLE_HEAD = ['Month', 'Net cash flow', 'Wheel realized P/L', 'Monthly gap', 'Cumulative gap'];

/**
 * The running difference between cash actually collected (Monthly cash flow,
 * dated to settlement) and wheel P/L actually realized (dated to close) --
 * see that chart's own hint for why the two diverge. Bars are the monthly
 * gap (diverging, same categorical months as the cash-flow chart); the line
 * overlaid on the same x positions is its running total, the headline: a
 * sustained rise means collected premium is piling up in still-open
 * positions faster than it's being realized -- not necessarily a problem,
 * but the thing worth watching for.
 */
function drawCashFlowGap(rows, pnlSeries) {
  const svg = $('chart-gap');
  const legend = $('legend-gap');
  clear(legend);
  if (!rows.length) {
    clear(svg);
    svg.removeAttribute('aria-label');
    buildTable('gap-table', GAP_TABLE_HEAD, []);
    return;
  }

  const wheelPl = monthlyWheelPl(rows, pnlSeries);
  const gaps = rows.map((row, index) => row.net_cash_flow - wheelPl[index].total_pl);
  const cumGaps = [];
  let running = 0;
  for (const gap of gaps) {
    running += gap;
    cumGaps.push(running);
  }
  const lastIndex = cumGaps.length - 1;

  const margin = { top: 14, right: 20, bottom: 30, left: 62 };
  const width = chartWidth(svg);
  const height = 260;
  const values = [...gaps, ...cumGaps];
  const yMin = Math.min(0, ...values) * 1.15;
  const yMax = Math.max(0, ...values, 1) * 1.15;

  const { group, plotWidth, plotHeight, y } = frame(svg, { width, height, margin, yMin, yMax });

  const slot = plotWidth / rows.length;
  const barWidth = Math.max(3, Math.min(22, slot * 0.4));
  const positive = cssVar('--pos');
  const negative = cssVar('--neg');
  const lineColor = cssVar('--series-2'); // same "wheel" identity color the cash-flow chart's own bars use
  const zeroY = y(0);
  const centers = rows.map((_, index) => margin.left + slot * (index + 0.5));

  group.appendChild(
    svgEl('line', { class: 'axis-line', x1: margin.left, x2: margin.left + plotWidth, y1: zeroY, y2: zeroY })
  );

  rows.forEach((row, index) => {
    const cx = centers[index];
    const value = gaps[index];
    const barTop = value >= 0 ? y(value) : zeroY;
    const barHeight = Math.max(Math.abs(y(value) - zeroY), value === 0 ? 0 : 1.5);
    const rect = svgEl('rect', {
      class: 'mark',
      x: cx - barWidth / 2,
      y: barTop,
      width: barWidth,
      height: barHeight,
      rx: 3,
      fill: value < 0 ? negative : positive,
    });
    group.appendChild(rect);

    attachTip(
      rect,
      monthLabel(row.period),
      [
        { label: 'Net cash flow', value: money(row.net_cash_flow, { cents: true }) },
        { label: 'Wheel realized P/L', value: money(wheelPl[index].total_pl, { cents: true }) },
        { label: 'Monthly gap', value: money(value, { cents: true }) },
        { label: 'Cumulative gap', value: money(cumGaps[index], { cents: true }) },
      ],
      formula([
        'Monthly gap = Net cash flow - Wheel realized P/L',
        `= ${money(row.net_cash_flow, { cents: true })} - ${money(wheelPl[index].total_pl, { cents: true })}`,
        `= ${money(value, { cents: true })}`,
        '',
        "Cumulative gap = running total of every month's gap so far",
        `= ${money(cumGaps[index], { cents: true })}`,
      ])
    );
  });

  const linePath = centers.map((cx, index) => `${cx},${y(cumGaps[index])}`).join('L');
  group.appendChild(
    svgEl('path', {
      d: 'M' + linePath,
      fill: 'none',
      stroke: lineColor,
      'stroke-width': 2,
      'stroke-linejoin': 'round',
      'stroke-linecap': 'round',
    })
  );
  group.appendChild(
    svgEl('circle', {
      cx: centers[lastIndex],
      cy: y(cumGaps[lastIndex]),
      r: 4,
      fill: lineColor,
      stroke: cssVar('--surface-1'),
      'stroke-width': 2,
    })
  );
  group.appendChild(
    svgEl(
      'text',
      { x: centers[lastIndex] + 7, y: y(cumGaps[lastIndex]) + 3.5, fill: 'var(--text-secondary)', 'font-weight': 600 },
      compactMoney(cumGaps[lastIndex])
    )
  );

  group.appendChild(
    svgEl('line', {
      class: 'axis-line',
      x1: margin.left,
      x2: margin.left + plotWidth,
      y1: margin.top + plotHeight,
      y2: margin.top + plotHeight,
    })
  );
  const maxLabels = Math.max(2, Math.floor(plotWidth / 60));
  const step = Math.max(1, Math.ceil(rows.length / maxLabels));
  rows.forEach((row, index) => {
    if (index % step !== 0 && index !== rows.length - 1) return;
    group.appendChild(
      svgEl(
        'text',
        { class: 'tick-label', x: centers[index], y: margin.top + plotHeight + 16, 'text-anchor': 'middle' },
        monthLabel(row.period)
      )
    );
  });

  svg.setAttribute(
    'aria-label',
    `Cash flow versus wheel P/L gap, ${rows.length} month(s). Cumulative gap now ` +
      `${money(cumGaps[lastIndex], { cents: true })}. Hover a bar for its month, or the line's end for the running total.`
  );

  const barSwatchItem = el('span');
  const barSwatch = el('i');
  barSwatch.style.background = `linear-gradient(90deg, ${positive} 50%, ${negative} 50%)`;
  barSwatchItem.appendChild(barSwatch);
  barSwatchItem.appendChild(
    document.createTextNode('Monthly gap (blue = cash flow ahead that month, red = wheel P/L ahead)')
  );
  legend.appendChild(barSwatchItem);

  const lineSwatchItem = el('span');
  const lineSwatch = el('i', { class: 'line' });
  lineSwatch.style.background = lineColor;
  lineSwatchItem.appendChild(lineSwatch);
  lineSwatchItem.appendChild(document.createTextNode('Cumulative gap (running total)'));
  lineSwatchItem.appendChild(el('b', { class: 'legend-value' }, money(cumGaps[lastIndex], { cents: true })));
  legend.appendChild(lineSwatchItem);

  buildTable(
    'gap-table',
    GAP_TABLE_HEAD,
    rows.map((row, index) => [
      monthLabel(row.period),
      money(row.net_cash_flow, { cents: true }),
      money(wheelPl[index].total_pl, { cents: true }),
      money(gaps[index], { cents: true }),
      money(cumGaps[index], { cents: true }),
    ])
  );
}

function renderCashFlowTiles(trailing, rows, pnlSeries) {
  const host = $('cashflow-tiles');
  clear(host);

  // Wheel P/L over the same range range_summary() covers -- every month in
  // `rows`, not a fixed lookback -- so the gap tile is directly comparable.
  // See drawCashFlowGap for the same computation applied per month.
  const wheelPl = monthlyWheelPl(rows, pnlSeries);
  const rangeWheelPl = wheelPl.reduce((sum, w) => sum + w.total_pl, 0);
  const gap = trailing.cash_flow - rangeWheelPl;

  const tiles = [
    {
      label: 'Avg monthly income',
      value: trailing.avg_monthly_income === null ? '—' : money(trailing.avg_monthly_income, { cents: true }),
      foot: `over the selected range (${trailing.months_counted} month(s))`,
      tone: (trailing.avg_monthly_income ?? 0) >= 0 ? 'pos' : 'neg',
      formula: formula([
        'Avg monthly income = Cash flow over the selected range ÷ months shown',
        `= ${money(trailing.cash_flow, { cents: true })} ÷ ${trailing.months_counted}`,
        `= ${trailing.avg_monthly_income === null ? 'N/A' : money(trailing.avg_monthly_income, { cents: true })}`,
      ]),
    },
    {
      label: 'Annualized cash-on-cash return',
      value: pct(trailing.annualized_cash_on_cash_return_pct, 2),
      foot: `vs. ${money(trailing.avg_collateral)} avg collateral over this range`,
      tone: (trailing.annualized_cash_on_cash_return_pct ?? 0) >= 0 ? 'pos' : 'neg',
      formula:
        trailing.annualized_cash_on_cash_return_pct === null
          ? formula([
              'Annualized cash-on-cash return =',
              '  (Avg monthly income × 12) ÷ Avg collateral × 100',
              '',
              'N/A -- no collateral committed in the selected range.',
            ])
          : formula([
              'Annualized cash-on-cash return =',
              '  (Avg monthly income × 12) ÷ Avg collateral × 100',
              '  Avg collateral is a time-weighted average over every day in this range.',
              '',
              `= (${money(trailing.avg_monthly_income, { cents: true })} × 12) ÷ ${money(trailing.avg_collateral)} × 100`,
              `= ${pct(trailing.annualized_cash_on_cash_return_pct, 2)}`,
              '',
              'Counts all cash actually collected -- including premium on still-open',
              '  legs, and dividends -- not just profit/loss on legs already closed.',
            ]),
    },
    {
      // No `tone`, deliberately: this is a timing gap, not a verdict -- a
      // large gap isn't inherently good or bad (see the chart below for why),
      // so it shouldn't be colored red/green like a win/loss figure would be.
      label: 'Cash flow vs. wheel P/L gap',
      value: money(gap, { cents: true }),
      foot: `${money(trailing.cash_flow, { cents: true })} cash flow - ${money(rangeWheelPl, { cents: true })} wheel P/L`,
      formula: formula([
        'Gap = Cash flow - Wheel realized P/L',
        `  (both over the selected range, ${trailing.months_counted} month(s))`,
        `= ${money(trailing.cash_flow, { cents: true })} - ${money(rangeWheelPl, { cents: true })}`,
        `= ${money(gap, { cents: true })}`,
        '',
        'Positive: premium collected is running ahead of what has been realized --',
        '  usually open positions not yet closed, or dividends (never in wheel P/L).',
        'Negative: realized wheel P/L is running ahead of newly collected premium.',
      ]),
    },
  ];

  for (const tile of tiles) {
    const node = el('div', { class: 'tile' });
    node.appendChild(el('div', { class: 'label' }, tile.label));
    node.appendChild(el('div', { class: 'value ' + (tile.tone || '') }, tile.value));
    node.appendChild(el('div', { class: 'foot' }, tile.foot));
    setFormula(node, tile.formula);
    host.appendChild(node);
  }
}

/* --------------------------------------------- chart: horizontal bar (sign) */

/**
 * Ranked horizontal bars whose colour encodes sign via the diverging pair.
 * Used for both P/L and ROC; `format` renders the value label and tooltip.
 */
function drawSignedBars(
  svgId,
  rows,
  { valueOf, format, tipRows, tipFormula, tableId, tableHead, tableRow, legendId, legendNote }
) {
  const svg = $(svgId);
  const legend = legendId ? $(legendId) : null;
  if (legend) clear(legend);
  if (!rows.length) {
    clear(svg);
    return;
  }

  const sorted = rows.slice().sort((a, b) => valueOf(b) - valueOf(a));
  const rowHeight = 26;
  const margin = { top: 8, right: 74, bottom: 26, left: 62 };
  const width = chartWidth(svg);
  const height = margin.top + margin.bottom + sorted.length * rowHeight;

  clear(svg);
  svg.setAttribute('width', width);
  svg.setAttribute('height', height);
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  const group = svgEl('g');
  svg.appendChild(group);

  const plotWidth = width - margin.left - margin.right;
  const values = sorted.map(valueOf);
  // Scale over the actual [min, max] domain rather than a symmetric one: a book
  // with one small loss should not surrender half the width to empty space.
  const domainMin = Math.min(0, ...values);
  const domainMax = Math.max(0, ...values);
  const scale = plotWidth / (domainMax - domainMin || 1);
  const zeroX = margin.left + -domainMin * scale;

  const positive = cssVar('--pos');
  const negative = cssVar('--neg');
  const surface = cssVar('--surface-1');

  group.appendChild(
    svgEl('line', {
      class: 'axis-line',
      x1: zeroX,
      x2: zeroX,
      y1: margin.top,
      y2: margin.top + sorted.length * rowHeight,
    })
  );

  sorted.forEach((row, index) => {
    const value = valueOf(row);
    const top = margin.top + index * rowHeight;
    const barHeight = 13;
    const barY = top + (rowHeight - barHeight) / 2;
    const length = Math.abs(value) * scale;
    const barX = value < 0 ? zeroX - length : zeroX;

    group.appendChild(
      svgEl(
        'text',
        {
          x: margin.left - 10,
          y: top + rowHeight / 2 + 4,
          'text-anchor': 'end',
          fill: 'var(--text-secondary)',
          'font-weight': 600,
        },
        row.underlying + (row.capital_estimated ? ' ~' : '')
      )
    );

    group.appendChild(
      svgEl('rect', {
        class: 'mark',
        x: barX,
        y: barY,
        width: Math.max(length, 1.5),
        height: barHeight,
        rx: 4, // rounded data-end
        fill: value < 0 ? negative : positive,
        stroke: surface,
        'stroke-width': 2,
      })
    );

    // Value labels sit past the data-end. When a short negative bar leaves no
    // room before the axis labels, the label flips to the empty side of zero
    // instead of colliding with the ticker name.
    const label = format(value);
    const labelWidth = label.length * 6.6;
    const outerX = value < 0 ? barX - 7 : barX + length + 7;
    const collides = value < 0 && outerX - labelWidth < margin.left;
    group.appendChild(
      svgEl(
        'text',
        {
          x: collides ? zeroX + 7 : outerX,
          y: top + rowHeight / 2 + 4,
          'text-anchor': collides ? 'start' : value < 0 ? 'end' : 'start',
          fill: 'var(--text-secondary)',
          class: 'tick-label',
        },
        label
      )
    );

    // Hit area spans the whole row, comfortably past the 24px minimum.
    const hit = svgEl('rect', {
      class: 'hit',
      x: margin.left,
      y: top,
      width: plotWidth + margin.right,
      height: rowHeight,
    });
    attachTip(hit, row.underlying, tipRows(row), tipFormula && tipFormula(row));
    group.appendChild(hit);
  });

  if (tableId) {
    buildTable(tableId, tableHead, sorted.map(tableRow));
  }

  if (legend) {
    const item = el('span');
    const swatch = el('i');
    swatch.style.background = `linear-gradient(90deg, ${positive} 50%, ${negative} 50%)`;
    item.appendChild(swatch);
    item.appendChild(document.createTextNode('Blue = positive, red = negative'));
    legend.appendChild(item);
    if (legendNote) {
      legend.appendChild(el('span', { class: 'legend-note' }, legendNote));
    }
  }
}

/* ------------------------------------------------- chart: ticker efficiency */

const TICKER_SCATTER_TABLE_HEAD = ['Ticker', 'Annualized ROC', 'Net realized P/L', 'Avg capital', 'Cycles'];

/**
 * Bubble chart combining "Realized P/L by ticker" and "Annualized Wheel ROC"
 * into one view: x = Annualized Wheel ROC (capital efficiency), y = Net
 * realized P/L (dollar magnitude), bubble area (never radius) ∝ average
 * capital deployed. The zero lines on both axes split the plot into the same
 * four quadrants the two source charts otherwise leave the reader to
 * cross-reference by hand.
 *
 * Color is the diverging pos/neg pair keyed to the y value (net P/L), the
 * same semantic "Realized P/L by ticker" already uses -- position carries the
 * quadrant, color simply reinforces it for a fast scan. Every bubble is
 * direct-labeled with its ticker: unlike a repeated-series line/bar chart,
 * each point here is one distinct, named entity (an underlying actually
 * traded), the same case "Realized P/L by ticker" already labels every bar for.
 */
function drawTickerScatter(rows) {
  const svg = $('chart-ticker-scatter');
  const legend = $('legend-ticker-scatter');
  clear(legend);

  const points = rows.filter((row) => row.annualized_wheel_roc_pct !== null);
  if (!points.length) {
    clear(svg);
    svg.removeAttribute('aria-label');
    buildTable('ticker-scatter-table', TICKER_SCATTER_TABLE_HEAD, []);
    return;
  }

  const margin = { top: 20, right: 28, bottom: 40, left: 64 };
  const width = chartWidth(svg);
  const height = 360;
  clear(svg);
  svg.setAttribute('width', width);
  svg.setAttribute('height', height);
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;

  const xValues = points.map((row) => row.annualized_wheel_roc_pct);
  const yValues = points.map((row) => row.net_realized_pl);
  // Symmetric around zero -- not just min(0, ...)/max(0, ...) -- so the 0%
  // ROC line always sits at the exact horizontal center of the plot. A skewed
  // domain (e.g. mostly positive ROC, one small loser) would otherwise push
  // that line off to one side, breaking the left/right symmetry the four
  // quadrants in the hint above are described by.
  const xAbsMax = Math.max(Math.abs(Math.min(0, ...xValues)), Math.abs(Math.max(0, ...xValues)), 1) * 1.15;
  const xMin = -xAbsMax;
  const xMax = xAbsMax;
  const yMin = Math.min(0, ...yValues) * 1.15;
  const yMax = Math.max(0, ...yValues, 1) * 1.15;

  const x = (v) => margin.left + ((v - xMin) / (xMax - xMin || 1)) * plotWidth;
  const y = (v) => margin.top + plotHeight - ((v - yMin) / (yMax - yMin || 1)) * plotHeight;

  const group = svgEl('g');
  svg.appendChild(group);

  for (const tick of niceTicks(yMin, yMax, 5)) {
    const yPos = y(tick);
    group.appendChild(
      svgEl('line', { class: 'grid-line', x1: margin.left, x2: margin.left + plotWidth, y1: yPos, y2: yPos })
    );
    group.appendChild(
      svgEl('text', { class: 'tick-label', x: margin.left - 8, y: yPos + 3.5, 'text-anchor': 'end' }, compactMoney(tick))
    );
  }
  for (const tick of niceTicks(xMin, xMax, 5)) {
    const xPos = x(tick);
    group.appendChild(
      svgEl('line', { class: 'grid-line', x1: xPos, x2: xPos, y1: margin.top, y2: margin.top + plotHeight })
    );
    group.appendChild(
      svgEl(
        'text',
        { class: 'tick-label', x: xPos, y: margin.top + plotHeight + 16, 'text-anchor': 'middle' },
        pct(tick, 0)
      )
    );
  }

  // Bolder zero baselines on both axes -- these are what actually cut the
  // plot into the four quadrants the hint paragraph describes.
  group.appendChild(
    svgEl('line', { class: 'axis-line', x1: margin.left, x2: margin.left + plotWidth, y1: y(0), y2: y(0) })
  );
  group.appendChild(
    svgEl('line', { class: 'axis-line', x1: x(0), x2: x(0), y1: margin.top, y2: margin.top + plotHeight })
  );

  // Radius scales with the SQUARE ROOT of capital so bubble AREA -- what the
  // eye actually compares -- is proportional to capital, not the radius.
  const maxCapital = Math.max(...points.map((row) => row.avg_capital || 0), 1);
  const minR = 6;
  const maxR = 28;
  const radius = (capital) => minR + (maxR - minR) * Math.sqrt((capital || 0) / maxCapital);

  const positive = cssVar('--pos');
  const negative = cssVar('--neg');
  const surface = cssVar('--surface-1');

  // Draw largest bubbles first so a small, high-ROC bubble never ends up
  // hidden underneath a big low-ROC one sharing roughly the same spot.
  const ordered = points.slice().sort((a, b) => (b.avg_capital || 0) - (a.avg_capital || 0));

  ordered.forEach((row) => {
    const cx = x(row.annualized_wheel_roc_pct);
    const cy = y(row.net_realized_pl);
    const r = radius(row.avg_capital);
    const color = row.net_realized_pl < 0 ? negative : positive;

    group.appendChild(
      svgEl('circle', {
        class: 'mark',
        cx,
        cy,
        r,
        fill: color,
        'fill-opacity': 0.72,
        stroke: surface,
        'stroke-width': 2,
      })
    );

    group.appendChild(
      svgEl(
        'text',
        {
          x: cx,
          y: cy - r - 6,
          'text-anchor': 'middle',
          fill: 'var(--text-secondary)',
          'font-weight': 600,
          class: 'tick-label',
        },
        row.underlying + (row.capital_estimated ? ' ~' : '')
      )
    );

    // Hit target is at least the 24px-diameter minimum, even for the
    // smallest bubble on the chart.
    const hit = svgEl('circle', { class: 'hit', cx, cy, r: Math.max(r, 12) });
    group.appendChild(hit);

    const rocFormula =
      row.annualized_wheel_roc_pct === null
        ? formula([
            'Annualized Wheel ROC (return on capital) =',
            '  (Option premium P/L ÷ Avg capital) × (365 ÷ Days)',
            '',
            'N/A -- no capital committed.',
          ])
        : formula([
            'Bubble position: x = Annualized Wheel ROC (return on capital),',
            '  y = Net realized profit/loss (P/L)',
            'Bubble size: area proportional to avg capital deployed',
            '',
            `Annualized Wheel ROC = (Option premium P/L ÷ Avg capital) × (365 ÷ Days) = ${pct(row.annualized_wheel_roc_pct)}`,
            `Net realized P/L = Premium collected (net) + Stock P/L = ${money(row.net_realized_pl, { cents: true })}`,
            `Avg capital deployed (bubble size) = ${money(row.avg_capital)}`,
          ]);

    attachTip(
      hit,
      row.underlying,
      [
        { label: 'Annualized Wheel ROC', value: pct(row.annualized_wheel_roc_pct), color },
        { label: 'Net realized P/L', value: money(row.net_realized_pl, { cents: true }), color },
        { label: 'Avg capital deployed', value: money(row.avg_capital) },
        { label: 'Peak capital', value: money(row.peak_capital) },
        { label: 'Cycles', value: `${row.cycles} (${row.active} active)` },
        ...(row.capital_estimated ? [{ label: 'Note', value: 'includes strike-based proxy' }] : []),
      ],
      rocFormula
    );
  });

  group.appendChild(
    svgEl(
      'text',
      { x: margin.left + plotWidth / 2, y: height - 4, 'text-anchor': 'middle', class: 'tick-label' },
      'Annualized Wheel ROC →'
    )
  );

  svg.setAttribute(
    'aria-label',
    `Ticker efficiency: Annualized Wheel ROC vs. Net realized P/L, bubble size proportional to average ` +
      `capital deployed. ${points.length} ticker(s) plotted; hover or focus a bubble for its figures.`
  );

  legend.appendChild(
    el(
      'span',
      { class: 'legend-note' },
      'Bubble size = avg capital deployed. Blue = net gain, red = net loss. Right of the vertical ' +
        'line = positive ROC; above the horizontal line = net profit.'
    )
  );

  buildTable(
    'ticker-scatter-table',
    TICKER_SCATTER_TABLE_HEAD,
    ordered.map((row) => [
      row.underlying,
      pct(row.annualized_wheel_roc_pct),
      money(row.net_realized_pl, { cents: true }),
      money(row.avg_capital),
      row.cycles,
    ])
  );
}

/* ----------------------------------------------------- chart: wheel timeline */

const LEG_STYLES = {
  CSP: { label: 'Cash-secured put', varName: '--series-1' },
  COVERED_CALL: { label: 'Covered call', varName: '--series-2' },
  LONG_PUT: { label: 'Long put', varName: '--series-4' },
  LONG_CALL: { label: 'Long call', varName: '--series-4' },
};

function drawTimeline(cycles, through) {
  const svg = $('chart-timeline');
  const legend = $('legend-timeline');
  clear(legend);
  if (!cycles.length) {
    clear(svg);
    return;
  }

  const ordered = cycles
    .slice()
    .sort((a, b) => parseDay(a.start_date) - parseDay(b.start_date) || a.cycle_id.localeCompare(b.cycle_id));

  const endOf = (leg) => leg.close_date || through;
  const laneHeight = 11;
  const margin = { top: 10, right: 18, bottom: 30, left: 108 };

  // Pack each cycle's legs into sub-lanes so overlapping positions stay legible.
  const laid = ordered.map((cycle) => {
    const legs = cycle.legs
      .slice()
      .sort((a, b) => parseDay(a.open_date) - parseDay(b.open_date));
    const laneEnds = [];
    const placed = legs.map((leg) => {
      const start = parseDay(leg.open_date).getTime();
      const finish = parseDay(endOf(leg)).getTime();
      let lane = laneEnds.findIndex((end) => end <= start);
      if (lane === -1) {
        lane = laneEnds.length;
        laneEnds.push(0);
      }
      laneEnds[lane] = finish + 86400000 / 2;
      return { leg, lane };
    });
    return { cycle, placed, lanes: Math.max(1, laneEnds.length) };
  });

  const rowPad = 9;
  const rowTops = [];
  let cursor = margin.top;
  for (const row of laid) {
    rowTops.push(cursor);
    cursor += row.lanes * laneHeight + rowPad;
  }

  const width = chartWidth(svg, 640);
  const height = cursor + margin.bottom;
  const plotWidth = width - margin.left - margin.right;

  const allDates = ordered.flatMap((cycle) => [
    parseDay(cycle.start_date).getTime(),
    parseDay(cycle.end_date || through).getTime(),
  ]);
  const tMin = Math.min(...allDates);
  const tMax = Math.max(...allDates, parseDay(through).getTime());
  const x = (iso) =>
    margin.left + (tMax === tMin ? 0 : ((parseDay(iso).getTime() - tMin) / (tMax - tMin)) * plotWidth);

  clear(svg);
  svg.setAttribute('width', width);
  svg.setAttribute('height', height);
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  const group = svgEl('g');
  svg.appendChild(group);

  const surface = cssVar('--surface-1');
  const ghost = cssVar('--ghost');

  laid.forEach((row, index) => {
    const top = rowTops[index];
    const rowHeight = row.lanes * laneHeight;

    if (index % 2 === 0) {
      group.appendChild(
        svgEl('rect', {
          x: 0,
          y: top - rowPad / 2,
          width,
          height: rowHeight + rowPad,
          fill: ghost,
        })
      );
    }

    // Full-row click target -- opens this wheel in the Trade Log tab. Painted
    // before the per-leg `.hit` rects so their hover tooltips still win.
    const rowHit = svgEl('rect', {
      class: 'row-hit',
      x: 0,
      y: top - rowPad / 2,
      width,
      height: rowHeight + rowPad,
    });
    rowHit.addEventListener('click', () => openTradeLog(row.cycle.cycle_id));
    group.appendChild(rowHit);

    const label = svgEl(
      'text',
      {
        x: margin.left - 10,
        y: top + rowHeight / 2 + 4,
        'text-anchor': 'end',
        fill: 'var(--text-secondary)',
        'font-weight': 600,
      },
      row.cycle.cycle_id
    );
    label.style.cursor = 'pointer';
    label.addEventListener('click', () => openTradeLog(row.cycle.cycle_id));
    group.appendChild(label);

    row.placed.forEach(({ leg, lane }) => {
      const style = LEG_STYLES[leg.strategy] || LEG_STYLES.CSP;
      const color = cssVar(style.varName);
      const y = top + lane * laneHeight;
      const x1 = x(leg.open_date);
      const x2 = Math.max(x(endOf(leg)), x1 + 3);

      group.appendChild(
        svgEl('rect', {
          class: 'mark',
          x: x1,
          y: y + 1.5,
          width: x2 - x1,
          height: laneHeight - 4,
          rx: 3,
          fill: color,
          'fill-opacity': leg.remaining > 0 ? 0.45 : 0.9,
          stroke: surface,
          'stroke-width': 1.5,
        })
      );

      const mid = y + (laneHeight - 1) / 2;
      // ▲ sold to open at the left edge.
      group.appendChild(
        svgEl('path', {
          d: `M${x1},${mid - 4.5}L${x1 + 3.6},${mid + 2}L${x1 - 3.6},${mid + 2}Z`,
          fill: color,
          stroke: surface,
          'stroke-width': 1.5,
        })
      );
      // ▼ bought to close, or ◆ assigned, at the right edge.
      if (leg.close_date) {
        const assigned = leg.outcome === 'ASSIGNED';
        group.appendChild(
          svgEl('path', {
            d: assigned
              ? `M${x2},${mid - 4.5}L${x2 + 4},${mid}L${x2},${mid + 4.5}L${x2 - 4},${mid}Z`
              : `M${x2},${mid + 4.5}L${x2 + 3.6},${mid - 2}L${x2 - 3.6},${mid - 2}Z`,
            fill: assigned ? cssVar('--series-2') : color,
            stroke: surface,
            'stroke-width': 1.5,
          })
        );
      }

      const hit = svgEl('rect', {
        class: 'hit',
        x: x1 - 8,
        y: y - 2,
        width: x2 - x1 + 16,
        height: laneHeight + 4,
      });
      // A close can carry a roll_id too -- the leg on the other end of the same
      // roll, closed rather than opened by it. Most legs have one close, but list
      // every distinct roll in case a partial fill was rolled across two rows.
      const closedByRolls = [...new Set(leg.closes.map((c) => c.roll_id).filter(Boolean))];
      attachTip(hit, leg.symbol, [
        {
          label: 'Strategy',
          value: style.label + (leg.shares_tracked === false ? ' (shares pre-date export)' : ''),
          color,
        },
        { label: 'Opened', value: `${leg.open_date} · ${leg.contracts}x @ ${leg.open_price ?? '—'}` },
        {
          label: leg.close_date ? 'Closed' : 'Status',
          value: leg.close_date ? `${leg.close_date} · ${leg.outcome}` : 'OPEN',
        },
        { label: 'Realized P/L', value: money(leg.realized_pl, { cents: true }) },
        { label: 'Collateral', value: money(leg.collateral) },
        ...(leg.opened_by_roll ? [{ label: 'Opened by', value: 'roll ' + leg.opened_by_roll }] : []),
        ...(closedByRolls.length ? [{ label: 'Closed by', value: 'roll ' + closedByRolls.join(', ') }] : []),
      ], legCollateralFormula(leg));
      hit.addEventListener('click', () => openTradeLog(row.cycle.cycle_id));
      group.appendChild(hit);
    });
  });

  const axisY = cursor;
  const ticks = 6;
  group.appendChild(
    svgEl('line', {
      class: 'axis-line',
      x1: margin.left,
      x2: margin.left + plotWidth,
      y1: axisY,
      y2: axisY,
    })
  );
  for (let i = 0; i <= ticks; i += 1) {
    const time = tMin + ((tMax - tMin) * i) / ticks;
    const iso = localIso(time);
    group.appendChild(
      svgEl(
        'text',
        {
          class: 'tick-label',
          x: margin.left + (plotWidth * i) / ticks,
          y: axisY + 16,
          'text-anchor': 'middle',
        },
        dayLabel(iso)
      )
    );
  }

  const seen = new Set();
  for (const strategy of Object.keys(LEG_STYLES)) {
    const style = LEG_STYLES[strategy];
    if (seen.has(style.label)) continue;
    seen.add(style.label);
    const item = el('span');
    const swatch = el('i');
    swatch.style.background = cssVar(style.varName);
    item.appendChild(swatch);
    item.appendChild(document.createTextNode(style.label));
    legend.appendChild(item);
  }
  legend.appendChild(el('span', {}, '▲ sold to open   ▼ bought to close   ◆ assigned'));
  legend.appendChild(el('span', {}, 'Faded bar = still open'));

  buildTable(
    'timeline-table',
    ['Cycle', 'Symbol', 'Strategy', 'Opened', 'Closed', 'Contracts', 'Outcome', 'Realized P/L'],
    ordered.flatMap((cycle) =>
      cycle.legs.map((leg) => [
        cycle.cycle_id,
        leg.symbol,
        leg.strategy,
        leg.open_date,
        leg.close_date || '—',
        leg.contracts,
        leg.outcome,
        money(leg.realized_pl, { cents: true }),
      ])
    )
  );
}

/* ------------------------------------------------------------------- tables */

/**
 * A `<th>`/`<td>` that may carry a calculation tooltip: either a plain
 * value/label, or `{ text, title }` to attach the formula behind it as a
 * native hover tooltip on that one cell.
 */
function cellNode(tag, className, value) {
  if (value !== null && typeof value === 'object' && 'text' in value) {
    const node = el(tag, { class: className }, value.text);
    setFormula(node, value.title);
    return node;
  }
  return el(tag, { class: className }, value);
}

function buildTable(containerId, head, rows) {
  const container = $(containerId);
  if (!container) return;
  clear(container);
  const table = el('table');
  const thead = el('thead');
  const headRow = el('tr');
  head.forEach((label, index) => headRow.appendChild(cellNode('th', index === 0 ? 'left' : '', label)));
  thead.appendChild(headRow);
  table.appendChild(thead);

  const tbody = el('tbody');
  for (const row of rows) {
    const tr = el('tr');
    row.forEach((cell, index) => tr.appendChild(cellNode('td', index === 0 ? 'left' : 'num', cell)));
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  container.appendChild(table);
}

/**
 * Real formulas behind each calculated cycle-table column, built only from
 * fields the API actually sends for this cycle (never approximated).
 * `cycle.capital` is that cycle's own daily capital timeline and `cycle.legs`
 * its leg list, both already in the payload -- see `_cycle_payload` /
 * `leg_rows` in wheel/metrics.py and wheel/api.py.
 */
function cycleFormulas(cycle) {
  const day1 = cycle.capital && cycle.capital[0];
  const initial = day1
    ? formula([
        "Initial cap = day one's total committed capital",
        '  Put collateral + Stock basis + Call proxy + Long debit + Spread collateral',
        `= ${money(day1.put)} + ${money(day1.stock)} + ${money(day1.call)} + ${money(day1.long)} + ${money(day1.spread)}`,
        `= ${money(cycle.initial_collateral)}`,
      ])
    : formula(["Initial cap = day one's total committed capital"]);

  const engagedDays = (cycle.capital || []).filter((p) => p.total > 0).length;
  const totalDays = (cycle.capital || []).length;
  const avgCap = formula([
    'Avg cap = time-weighted mean of the daily total,',
    '  over the days capital was actually committed',
    `  (${engagedDays} of ${totalDays} days in this cycle; $0 days excluded)`,
    `= ${money(cycle.avg_collateral)}`,
  ]);

  const roi = formula([
    'ROI (return on investment) = Net realized P/L ÷ Initial collateral',
    '  Net P/L here includes stock profit/loss, not just option premium.',
    '',
    `= ${money(cycle.net_realized_pl, { cents: true })} ÷ ${money(cycle.initial_collateral)}`,
    `= ${cycle.roi_pct === null ? 'N/A' : pct(cycle.roi_pct, 2)}`,
  ]);

  const wheelRoc =
    cycle.annualized_wheel_roc_pct === null
      ? formula([
          'Annualized Wheel ROC (return on capital) =',
          '  (Wheel option P/L ÷ Avg collateral) × (365 ÷ Days active)',
          '',
          'N/A -- no capital committed in this cycle.',
        ])
      : formula([
          ...wheelOptionPlFormula(cycle, 'Wheel option P/L').split('\n'),
          '',
          'Annualized Wheel ROC (return on capital) =',
          '  (Wheel option P/L ÷ Avg collateral) × (365 ÷ Days active)',
          '  Includes cash-secured-put, covered-call, and hedge legs; excludes stock P/L.',
          '',
          `= (${money(cycle.option_realized_pl, { cents: true })} ÷ ${money(cycle.avg_collateral)}) × (365 ÷ ${cycle.days_active})`,
          `= ${pct(cycle.roi_on_avg_wheel_pct, 2)} × ${(365 / cycle.days_active).toFixed(2)}`,
          `= ${pct(cycle.annualized_wheel_roc_pct)}`,
        ]);

  const netOptionYield =
    cycle.annualized_net_option_yield_pct === null
      ? formula([
          'Annualized Net Option Yield =',
          '  (Net premium income - Debit adjustments - Fees) ÷ Initial collateral × (365 ÷ Days active)',
          '',
          'N/A -- no initial collateral committed in this cycle.',
        ])
      : formula([
          'Net Option Yield %: premium income only, measured against the capital',
          '  committed on day one (not a time-weighted average over the cycle).',
          '  Fees are already netted into every cash figure here.',
          '',
          'Net Option Yield = Option P/L ÷ Initial collateral',
          `= ${money(cycle.option_realized_pl, { cents: true })} ÷ ${money(cycle.initial_collateral)}`,
          `= ${pct(cycle.net_option_yield_pct, 2)}`,
          '',
          'Annualized = Net Option Yield × (365 ÷ Days active)',
          `= ${pct(cycle.net_option_yield_pct, 2)} × ${(365 / cycle.days_active).toFixed(2)}`,
          `= ${pct(cycle.annualized_net_option_yield_pct)}`,
        ]);

  const totalPositionRoi =
    cycle.annualized_total_position_roi_pct === null
      ? formula([
          'Annualized Total Position ROI (return on investment) =',
          '  (Option P/L + Stock realized/unrealized P&L + Dividends) ÷ Initial collateral × (365 ÷ Days active)',
          '',
          'N/A -- no initial collateral committed in this cycle.',
        ])
      : formula([
          'Total Position ROI %: everything this position has produced --',
          '  option P/L, realized AND unrealized stock P/L, and dividends --',
          '  against day-one capital. Unrealized P/L on an open long-option',
          '  hedge is not included: no options-quote feed exists to mark it.',
          '',
          'Total Position ROI (return on investment) = (Option P/L + Stock realized P/L +',
          '  Stock unrealized P/L + Dividends) ÷ Initial collateral',
          `= (${money(cycle.option_realized_pl, { cents: true })} + ${money(cycle.stock_realized_pl, { cents: true })} + ${
            cycle.stock_unrealized_pl === null ? 'N/A' : money(cycle.stock_unrealized_pl, { cents: true })
          } + ${money(cycle.dividends_received, { cents: true })}) ÷ ${money(cycle.initial_collateral)}`,
          `= ${pct(cycle.total_position_roi_pct, 2)}`,
          '',
          'Annualized = Total Position ROI × (365 ÷ Days active)',
          `= ${pct(cycle.total_position_roi_pct, 2)} × ${(365 / cycle.days_active).toFixed(2)}`,
          `= ${pct(cycle.annualized_total_position_roi_pct)}`,
        ]);

  const held = (cycle.legs || [])
    .map((leg) => leg.days_held)
    .filter((d) => d !== null && d !== undefined);
  const avgDays =
    cycle.avg_days_in_trade === null
      ? formula(['Avg days in trade = mean(days held), over closed legs', '', 'N/A -- no closed legs yet.'])
      : formula([
          'Avg days in trade = mean(days held), over closed legs',
          `  (${held.length} closed leg(s) in this cycle)`,
          '',
          `= ${cycle.avg_days_in_trade.toFixed(2)}`,
        ]);

  const premium = wheelOptionPlFormula(cycle, 'Premium (net)');

  const ppd = formula([
    'Profit Per Day (PPD) =',
    '  (Realized premium collected - Closeout cost) ÷ Days active',
    '  Option premium only -- never includes stock profit/loss.',
    '',
    `= ${money(cycle.option_realized_pl, { cents: true })} ÷ ${cycle.days_active}`,
    `= ${money(cycle.profit_per_day, { cents: true })}/day`,
  ]);

  const netPl = formula([
    'Net P/L = Premium (net) + Stock realized P/L',
    '',
    `= ${money(cycle.option_realized_pl, { cents: true })} + ${money(cycle.stock_realized_pl, { cents: true })}`,
    `= ${money(cycle.net_realized_pl, { cents: true })}`,
  ]);

  const days = formula([
    'Days = (End date, or the filter\'s "through" date if still open)',
    '  - Start date, at least 1 day',
    `= ${cycle.days_active}`,
  ]);

  return { initial, avgCap, roi, wheelRoc, netOptionYield, totalPositionRoi, avgDays, premium, ppd, netPl, days };
}

const CYCLE_STATUS_MEANING = {
  ACTIVE: 'ACTIVE: something is still open -- a short leg, a long hedge, or shares held.',
  CLOSED: 'CLOSED: fully flat -- no open contracts, no shares. Never went through an assignment.',
  ASSIGNED: 'ASSIGNED: fully flat now, but this campaign went through at least one option assignment along the way.',
};

const CYCLE_COLUMNS = [
  { key: 'cycle_id', label: 'Cycle', left: true },
  { key: 'status', label: 'Status', left: true },
  { key: 'start_date', label: 'Start', left: true },
  { key: 'end_date', label: 'End', left: true },
  { key: 'days_active', label: 'Days' },
  { key: 'legs_total', label: 'Legs' },
  { key: 'rolls', label: 'Rolls' },
  { key: 'assignments', label: 'Assign' },
  { key: 'option_realized_pl', label: 'Premium (net)' },
  { key: 'profit_per_day', label: 'PPD' },
  { key: 'net_realized_pl', label: 'Net P/L' },
  { key: 'initial_collateral', label: 'Initial cap' },
  { key: 'avg_collateral', label: 'Avg cap' },
  { key: 'roi_pct', label: 'ROI' },
  { key: 'annualized_wheel_roc_pct', label: 'Wheel ROC' },
  { key: 'annualized_net_option_yield_pct', label: 'Net Option Yield' },
  { key: 'annualized_total_position_roi_pct', label: 'Total Position ROI' },
  { key: 'avg_days_in_trade', label: 'Avg days' },
];

function renderCycles(cycles) {
  const table = $('cycles-table');
  clear(table);

  const thead = el('thead');
  const headRow = el('tr');
  for (const column of CYCLE_COLUMNS) {
    const th = el('th', { class: `sortable${column.left ? ' left' : ''}` }, column.label);
    if (state.cycleSort.key === column.key) {
      th.textContent = column.label + (state.cycleSort.dir === 1 ? ' ▲' : ' ▼');
    }
    th.addEventListener('click', () => {
      if (state.cycleSort.key === column.key) state.cycleSort.dir *= -1;
      else state.cycleSort = { key: column.key, dir: -1 };
      renderCycles(cycles);
    });
    headRow.appendChild(th);
  }
  thead.appendChild(headRow);
  table.appendChild(thead);

  const { key, dir } = state.cycleSort;
  const sorted = cycles.slice().sort((a, b) => {
    const left = a[key];
    const right = b[key];
    if (left === null || left === undefined) return 1;
    if (right === null || right === undefined) return -1;
    return (typeof left === 'string' ? left.localeCompare(right) : left - right) * dir;
  });

  const tbody = el('tbody');
  if (!sorted.length) {
    const tr = el('tr');
    const td = el('td', { colspan: CYCLE_COLUMNS.length, class: 'empty left' }, 'No cycles in this slice.');
    tr.appendChild(td);
    tbody.appendChild(tr);
  }

  for (const cycle of sorted) {
    const tr = el('tr', { class: 'cycle-row' + (state.expanded.has(cycle.cycle_id) ? ' open' : '') });

    const idCell = el('td', { class: 'left ticker-cell' });
    idCell.appendChild(document.createTextNode(cycle.cycle_id));
    if (cycle.capital_estimated) {
      const flag = el('span', { class: 'est-flag', title: 'Includes a strike-based capital proxy' }, '~');
      idCell.appendChild(flag);
    }
    tr.appendChild(idCell);

    const statusCell = el('td', { class: 'left' });
    const statusBadge = el('span', { class: 'badge ' + cycle.status }, cycle.status);
    setFormula(statusBadge, CYCLE_STATUS_MEANING[cycle.status] || null);
    statusCell.appendChild(statusBadge);
    tr.appendChild(statusCell);

    tr.appendChild(el('td', { class: 'left' }, cycle.start_date));
    tr.appendChild(el('td', { class: 'left' }, cycle.end_date || '—'));

    const f = cycleFormulas(cycle);
    const daysCell = el('td', { class: 'num' }, cycle.days_active);
    setFormula(daysCell, f.days);
    tr.appendChild(daysCell);

    tr.appendChild(el('td', { class: 'num' }, cycle.legs_total));
    tr.appendChild(el('td', { class: 'num' }, cycle.rolls));
    tr.appendChild(el('td', { class: 'num' }, cycle.assignments));

    const premiumCell = el('td', { class: 'num' }, money(cycle.option_realized_pl, { cents: true }));
    setFormula(premiumCell, f.premium);
    tr.appendChild(premiumCell);

    const ppdCell = el('td', { class: 'num' }, money(cycle.profit_per_day, { cents: true }) + '/day');
    setFormula(ppdCell, f.ppd);
    tr.appendChild(ppdCell);

    const pl = el('td', { class: 'num ' + (cycle.net_realized_pl >= 0 ? 'pos' : 'neg') },
      money(cycle.net_realized_pl, { cents: true }));
    setFormula(pl, f.netPl);
    tr.appendChild(pl);

    const initialCell = el('td', { class: 'num' }, money(cycle.initial_collateral));
    setFormula(initialCell, f.initial);
    tr.appendChild(initialCell);

    const avgCapCell = el('td', { class: 'num' }, money(cycle.avg_collateral));
    setFormula(avgCapCell, f.avgCap);
    tr.appendChild(avgCapCell);

    const roiCell = el('td', { class: 'num' }, pct(cycle.roi_pct, 2));
    setFormula(roiCell, f.roi);
    tr.appendChild(roiCell);

    const rocCell = el('td', { class: 'num' }, pct(cycle.annualized_wheel_roc_pct));
    setFormula(rocCell, f.wheelRoc);
    tr.appendChild(rocCell);

    const netOptionYieldCell = el('td', { class: 'num' }, pct(cycle.annualized_net_option_yield_pct));
    setFormula(netOptionYieldCell, f.netOptionYield);
    tr.appendChild(netOptionYieldCell);

    const totalPositionRoiCell = el('td', { class: 'num' }, pct(cycle.annualized_total_position_roi_pct));
    setFormula(totalPositionRoiCell, f.totalPositionRoi);
    tr.appendChild(totalPositionRoiCell);

    const avgDaysCell = el(
      'td',
      { class: 'num' },
      cycle.avg_days_in_trade === null ? '—' : cycle.avg_days_in_trade.toFixed(1)
    );
    setFormula(avgDaysCell, f.avgDays);
    tr.appendChild(avgDaysCell);

    tr.addEventListener('click', () => {
      if (state.expanded.has(cycle.cycle_id)) state.expanded.delete(cycle.cycle_id);
      else state.expanded.add(cycle.cycle_id);
      renderCycles(cycles);
    });
    tbody.appendChild(tr);

    if (state.expanded.has(cycle.cycle_id)) {
      tbody.appendChild(cycleDetail(cycle));
    }
  }
  table.appendChild(tbody);
}

/**
 * The exact branch of `OptionLeg.collateral_per_contract` (wheel/engine.py)
 * that produced this leg's collateral figure, spelled out with its real
 * strike/contracts/open-cash rather than a generic description.
 *
 * When some of this leg's contracts are paired into a Spread (same-day short
 * + long, same underlying/right/expiry -- see the Spreads table below), those
 * contracts' collateral is reported once, at the Spread's own netted rate,
 * not here -- so this only ever prices the leg's naked (unpaired) contracts.
 */
function legCollateralFormula(leg) {
  const contracts = leg.naked_contracts ?? leg.contracts;
  const pairedNote =
    leg.paired_contracts > 0
      ? [
          `${leg.paired_contracts} of ${leg.contracts} contract(s) are paired into a spread`,
          '  -- a matched same-day short + long position whose collateral is',
          '  netted as one |short strike - long strike| figure, not priced here.',
          `  Only the remaining ${contracts} naked contract(s) are priced below.`,
          '',
        ]
      : [];
  if (leg.strategy === 'CSP') {
    return formula([
      ...pairedNote,
      'Collateral = Strike × 100 × Naked contracts',
      `= $${leg.strike} × 100 × ${contracts}`,
      `= ${money(leg.collateral)}`,
    ]);
  }
  if (leg.strategy === 'COVERED_CALL') {
    if (leg.shares_tracked) {
      return formula([
        'Collateral = $0',
        '  The shares backing this call are already counted as capital',
        '  on their own -- a covered call adds nothing on top of shares',
        '  already held.',
      ]);
    }
    return formula([
      ...pairedNote,
      'Collateral = Strike × 100 × Naked contracts',
      '  (proxy: these shares pre-date the export, so their real cost',
      '  basis is not visible -- the strike stands in for it)',
      `= $${leg.strike} × 100 × ${contracts}`,
      `= ${money(leg.collateral)}`,
    ]);
  }
  if (leg.side === 'LONG') {
    return formula([
      ...pairedNote,
      'Collateral = |Debit paid at open ÷ Contracts| × Naked contracts',
      `= |${money(leg.open_cash, { cents: true })} ÷ ${leg.contracts}| × ${contracts}`,
      `= ${money(leg.collateral)}`,
    ]);
  }
  return formula(['Collateral = $0 -- this leg commits no capital.']);
}

/**
 * Right side of a Spread's netted collateral figure -- mirrors
 * `Spread.collateral_per_contract` in wheel/engine.py.
 */
function spreadCollateralFormula(spread) {
  return formula([
    'Collateral = |Short strike - Long strike| × 100 × Paired contracts',
    `= |$${spread.short_strike} - $${spread.long_strike}| × 100 × ${spread.paired_contracts}`,
    `= ${money(spread.collateral)}`,
    '',
    'Net credit = Short premium received - Long premium paid (paired portion only)',
    `= ${money(spread.net_credit, { cents: true })}`,
  ]);
}

function cycleDetail(cycle) {
  const tr = el('tr', { class: 'detail' });
  const td = el('td', { colspan: CYCLE_COLUMNS.length });
  const inner = el('div', { class: 'detail-inner' });

  inner.appendChild(el('h3', {}, `Legs (${cycle.legs.length})`));
  const legHost = el('div');
  inner.appendChild(legHost);
  buildTableInto(
    legHost,
    ['Symbol', 'Strategy', 'Opened', 'Contracts', 'Price', 'Closed', 'Outcome', 'Days', 'Collateral', 'Realized P/L'],
    cycle.legs.map((leg) => [
      leg.symbol,
      leg.strategy,
      leg.open_date,
      leg.contracts,
      leg.open_price ?? '—',
      leg.close_date || '—',
      leg.outcome,
      leg.days_held ?? '—',
      { text: money(leg.collateral), title: legCollateralFormula(leg) },
      money(leg.realized_pl, { cents: true }),
    ])
  );

  if (cycle.spreads && cycle.spreads.length) {
    inner.appendChild(el('h3', {}, `Spreads (${cycle.spreads.length})`));
    inner.appendChild(
      el(
        'p',
        { class: 'hint' },
        'A short and a long leg of the same underlying, right and expiry, opened the same day, pair ' +
          'automatically -- collateral nets to the strike distance instead of the short leg\'s full ' +
          'cash-secured-put/covered-call figure. Legs that don\'t pair this way appear in the Legs table, priced normally.'
      )
    );
    const spreadHost = el('div');
    inner.appendChild(spreadHost);
    buildTableInto(
      spreadHost,
      ['Right', 'Expiry', 'Opened', 'Short strike', 'Long strike', 'Paired', 'Collateral', 'Net credit'],
      cycle.spreads.map((spread) => [
        spread.right === 'P' ? 'PUT' : 'CALL',
        spread.expiry,
        spread.open_date,
        spread.short_strike,
        spread.long_strike,
        spread.paired_contracts,
        { text: money(spread.collateral) + (spread.capital_estimated ? ' ~' : ''), title: spreadCollateralFormula(spread) },
        money(spread.net_credit, { cents: true }),
      ])
    );
  }

  if (cycle.share_lots && cycle.share_lots.length) {
    inner.appendChild(el('h3', {}, `Share lots (${cycle.share_lots.length})`));
    const lotHost = el('div');
    inner.appendChild(lotHost);
    buildTableInto(
      lotHost,
      [
        'Acquired',
        'Source',
        'Shares',
        'Remaining',
        {
          text: 'Tax basis',
          title: formula([
            'The raw assignment/purchase price -- what a 1099-B would show.',
            '"Unknown" means these shares were acquired before this export',
            'begins: the real cost basis was never seen, so it is reported',
            'as unknown rather than invented.',
          ]),
        },
        'Net adjusted cost basis',
      ],
      cycle.share_lots.map((lot) => [
        lot.acquired,
        lot.source,
        lot.shares,
        lot.remaining,
        lot.basis_known ? money(lot.basis_per_share) : 'Unknown',
        {
          text: lot.net_adjusted_cost_basis === null ? 'N/A' : money(lot.net_adjusted_cost_basis),
          title: formula([
            'Net adjusted cost basis = Tax basis',
            '  - (this cycle\'s net option cash flow ÷ share, pro-rated to this lot)',
            '  Distinct from tax basis: this is the wheel\'s own economic',
            '  break-even, not the raw price a 1099-B would show.',
            '',
            lot.basis_known
              ? `Tax basis = ${money(lot.basis_per_share)}`
              : 'Tax basis unknown -- shares pre-date this export.',
            lot.net_adjusted_cost_basis === null
              ? 'N/A -- no strike to net against.'
              : `= ${money(lot.net_adjusted_cost_basis)}`,
          ]),
        },
      ])
    );
  }

  if (cycle.roll_events.length) {
    inner.appendChild(el('h3', {}, `Rolls (${cycle.roll_events.length})`));
    inner.appendChild(
      el(
        'p',
        { class: 'hint' },
        'A same-day close-and-reopen on the same underlying and option right, where the new expiry is ' +
          'no earlier than the one closed -- re-entering the identical contract just closed is a round ' +
          'trip, not a roll. Quantities are not required to match: a real roll can close 4 contracts and ' +
          'open 2.'
      )
    );
    const rollHost = el('div');
    inner.appendChild(rollHost);
    buildTableInto(
      rollHost,
      [
        'Date',
        {
          text: 'Direction',
          title: formula([
            'OUT: same strike, later expiry.  UP/DOWN: same expiry, strike moved.',
            'OUT_AND_UP / OUT_AND_DOWN: both -- later expiry and a moved strike.',
          ]),
        },
        'Closed',
        'Opened',
        {
          text: 'Net credit',
          title: formula([
            'Net credit = Σ cash from every closed leg + Σ cash from every opened leg',
            '  (a positive roll banks a credit; a negative one costs a debit to move the position)',
          ]),
        },
      ],
      cycle.roll_events.map((roll) => [
        roll.date,
        roll.direction.replace(/_/g, ' ').toLowerCase(),
        roll.closed.map((item) => `${item.contracts}x ${item.strike}`).join(', '),
        roll.opened.map((item) => `${item.contracts}x ${item.strike}`).join(', '),
        money(roll.net_credit, { cents: true }),
      ])
    );
  }

  if (cycle.assignment_events.length) {
    inner.appendChild(
      el('h3', {}, `Assignments (${cycle.assignment_events.length}) — share legs synthesized at strike`)
    );
    inner.appendChild(
      el(
        'p',
        { class: 'hint' },
        'The share movement an option assignment creates. When the broker\'s own export contains the ' +
          'equity fill it\'s used directly; otherwise (most exports) it\'s synthesized at the option\'s ' +
          'strike price and flagged in the Note column below.'
      )
    );
    const host = el('div');
    inner.appendChild(host);
    buildTableInto(
      host,
      [
        'Date',
        'Symbol',
        'Direction',
        'Shares',
        'Strike',
        {
          text: 'Implied cash',
          title: formula([
            'Cash the share movement implies at the strike price:',
            '  ACQUIRE = -Strike × Shares (buying the stock)',
            '  DISPOSE = +Strike × Shares (selling the stock, i.e. called away)',
            'From the broker\'s own equity fill when the export supplies one;',
            'synthesized at the strike price otherwise.',
          ]),
        },
        'Note',
      ],
      cycle.assignment_events.map((item) => [
        item.date,
        item.symbol,
        item.direction,
        item.shares,
        item.strike,
        money(item.cash),
        item.note,
      ])
    );
  }

  if (cycle.warnings && cycle.warnings.length) {
    const notice = el('div', { class: 'notice' });
    notice.appendChild(el('strong', {}, 'Data notes'));
    const list = el('ul');
    cycle.warnings.forEach((warning) => list.appendChild(el('li', {}, warning)));
    notice.appendChild(list);
    inner.appendChild(notice);
  }

  td.appendChild(inner);
  tr.appendChild(td);
  return tr;
}

function buildTableInto(host, head, rows) {
  const table = el('table');
  const thead = el('thead');
  const headRow = el('tr');
  head.forEach((label, index) => headRow.appendChild(cellNode('th', index === 0 ? 'left' : '', label)));
  thead.appendChild(headRow);
  table.appendChild(thead);
  const tbody = el('tbody');
  for (const row of rows) {
    const tr = el('tr');
    row.forEach((cell, index) => tr.appendChild(cellNode('td', index === 0 ? 'left' : 'num', cell)));
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  host.appendChild(table);
}

/* -------------------------------------------------------------- stat tiles */

function renderTiles(portfolio, reconciliation) {
  const host = $('tiles');
  clear(host);

  const days = portfolio.days_span;
  const scale = days ? 365 / days : null;
  const wl = portfolio.wins + portfolio.losses;

  const tiles = [
    {
      label: 'Profit Per Day (PPD)',
      value: money(portfolio.profit_per_day, { cents: true }) + '/day',
      foot: `${money(portfolio.option_realized_pl, { cents: true })} realized premium over ${portfolio.days_span} calendar days`,
      tone: portfolio.profit_per_day >= 0 ? 'pos' : 'neg',
      primary: true,
      formula: formula([
        'Portfolio PPD = Σ (Premium collected - Closeout cost) ÷ Days',
        '  Every cycle\'s realized option P/L; never stock P/L.',
        '',
        `= ${money(portfolio.option_realized_pl, { cents: true })} ÷ ${portfolio.days_span}`,
        `= ${money(portfolio.profit_per_day, { cents: true })}/day`,
      ]),
    },
    {
      label: 'Net realized P/L',
      value: money(portfolio.net_realized_pl, { cents: true }),
      foot: `${money(portfolio.option_realized_pl)} options · ${money(portfolio.stock_realized_pl)} stock`,
      tone: portfolio.net_realized_pl >= 0 ? 'pos' : 'neg',
      formula: formula([
        'Net realized P/L = Premium collected (net) + Stock realized P/L',
        '',
        `= ${money(portfolio.option_realized_pl, { cents: true })} + ${money(portfolio.stock_realized_pl, { cents: true })}`,
        `= ${money(portfolio.net_realized_pl, { cents: true })}`,
      ]),
    },
    {
      label: 'Premium collected (net)',
      value: money(portfolio.option_realized_pl, { cents: true }),
      foot: `${money(portfolio.premium_received)} gross · ${money(portfolio.premium_paid)} paid to close · ${money(portfolio.open_premium)} still open`,
      tone: portfolio.option_realized_pl >= 0 ? 'pos' : 'neg',
      formula: wheelOptionPlFormula(portfolio, 'Wheel option P/L (this tile)'),
    },
    {
      label: 'Annualized Wheel ROC',
      value: pct(portfolio.annualized_wheel_roc_pct),
      foot: `${pct(portfolio.roi_on_avg_wheel_pct)} over ${portfolio.days_span} days, option P/L only, excludes stock P/L`,
      tone: (portfolio.annualized_wheel_roc_pct ?? 0) >= 0 ? 'pos' : 'neg',
      formula:
        portfolio.annualized_wheel_roc_pct === null
          ? formula([
              'Annualized Wheel ROC (return on capital) =',
              '  (Wheel option P/L ÷ Time-weighted avg capital) × (365 ÷ Days)',
              '',
              'N/A -- no capital has been committed in this window yet.',
            ])
          : formula([
              ...wheelOptionPlFormula(portfolio, 'Wheel option P/L').split('\n'),
              '',
              'Annualized Wheel ROC (return on capital) =',
              '  (Wheel option P/L ÷ Time-weighted avg capital) × (365 ÷ Days)',
              '  Avg capital excludes days at $0 committed; never includes stock P/L.',
              '',
              `= (${money(portfolio.option_realized_pl, { cents: true })} ÷ ${money(portfolio.avg_capital)}) × (365 ÷ ${days})`,
              `= ${pct(portfolio.roi_on_avg_wheel_pct, 2)} × ${scale.toFixed(2)}`,
              `= ${pct(portfolio.annualized_wheel_roc_pct)}`,
            ]),
    },
    {
      label: 'Annualized Active Wheel ROC',
      value: pct(portfolio.annualized_active_wheel_roc_pct),
      foot: `${pct(portfolio.roi_on_avg_active_capital_pct)} over ${portfolio.days_span} days, excludes idle holding-shares capital`,
      tone: (portfolio.annualized_active_wheel_roc_pct ?? 0) >= 0 ? 'pos' : 'neg',
      formula:
        portfolio.annualized_active_wheel_roc_pct === null
          ? formula([
              'Annualized Active Wheel ROC (return on capital) =',
              '  (Wheel option P/L ÷ Time-weighted avg active capital) × (365 ÷ Days)',
              '',
              'N/A -- no capital was actively backing an open put or covered call',
              '  in this window yet.',
            ])
          : formula([
              'Active capital excludes idle holding-shares capital: shares held',
              '  with no covered call currently written against them.',
              '',
              'Annualized Active Wheel ROC (return on capital) =',
              '  (Wheel option P/L ÷ Avg active capital) × (365 ÷ Days)',
              `= (${money(portfolio.option_realized_pl, { cents: true })} ÷ ${money(portfolio.avg_active_capital)}) × (365 ÷ ${days})`,
              `= ${pct(portfolio.annualized_active_wheel_roc_pct)}`,
            ]),
    },
    {
      label: 'Annualized Net Option Yield',
      value: pct(portfolio.annualized_net_option_yield_pct),
      foot: `${pct(portfolio.net_option_yield_pct, 2)} over ${portfolio.days_span} days, vs. initial (not avg) collateral`,
      tone: (portfolio.annualized_net_option_yield_pct ?? 0) >= 0 ? 'pos' : 'neg',
      formula:
        portfolio.annualized_net_option_yield_pct === null
          ? formula([
              'Annualized Net Option Yield =',
              '  (Option P/L ÷ Total initial collateral) × (365 ÷ Days)',
              '',
              'N/A -- no initial collateral committed in this window.',
            ])
          : formula([
              'Denominator is the sum of every cycle\'s own day-one capital,',
              '  added across every cycle -- not a time-weighted average.',
              '',
              'Annualized Net Option Yield =',
              '  (Option P/L ÷ Total initial collateral) × (365 ÷ Days)',
              `= (${money(portfolio.option_realized_pl, { cents: true })} ÷ ${money(portfolio.total_initial_collateral)}) × (365 ÷ ${days})`,
              `= ${pct(portfolio.net_option_yield_pct, 2)} × ${scale.toFixed(2)}`,
              `= ${pct(portfolio.annualized_net_option_yield_pct)}`,
            ]),
    },
    {
      label: 'Annualized Total Position ROI',
      value: pct(portfolio.annualized_total_position_roi_pct),
      foot: `${money(portfolio.dividends_received)} dividends · ${money(portfolio.stock_unrealized_pl)} stock unrealized`,
      tone: (portfolio.annualized_total_position_roi_pct ?? 0) >= 0 ? 'pos' : 'neg',
      formula:
        portfolio.annualized_total_position_roi_pct === null
          ? formula([
              'Annualized Total Position ROI (return on investment) =',
              '  (Option P/L + Stock realized/unrealized P&L + Dividends) ÷ Total initial collateral × (365 ÷ Days)',
              '',
              'N/A -- no initial collateral committed in this window.',
            ])
          : formula([
              'Everything this portfolio has produced -- option P/L, realized AND',
              '  unrealized stock P/L, dividends -- against total day-one capital.',
              '  Open long-option (hedge) unrealized P/L is not included: no',
              '  options-quote feed exists to mark it.',
              '',
              'Annualized Total Position ROI (return on investment) =',
              '  (Option P/L + Stock realized P/L + Stock unrealized P/L + Dividends) ÷ Total initial collateral × (365 ÷ Days)',
              `= (${money(portfolio.option_realized_pl, { cents: true })} + ${money(portfolio.stock_realized_pl, { cents: true })} + ${money(portfolio.stock_unrealized_pl, { cents: true })} + ${money(portfolio.dividends_received, { cents: true })}) ÷ ${money(portfolio.total_initial_collateral)} × (365 ÷ ${days})`,
              `= ${pct(portfolio.total_position_roi_pct, 2)} × ${scale.toFixed(2)}`,
              `= ${pct(portfolio.annualized_total_position_roi_pct)}`,
            ]),
    },
    {
      label: 'Capital deployed',
      value: money(portfolio.capital_deployed_now),
      foot: `${money(portfolio.avg_capital)} avg · ${money(portfolio.peak_capital)} peak`,
      formula: formula([
        'Today =',
        '  Put collateral + Stock cost basis + Call proxy + Long-option debit + Spread collateral',
        `= ${money(portfolio.capital_deployed_now)}`,
        '',
        'Avg = time-weighted mean of the daily total, over the days capital',
        `  was actually committed ($0 days excluded) = ${money(portfolio.avg_capital)}`,
        '',
        `Peak = highest single day's total = ${money(portfolio.peak_capital)}`,
        '',
        `Includes idle holding-shares capital (${money(portfolio.avg_capital - portfolio.avg_active_capital)} of`,
        '  the avg): shares held with no covered call currently written against them.',
      ]),
    },
    {
      // Secondary/diagnostic only -- Annualized Wheel ROC above is the
      // primary performance figure. wins + losses excludes open legs and
      // exact break-evens, so it can (and usually does) undercount total_legs;
      // spelling out "N/M closed" keeps that from reading as a mismatch.
      label: 'Win rate',
      value: pct(portfolio.win_rate_pct, 0),
      foot: `${portfolio.wins}W / ${portfolio.losses}L · ${portfolio.wins + portfolio.losses}/${portfolio.total_legs} closed`,
      formula:
        wl > 0
          ? formula([
              'Win rate = Winning legs ÷ (Winning legs + Losing legs)',
              '  Open legs and exact break-even legs (P/L = $0) are excluded.',
              '',
              `= ${portfolio.wins} ÷ (${portfolio.wins} + ${portfolio.losses})`,
              `= ${pct(portfolio.win_rate_pct, 1)}`,
            ])
          : formula([
              'Win rate = Winning legs ÷ (Winning legs + Losing legs)',
              '',
              'N/A -- no closed leg has a decided (non-zero) P/L yet.',
              'Not shown as 0%, which would wrongly imply losses occurred.',
            ]),
    },
    {
      label: 'Avg days in trade',
      value: portfolio.avg_days_in_trade === null ? '—' : portfolio.avg_days_in_trade.toFixed(1),
      foot: `${portfolio.rolls} rolls · ${portfolio.assignments} assignments`,
      formula: formula([
        'Avg days in trade =',
        "  Σ (cycle's avg days-held × that cycle's decided legs)",
        '  ÷ Σ (decided legs), across every cycle',
        '  "Decided" = closed legs with a win or a loss; open and',
        '  exact break-even legs do not contribute a days-held value.',
      ]),
    },
    {
      label: 'Cycles',
      value: String(portfolio.cycles),
      foot: `${portfolio.active_cycles} active · ${portfolio.tickers} tickers`,
      formula: formula([
        `Cycles = wheel sequences in the current filtered view = ${portfolio.cycles}`,
        `Active = of those, status = ACTIVE = ${portfolio.active_cycles}`,
        `Tickers = distinct underlyings among those cycles = ${portfolio.tickers}`,
      ]),
    },
    {
      label: 'Cash reconciliation',
      value: reconciliation.balanced ? 'Balanced' : 'Mismatch',
      foot: `${reconciliation.rows_checked} rows · delta ${reconciliation.delta}`,
      tone: reconciliation.balanced ? 'pos' : 'neg',
      formula: formula([
        'Balanced = |Δ| < $0.005',
        'Δ = File cash total - Model cash total - Unmatched cash',
        '  (unmatched: closes whose opening leg sits outside this window)',
        '',
        `= ${money(reconciliation.file_cash_total, { cents: true })} - ${money(reconciliation.model_cash_total, { cents: true })} - ${money(reconciliation.unmatched_cash, { cents: true })}`,
        `= ${reconciliation.delta}`,
      ]),
    },
  ];

  for (const tile of tiles) {
    const node = el('div', { class: 'tile' + (tile.primary ? ' primary' : '') });
    node.appendChild(el('div', { class: 'label' }, tile.label));
    node.appendChild(el('div', { class: 'value ' + (tile.tone || '') }, tile.value));
    node.appendChild(el('div', { class: 'foot' }, tile.foot));
    setFormula(node, tile.formula);
    host.appendChild(node);
  }
}

function renderNotices(meta, reconciliation) {
  const host = $('notices');
  clear(host);

  const notice = el('div', { class: 'notice' + (reconciliation.balanced ? ' ok' : '') });
  notice.appendChild(
    el(
      'strong',
      {},
      reconciliation.balanced
        ? 'Option cash matches the broker file exactly.'
        : 'Option cash does not reconcile against the broker file.'
    )
  );
  const list = el('ul');
  list.appendChild(
    el(
      'li',
      {},
      `${reconciliation.rows_checked} priced rows re-derived from price × quantity - fees; ` +
        `${reconciliation.row_failures.length} mismatches. Modelled cash ` +
        `${money(reconciliation.model_cash_total, { cents: true })} vs file ` +
        `${money(reconciliation.file_cash_total, { cents: true })}.`
    )
  );
  list.appendChild(el('li', {}, reconciliation.note));

  if (meta.combined) {
    const detail = meta.sources
      .map((source) => `${source.name} (${source.kept} kept, ${source.duplicates} duplicate)`)
      .join('; ');
    list.appendChild(
      el(
        'li',
        {},
        `Combined ${meta.sources.length} exports: ${meta.rows_parsed} rows in, ` +
          `${meta.rows_kept} after merging ${meta.duplicates_removed} trades that appear in more ` +
          `than one file — ${detail}.`
      )
    );
  }

  for (const warning of meta.parse_warnings || []) {
    if (/transposed|newest-first/.test(warning)) {
      list.appendChild(el('li', {}, warning));
    }
  }
  if (meta.unmatched_closes && meta.unmatched_closes.length) {
    list.appendChild(
      el(
        'li',
        {},
        `${meta.unmatched_closes.length} closing rows had no opening leg inside this window ` +
          `(${money(reconciliation.unmatched_cash, { cents: true })} of cash), counted separately.`
      )
    );
  }
  notice.appendChild(list);
  host.appendChild(notice);
}

/* ------------------------------------------------------ net worth & benchmark */

function renderNetWorthTiles(netWorth, benchmark, wheelReturn) {
  const host = $('networth-tiles');
  clear(host);

  // The combined view nests per-account totals under `.combined`; a single
  // account's payload already has these fields at the top level.
  const totals = netWorth.combined || netWorth;

  // Total value and Cash are read verbatim off the broker's Positions
  // snapshot -- nothing computed to show a formula for, but four similarly-
  // named dollar figures (this pair, plus True capital deployed here and the
  // portfolio's own Capital deployed tile above) need a foot line each so a
  // reader isn't left guessing which is which.
  const tiles = [
    {
      label: 'Total value',
      value: money(totals.total_value, { cents: true }),
      foot: 'Whole account, from the broker\'s own Positions snapshot -- cash + every holding, wheeled or not.',
    },
    {
      label: 'Cash',
      value: money(totals.cash_total, { cents: true }),
      foot: 'Uninvested cash sitting in the account right now -- part of Total value, not deployed anywhere.',
    },
    {
      label: 'True capital deployed',
      value: money(totals.wheel_capital_deployed),
      foot: `of ${money(totals.total_value)} total value -- the rest is cash or buy-and-hold`,
      formula: formula([
        "Today's committed wheel capital =",
        '  Put collateral + Stock cost basis + Call proxy + Long-option debit + Spread collateral',
        `= ${money(totals.wheel_capital_deployed)}`,
        '',
        "Dated to the broker's Positions export (the as-of date), which can",
        '  trail a few days behind the latest transaction on file.',
      ]),
    },
  ];

  // Independent of the SPY replay below -- no Positions/Yahoo price data
  // needed, just the wheel's own transaction history -- so it renders
  // whenever it has enough of its own activity, whether or not the
  // whole-account benchmark comparison below is available.
  if (wheelReturn && wheelReturn.available) {
    const events = wheelReturn.cash_flow_events || [];
    const span = events.length ? `${events[0].date} → ${events[events.length - 1].date}` : '—';
    const wb = wheelReturn.benchmark;
    tiles.push({
      label: 'Wheel-only return (XIRR)',
      value: pct(wheelReturn.xirr_pct, 1),
      foot: `${span} · ${money(wheelReturn.terminal_value)} still committed`,
      tone: (wheelReturn.xirr_pct ?? 0) >= 0 ? 'pos' : 'neg',
      formula: formula([
        'XIRR (money-weighted annualized return): the single rate that makes',
        '  every dated cash flow -- each option-leg open/close, each wheel-',
        '  active share purchase/sale -- discount to zero against the',
        '  capital still committed today.',
        `${events.length} events, ${span} = ${pct(wheelReturn.xirr_pct, 1)}`,
        '',
        'Not risk-adjusted; fast turnover inflates this vs. buy-and-hold.',
        'Spreads not netted; dividends excluded.',
      ]),
    });

    if (wb && wb.xirr_pct !== null && wb.xirr_pct !== undefined) {
      tiles.push(
        {
          label: 'Wheel vs. S&P 500 (XIRR)',
          value: pct(wb.xirr_pct, 1),
          tone: (wb.xirr_pct ?? 0) >= 0 ? 'pos' : 'neg',
          formula: formula([
            'The wheel\'s own option-leg opens/closes and share buys/sells,',
            '  replayed into SPY (an S&P 500 index fund) shares priced on',
            `  each date instead, then valued at SPY's price on ${wheelReturn.as_of}`,
            `  -- terminal value ${money(wb.terminal_value)}.`,
            '',
            'Answers "did the wheel itself beat buy-and-hold SPY," isolated',
            '  from whatever else -- other ETFs, other stock -- sits in this account.',
          ]),
        },
        {
          label: 'Wheel value added vs. S&P 500',
          value: money(wheelReturn.value_added, { cents: true, sign: true }),
          foot: `${money(wheelReturn.terminal_value)} wheel vs ${money(wb.terminal_value)} in SPY`,
          tone: (wheelReturn.value_added ?? 0) >= 0 ? 'pos' : 'neg',
          formula: formula([
            'Value added = Wheel terminal value - SPY terminal value',
            '  (same cash-flow timing replayed into both, so this isolates',
            '  strategy performance from when money happened to move)',
            '',
            `= ${money(wheelReturn.terminal_value)} - ${money(wb.terminal_value)}`,
            `= ${money(wheelReturn.value_added, { sign: true })}`,
          ]),
        }
      );
    }
  }

  if (benchmark.available) {
    const events = benchmark.cash_flow_events || [];
    const span = events.length ? `${events[0].date} → ${events[events.length - 1].date}` : '—';
    // The S&P 500 benchmark and Value-added tiles that used to sit here were
    // removed: both were computed almost entirely from data/accounts.json's
    // manually-typed "opening_balances" guess (no real Positions snapshot
    // exists that far back), so their precision was fake -- see the
    // Wheel-only tiles above for a same-question comparison built from real
    // transaction history instead of a guessed starting balance.
    tiles.push({
      label: 'Actual return (XIRR)',
      value: pct(benchmark.actual.xirr_pct, 1),
      tone: (benchmark.actual.xirr_pct ?? 0) >= 0 ? 'pos' : 'neg',
      formula: formula([
        'Money-weighted return (XIRR): the annualized rate r solving',
        '  Σ amount_i ÷ (1 + r)^((date_i - date_0) / 365) = 0',
        `  over ${events.length} cash-flow event(s) -- the opening balance`,
        '  plus every external deposit/withdrawal found in the transaction',
        `  history, ${span} -- valued against the account's`,
        `  terminal value of ${money(benchmark.actual.terminal_value)} on ${benchmark.as_of}.`,
      ]),
    });
  }

  for (const tile of tiles) {
    const node = el('div', { class: 'tile' });
    node.appendChild(el('div', { class: 'label' }, tile.label));
    node.appendChild(el('div', { class: 'value ' + (tile.tone || '') }, tile.value));
    if (tile.foot) node.appendChild(el('div', { class: 'foot' }, tile.foot));
    setFormula(node, tile.formula);
    host.appendChild(node);
  }
}

/**
 * Actual account value vs. a same-timing SPY benchmark, one point per
 * Portfolio Positions snapshot -- deliberately sparse, so every point gets an
 * explicit marker rather than relying on the line alone to be readable.
 */
function drawNetWorthChart(benchmark) {
  const svg = $('chart-networth');
  const legend = $('legend-networth');
  clear(legend);

  const series = (benchmark.series || []).filter((point) => point.actual_value !== null);
  if (!series.length) {
    clear(svg);
    buildTable('networth-table', ['Date', 'Actual value', 'If held in SPY instead'], []);
    return;
  }

  const lines = [
    { key: 'actual_value', label: 'Actual account value', varName: '--series-1' },
    { key: 'benchmark_value', label: 'If held in SPY instead', varName: '--series-2' },
  ];
  const colors = lines.map((line) => cssVar(line.varName));

  const values = series.flatMap((point) => lines.map((line) => point[line.key]).filter((v) => v !== null));
  const margin = { top: 14, right: 58, bottom: 30, left: 62 };
  const width = chartWidth(svg);
  const height = 260;
  const yMin = Math.min(0, ...values) * 0.98;
  const yMax = Math.max(...values) * 1.08 || 1;

  const { group, plotWidth, plotHeight, y } = frame(svg, { width, height, margin, yMin, yMax });

  const times = series.map((point) => parseDay(point.as_of).getTime());
  const [tMin, tMax] = [times[0], times[times.length - 1]];
  const x = (iso) =>
    margin.left +
    (tMax === tMin ? plotWidth / 2 : ((parseDay(iso).getTime() - tMin) / (tMax - tMin)) * plotWidth);

  lines.forEach((line, index) => {
    const points = series.filter((point) => point[line.key] !== null);
    if (points.length > 1) {
      const path = points.map((point) => `${x(point.as_of)},${y(point[line.key])}`).join('L');
      group.appendChild(
        svgEl('path', {
          d: 'M' + path,
          fill: 'none',
          stroke: colors[index],
          'stroke-width': 2,
          'stroke-linejoin': 'round',
          'stroke-linecap': 'round',
        })
      );
    }
    for (const point of points) {
      group.appendChild(
        svgEl('circle', { cx: x(point.as_of), cy: y(point[line.key]), r: 3.5, fill: colors[index] })
      );
    }
    const last = points[points.length - 1];
    if (last) {
      group.appendChild(
        svgEl(
          'text',
          { x: x(last.as_of) + 7, y: y(last[line.key]) + 3.5, fill: 'var(--text-secondary)', 'font-weight': 600 },
          compactMoney(last[line.key])
        )
      );
    }
  });

  timeAxis(group, series.map((point) => point.as_of), x, margin.top + plotHeight, plotWidth);

  const dots = lines.map((_, index) =>
    group.appendChild(
      svgEl('circle', { r: 5, fill: colors[index], stroke: cssVar('--surface-1'), 'stroke-width': 2, opacity: 0 })
    )
  );

  crosshairLayer(svg, group, { margin, plotWidth, plotHeight, width }, series, (p) => x(p.as_of), {
    label: 'Actual account value vs. a same-timing SPY benchmark, one point per snapshot.',
    onIndex: (index, at) => {
      const point = series[index];
      dots.forEach((dot, i) => {
        const value = point[lines[i].key];
        if (value === null) {
          dot.setAttribute('opacity', 0);
          return;
        }
        dot.setAttribute('cx', x(point.as_of));
        dot.setAttribute('cy', y(value));
        dot.setAttribute('opacity', 1);
      });
      showTooltip(
        at,
        longDate(point.as_of),
        lines.map((line, i) => ({
          label: line.label,
          value: point[line.key] === null ? '—' : money(point[line.key], { cents: true }),
          color: colors[i],
        })),
        formula([
          'Actual account value is read verbatim off the Positions snapshot.',
          '',
          '"If held in SPY instead" replays the account\'s opening balance plus',
          '  every external deposit/withdrawal, on those same dates, into SPY',
          "  (an S&P 500 index fund) shares priced then, valued at SPY's",
          '  price here.',
        ])
      );
    },
    onLeave: () => dots.forEach((dot) => dot.setAttribute('opacity', 0)),
  });

  lines.forEach((line, i) => {
    const item = el('span');
    const swatch = el('i', { class: 'line' });
    swatch.style.background = colors[i];
    item.appendChild(swatch);
    item.appendChild(document.createTextNode(line.label));
    legend.appendChild(item);
  });

  buildTable(
    'networth-table',
    ['Date', 'Actual value', 'If held in SPY instead'],
    series.map((point) => [
      point.as_of,
      money(point.actual_value, { cents: true }),
      point.benchmark_value === null ? '—' : money(point.benchmark_value, { cents: true }),
    ])
  );
}

function renderNetWorth(netWorth, benchmark, wheelReturn) {
  const card = $('net-worth-card');
  const empty = $('net-worth-empty');

  if (!netWorth || !netWorth.available) {
    card.hidden = false;
    empty.hidden = false;
    clear(empty);
    empty.appendChild(el('strong', {}, 'No net worth data yet. '));
    empty.appendChild(
      document.createTextNode(
        (netWorth && netWorth.warnings && netWorth.warnings[0]) ||
          'Drop a Fidelity Portfolio Positions export into an account folder under data/ to enable this section.'
      )
    );
    $('networth-tiles').replaceChildren();
    clear($('chart-networth'));
    clear($('legend-networth'));
    return;
  }

  card.hidden = false;
  empty.hidden = true;
  renderNetWorthTiles(netWorth, benchmark, wheelReturn);

  if (benchmark.available) {
    drawNetWorthChart(benchmark);
  } else {
    clear($('chart-networth'));
    const note = el(
      'div',
      { class: 'hint-inline' },
      (benchmark.warnings && benchmark.warnings[0]) ||
        'Add another Portfolio Positions snapshot on a different date to unlock the benchmark comparison.'
    );
    clear($('legend-networth'));
    $('legend-networth').appendChild(note);
    buildTable('networth-table', ['Note'], [[note.textContent]]);
  }
}

/* ------------------------------------------------------------- data source */

function sourceStatus(text, tone = '') {
  const host = $('source-status');
  host.className = 'source-status ' + tone;
  host.textContent = text;
}

/** Populate the export picker, ticking whichever are currently loaded. */
function renderDatasets(listing) {
  const host = $('dataset-list');
  clear(host);

  const unsupported = listing.unsupported_datasets || [];

  if (!listing.datasets.length && !unsupported.length) {
    host.appendChild(el('div', { class: 'hint-inline' }, 'No Fidelity exports found.'));
    return;
  }

  for (const dataset of listing.datasets) {
    const chip = el('label', { class: 'dataset-chip' + (dataset.active ? ' on' : '') });
    const box = el('input', { type: 'checkbox', value: dataset.path });
    box.checked = Boolean(dataset.active);
    box.addEventListener('change', () => chip.classList.toggle('on', box.checked));
    chip.appendChild(box);
    chip.appendChild(
      document.createTextNode((dataset.folder === '.' ? '' : dataset.folder + '/') + dataset.name)
    );
    chip.appendChild(el('span', { class: 'meta' }, `${dataset.size_kb} KB`));
    host.appendChild(chip);
  }

  // Found on disk but not (yet) supported -- shown as a plain, unselectable
  // note so the file doesn't just silently never appear with no explanation.
  for (const dataset of unsupported) {
    const name = (dataset.folder === '.' ? '' : dataset.folder + '/') + dataset.name;
    host.appendChild(
      el('div', { class: 'dataset-unsupported' }, `${name} (${dataset.size_kb} KB) — ${dataset.reason}`)
    );
  }
}

const selectedDatasets = () =>
  [...document.querySelectorAll('#dataset-list input:checked')].map((box) => box.value);

async function refreshDatasets() {
  try {
    const response = await fetch('/api/datasets');
    if (response.ok) renderDatasets(await response.json());
  } catch (error) {
    sourceStatus('Could not list exports: ' + error.message, 'err');
  }
}

/* ------------------------------------------------------------- accounts */

/**
 * One data/<account>/ folder = one brokerage account's own files. "Combined"
 * aggregates every account without merging their cycles (see wheel/accounts.py).
 * Single-select, unlike the ticker/status chips -- switching accounts changes
 * which dataset is loaded, so it reloads rather than filtering in place.
 */
function renderAccountChips(accounts) {
  const bar = $('accounts-bar');
  const host = $('account-chips');
  clear(host);

  // The currently selected account may no longer exist (a folder removed,
  // an account ignored via a config edit, ...) -- fall back to Combined
  // rather than stay silently pointed at something gone, which would keep
  // sending a doomed ?account=<stale-id> on every future load(). Checked
  // against the known ids, not just "is the switcher shown": a single
  // remaining account legitimately named via default_account must not be
  // reset out from under a page that never had more than one to switch
  // between in the first place.
  const knownIds = new Set(accounts.map((a) => a.id));
  if (state.account !== COMBINED_ACCOUNT_ID && !knownIds.has(state.account)) {
    state.account = COMBINED_ACCOUNT_ID;
  }

  if (accounts.length < 2) {
    bar.hidden = true;
    return;
  }
  bar.hidden = false;

  const options = [
    { id: COMBINED_ACCOUNT_ID, label: 'Combined' },
    ...accounts.map((a) => ({ id: a.id, label: a.label })),
  ];
  for (const option of options) {
    const chip = el('button', { class: 'chip', type: 'button' }, option.label);
    chip.setAttribute('aria-pressed', state.account === option.id ? 'true' : 'false');
    chip.addEventListener('click', () => {
      if (state.account === option.id) return;
      state.account = option.id;
      resetSelectionState();
      renderAccountChips(accounts);
      load();
    });
    host.appendChild(chip);
  }
}

// Applied at most once, on the page's first /api/accounts response -- a
// later refreshAccounts() call (after an upload, say) must never yank the
// user back to data/accounts.json's default_account/default_range if
// they've since picked something else. default_range can't be applied here
// yet -- applyPreset() needs state.data.meta, which only exists after the
// first /api/dashboard response -- so this just remembers it for load() to
// apply once that first response lands.
let defaultAccountApplied = false;
let defaultRangeToApply = null;
let defaultRangeApplied = false;

async function refreshAccounts() {
  try {
    const response = await fetch('/api/accounts');
    if (!response.ok) return;
    const listing = await response.json();
    if (!defaultAccountApplied) {
      if (listing.default_account) state.account = listing.default_account;
      defaultRangeToApply = listing.default_range || null;
      defaultAccountApplied = true;
    }
    renderAccountChips(listing.accounts || []);
  } catch (error) {
    // Non-fatal: the dashboard still works against whatever account is active.
  }
}

/**
 * Load whatever the user has chosen, on an explicit button press.
 *
 * Newly picked files are uploaded and validated server-side; otherwise the
 * ticked exports are combined. Several files are sent as one length-prefixed
 * bundle described by an X-Files manifest, which keeps the server free of a
 * multipart parser. Anything that fails to parse is rejected without replacing
 * the dataset currently on screen.
 */
async function loadSelectedSource() {
  const fileInput = $('csv-file');
  const button = $('load-data');
  const files = [...(fileInput.files || [])];
  const chosenPaths = selectedDatasets();

  button.disabled = true;
  sourceStatus(files.length ? `Uploading ${files.length} file(s)…` : 'Combining…', 'busy');

  try {
    let response;
    if (files.length) {
      const buffers = await Promise.all(files.map((file) => file.arrayBuffer()));
      const manifest = files.map((file, index) => ({
        name: file.name,
        size: buffers[index].byteLength,
      }));
      const bundle = new Uint8Array(buffers.reduce((total, buf) => total + buf.byteLength, 0));
      let offset = 0;
      for (const buffer of buffers) {
        bundle.set(new Uint8Array(buffer), offset);
        offset += buffer.byteLength;
      }
      response = await fetch('/api/upload', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/octet-stream',
          'X-Files': JSON.stringify(manifest),
          // Ticked exports stay in the set, so uploading adds to the picture
          // rather than replacing it.
          'X-Keep-Current': chosenPaths.length ? 'true' : 'false',
          // A specific account selected in the switcher -> its own folder
          // under data/, never the default bucket. "Combined" has no single
          // folder to target, so it falls back to the default account, same
          // as today's behavior.
          ...(state.account && state.account !== COMBINED_ACCOUNT_ID ? { 'X-Account': state.account } : {}),
        },
        body: bundle,
      });
    } else if (chosenPaths.length) {
      response = await fetch('/api/select', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ paths: chosenPaths }),
      });
    } else {
      sourceStatus('Tick at least one export, or choose a CSV file.', 'err');
      return;
    }

    const result = await response.json();
    if (!response.ok) {
      sourceStatus('Rejected: ' + (result.error || 'could not read that file'), 'err');
      return;
    }

    // An account-scoped upload (X-Account) returns {message, account,
    // accounts} rather than the default account's dataset listing -- nothing
    // in #dataset-list changes when a *different* account's folder gets a
    // new file, so it's only re-rendered when the response actually is one.
    if (result.datasets) renderDatasets(result);
    fileInput.value = '';
    // A new account has different tickers and dates, so start from a clean slice.
    resetSelectionState();

    await refreshAccounts();
    await load();
    sourceStatus(result.message || 'Loaded.', 'ok');
  } catch (error) {
    sourceStatus('Load failed: ' + error.message, 'err');
  } finally {
    button.disabled = false;
  }
}

/* ------------------------------------------------------------------ filters */

/**
 * Back to the unfiltered view: no ticker/status/date selection, nothing
 * expanded, the preset select and date inputs reset to match. Shared by the
 * account switcher, a new upload, and the Reset button -- all three mean
 * "the previous selection may no longer make sense, start clean," including
 * the date range: a ticker or status picked under one account may not exist
 * under another, and a custom date range from one account's history can
 * just as easily fall entirely outside another's.
 */
function resetSelectionState() {
  state.tickers.clear();
  state.statuses.clear();
  state.start = null;
  state.end = null;
  state.expanded.clear();
  $('preset').value = 'all';
  $('start').value = '';
  $('end').value = '';
}

function renderChips(hostId, values, selected, onToggle) {
  const host = $(hostId);
  clear(host);
  for (const value of values) {
    const chip = el('button', { class: 'chip', type: 'button' }, value);
    chip.setAttribute('aria-pressed', selected.has(value) ? 'true' : 'false');
    chip.addEventListener('click', () => onToggle(value));
    host.appendChild(chip);
  }
}

const PRESET_YEARS = { '1y': 1, '3y': 3, '5y': 5 };

function applyPreset(preset) {
  const meta = state.data.meta;
  const last = parseDay(meta.data_last_date);
  if (preset === 'all') {
    state.start = null;
    state.end = null;
  } else if (preset === 'ytd') {
    state.start = localIso(new Date(last.getFullYear(), 0, 1).getTime());
    state.end = meta.data_last_date;
  } else if (preset in PRESET_YEARS) {
    const from = new Date(last);
    from.setFullYear(from.getFullYear() - PRESET_YEARS[preset]);
    state.start = localIso(from.getTime());
    state.end = meta.data_last_date;
  } else if (preset.startsWith('year:')) {
    // A specific calendar year, e.g. "year:2025" -> Jan 1 through Dec 31
    // (or the last available data date, if that year is still in progress).
    const year = Number(preset.slice('year:'.length));
    const yearEnd = new Date(year, 11, 31);
    state.start = localIso(new Date(year, 0, 1).getTime());
    state.end = yearEnd.getTime() < last.getTime() ? localIso(yearEnd.getTime()) : meta.data_last_date;
  } else if (preset !== 'custom') {
    // A bare day count, e.g. "10" for the last 10 days.
    const from = new Date(last);
    from.setDate(from.getDate() - Number(preset));
    state.start = localIso(from.getTime());
    state.end = meta.data_last_date;
  }
  $('start').value = state.start || '';
  $('end').value = state.end || '';
}

function renderYearPresets(meta) {
  const select = $('preset');
  const current = select.value;
  // Re-derived from the loaded data on every render, so drop whatever was
  // injected last time before adding this run's years.
  select.querySelectorAll('option[data-year-preset]').forEach((opt) => opt.remove());
  const customOption = select.querySelector('option[value="custom"]');
  const firstYear = parseDay(meta.data_first_date).getFullYear();
  const lastYear = parseDay(meta.data_last_date).getFullYear();
  for (let year = lastYear; year >= firstYear; year--) {
    const option = el('option', { value: `year:${year}`, 'data-year-preset': 'true' }, String(year));
    select.insertBefore(option, customOption);
  }
  select.value = current;
}

function wireFilters() {
  $('load-data').addEventListener('click', loadSelectedSource);
  // Picking a file arms the button but does not evaluate anything yet.
  $('csv-file').addEventListener('change', (event) => {
    const files = [...(event.target.files || [])];
    if (!files.length) {
      sourceStatus('');
    } else {
      const names = files.map((file) => file.name).join(', ');
      sourceStatus(`${files.length} file(s) ready — press Load to evaluate: ${names}`);
    }
  });

  $('preset').addEventListener('change', (event) => {
    applyPreset(event.target.value);
    if (event.target.value !== 'custom') load();
  });
  $('start').addEventListener('change', (event) => {
    state.start = event.target.value || null;
    $('preset').value = 'custom';
    load();
  });
  $('end').addEventListener('change', (event) => {
    state.end = event.target.value || null;
    $('preset').value = 'custom';
    load();
  });
  $('reset').addEventListener('click', () => {
    resetSelectionState();
    load();
  });

  // Re-expresses one chart, so it redraws that chart rather than refetching.
  $('capital-mode').addEventListener('click', (event) => {
    const on = event.currentTarget.getAttribute('aria-pressed') === 'true';
    event.currentTarget.setAttribute('aria-pressed', on ? 'false' : 'true');
    state.capitalMode = on ? 'value' : 'share';
    if (state.data) drawCapital(state.data.capital_series, state.data.net_worth);
  });

  document.querySelectorAll('.toggle[data-twin]').forEach((button) => {
    button.addEventListener('click', () => {
      const twin = $(button.dataset.twin);
      const showing = button.getAttribute('aria-pressed') === 'true';
      button.setAttribute('aria-pressed', showing ? 'false' : 'true');
      twin.hidden = showing;
    });
  });

  document.querySelectorAll('.tab').forEach((button) => {
    button.addEventListener('click', () => switchTab(button.dataset.tab));
  });
  $('tradelog-ticker').addEventListener('change', (event) => {
    state.tradeLogTicker = event.target.value || null;
    // The current wheel may not belong to the new ticker; renderTradeLog
    // re-resolves the selection against the narrowed list.
    state.tradeLogCycleId = null;
    renderTradeLog();
  });
  $('tradelog-pick').addEventListener('change', (event) => {
    state.tradeLogCycleId = event.target.value || null;
    renderTradeLog();
  });
  $('tradelog-prev').addEventListener('click', () => stepTradeLog(-1));
  $('tradelog-next').addEventListener('click', () => stepTradeLog(1));

  $('theme-toggle').addEventListener('click', () => {
    const root = document.documentElement;
    const isDark =
      root.dataset.theme === 'dark' ||
      (!root.dataset.theme && window.matchMedia('(prefers-color-scheme: dark)').matches);
    root.dataset.theme = isDark ? 'light' : 'dark';
    render(); // re-read CSS custom properties for the new mode
  });

  window.addEventListener('resize', debounce(render, 180));
}

function debounce(fn, wait) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), wait);
  };
}

/* -------------------------------------------------------------------- load */

// Bumped on every load() call; a response is only applied if it's still the
// most recent one in flight when it resolves. Without this, two overlapping
// fetches (e.g. two account-chip clicks in quick succession) can resolve out
// of order and let a stale, slower response overwrite a newer one on screen.
let loadGeneration = 0;

async function load() {
  const generation = ++loadGeneration;
  // The very first load() attempt gets one shot at applying
  // data/accounts.json's default_range, win or lose -- marked spent right
  // away (not only on success) so a later, unrelated load() (e.g. the user
  // picking a filter after the first attempt merely failed on the network)
  // is never mistaken for "the first response" and made to silently clobber
  // whatever the user just chose.
  const isFirstAttempt = !defaultRangeApplied;
  defaultRangeApplied = true;

  const params = new URLSearchParams();
  if (state.account && state.account !== COMBINED_ACCOUNT_ID) params.set('account', state.account);
  if (state.tickers.size) params.set('tickers', [...state.tickers].join(','));
  if (state.statuses.size) params.set('status', [...state.statuses].join(','));
  if (state.start) params.set('start', state.start);
  if (state.end) params.set('end', state.end);

  // Hold the previous render at reduced opacity -- no skeleton, no layout jump.
  document.querySelector('.wrap').classList.add('loading');
  let succeeded = false;
  try {
    const response = await fetch('/api/dashboard?' + params.toString());
    if (!response.ok) throw new Error('HTTP ' + response.status);
    const data = await response.json();
    if (generation !== loadGeneration) return; // superseded by a newer load()
    state.data = data;
    render();
    succeeded = true;
  } catch (error) {
    if (generation !== loadGeneration) return; // superseded by a newer load()
    const host = $('notices');
    clear(host);
    const notice = el('div', { class: 'notice' });
    notice.appendChild(el('strong', {}, 'Could not load data'));
    notice.appendChild(document.createTextNode(' ' + error.message));
    host.appendChild(notice);
  } finally {
    if (generation === loadGeneration) document.querySelector('.wrap').classList.remove('loading');
  }

  // data/accounts.json's default_range, applied once the very first
  // dashboard response has told us meta.data_last_date -- applyPreset()
  // needs it, so this can't happen any earlier than here. Runs as a second,
  // independent load() rather than inline, so the loading-state add/remove
  // above stays correctly paired for both fetches.
  if (isFirstAttempt && succeeded && defaultRangeToApply) {
    applyPreset(defaultRangeToApply);
    $('preset').value = defaultRangeToApply;
    await load();
  }
}

/* --------------------------------------------------------------- trade log */

/**
 * Whether a wheel is shown given the dashboard's date-range filter: it overlaps
 * the window, or it is still open (an open wheel always shows, even one opened
 * long before a 10-day window). Ignores the Trade Log's own ticker select.
 */
function tradeLogInWindow(wheel) {
  const filters = (state.data && state.data.meta && state.data.meta.filters) || {};
  const start = filters.start || null;
  const end = filters.end || null;
  if (wheel.is_open) return true;
  if (!start && !end) return true;
  const closed = wheel.end_date || '9999-12-31';
  if (start && closed < start) return false; // wheel ended before the window
  if (end && wheel.start_date > end) return false; // wheel started after it
  return true;
}

/**
 * Wheels for the picker / prev-next, newest-started first: the ones in the
 * date window, then narrowed to one ticker if the Trade Log's ticker select
 * is set.
 */
function orderedTradeLog() {
  const wheels = (state.data && state.data.trade_log && state.data.trade_log.wheels) || [];
  const ticker = state.tradeLogTicker || null;
  return wheels
    .filter((wheel) => (!ticker || wheel.underlying === ticker) && tradeLogInWindow(wheel))
    .sort((a, b) =>
      a.start_date < b.start_date ? 1 : a.start_date > b.start_date ? -1 : a.cycle_id.localeCompare(b.cycle_id)
    );
}

/** Tickers that have at least one wheel displayable under the date window, sorted. */
function tradeLogTickers() {
  const wheels = (state.data && state.data.trade_log && state.data.trade_log.wheels) || [];
  return [...new Set(wheels.filter(tradeLogInWindow).map((wheel) => wheel.underlying))].sort();
}

function renderTabs() {
  const onTradelog = state.activeTab === 'tradelog';
  $('tab-dashboard').hidden = onTradelog;
  $('tab-tradelog').hidden = !onTradelog;
  document
    .querySelectorAll('.tab')
    .forEach((btn) => btn.setAttribute('aria-selected', btn.dataset.tab === state.activeTab ? 'true' : 'false'));
}

function switchTab(name) {
  state.activeTab = name;
  renderTabs();
  if (name === 'tradelog') renderTradeLog();
}

/** Click-through from the Dashboard's Wheel-timelines chart. */
function openTradeLog(cycleId) {
  state.tradeLogCycleId = cycleId;
  state.tradeLogTicker = null; // don't let a stale ticker filter hide the clicked wheel
  switchTab('tradelog');
}

function stepTradeLog(delta) {
  const wheels = orderedTradeLog();
  const index = wheels.findIndex((w) => w.cycle_id === state.tradeLogCycleId);
  const next = index < 0 ? (delta > 0 ? 0 : wheels.length - 1) : index + delta;
  if (next < 0 || next >= wheels.length) return;
  state.tradeLogCycleId = wheels[next].cycle_id;
  renderTradeLog();
}

function tradeLogCell(label, value, { help, foot, tone } = {}) {
  const cell = el('div', { class: 'tl-cell' });
  cell.appendChild(el('div', { class: 'tl-label' }, label));
  // An array value stacks each part on its own line (e.g. a date range), so a
  // long value never wraps mid-token across two lines. `tone` ('pos'|'neg')
  // colours the value.
  const valueNode = el('div', {
    class: 'tl-value' + (help ? ' help' : '') + (tone ? ' ' + tone : ''),
  });
  for (const part of Array.isArray(value) ? value : [value]) {
    valueNode.appendChild(el('div', {}, part));
  }
  if (help) setFormula(valueNode, help);
  cell.appendChild(valueNode);
  if (foot) cell.appendChild(el('div', { class: 'tl-foot' }, foot));
  return cell;
}

/**
 * A vertical waterfall: y is the running money value, x is each action, read
 * left to right -- the first action (Premium sold) is the leftmost column,
 * "P&L now" is the rightmost. `step` bars float between consecutive running
 * totals (green up / red down); `subtotal` and `total` bars rise from $0.
 * Data comes from `entry.pl_bridge` -- see `_trade_log_entry` in wheel/api.py.
 */
function drawTradeLogBridge(entry) {
  const host = $('tradelog-bridge');
  clear(host);
  const steps = entry.pl_bridge || [];
  if (steps.length < 2) {
    host.hidden = true;
    return;
  }
  host.hidden = false;

  const svg = svgEl('svg', { role: 'img' });
  host.appendChild(svg);

  const n = steps.length;
  const margin = { top: 18, right: 14, bottom: 78, left: 62 };
  const width = chartWidth(svg, Math.max(520, n * 96));
  const height = 300;
  const colW = (width - margin.left - margin.right) / n;
  const barW = Math.min(48, colW * 0.58);
  svg.setAttribute('width', width);
  svg.setAttribute('height', height);
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);

  const runs = steps.map((s) => s.running);
  let lo = Math.min(0, ...runs);
  let hi = Math.max(0, ...runs);
  const pad = (hi - lo) * 0.14 || 1;
  lo -= pad;
  hi += pad;
  const y = (v) => margin.top + (1 - (v - lo) / (hi - lo)) * (height - margin.top - margin.bottom);
  const colX = (j) => margin.left + (j + 0.5) * colW;

  const g = svgEl('g');
  svg.appendChild(g);
  const green = cssVar('--good');
  const red = cssVar('--critical');
  // Accounting style: no leading + on gains, negatives wrapped in parentheses.
  const acctMoney = (v) => (v < 0 ? `(${compactMoney(-v)})` : compactMoney(v));
  const acctMoneyFull = (v) => (v < 0 ? `(${money(-v)})` : money(v));

  for (let i = 0; i <= 4; i += 1) {
    const v = lo + ((hi - lo) * i) / 4;
    const gy = y(v);
    g.appendChild(
      svgEl('line', {
        x1: margin.left,
        x2: width - margin.right,
        y1: gy,
        y2: gy,
        stroke: cssVar('--gridline'),
        'stroke-width': 1,
      })
    );
    g.appendChild(
      svgEl(
        'text',
        { x: margin.left - 8, y: gy + 4, 'text-anchor': 'end', fill: 'var(--text-muted)', 'font-variant-numeric': 'tabular-nums' },
        compactMoney(v)
      )
    );
  }
  g.appendChild(
    svgEl('line', {
      x1: margin.left,
      x2: width - margin.right,
      y1: y(0),
      y2: y(0),
      stroke: cssVar('--axis'),
      'stroke-width': 1.5,
    })
  );

  steps.forEach((step, j) => {
    const cx = colX(j);
    const anchor = step.kind !== 'step';
    const from = anchor ? 0 : step.running - step.delta;
    const to = step.running;
    const top = Math.min(y(from), y(to));
    const bot = Math.max(y(from), y(to));
    const up = anchor ? step.running >= 0 : step.delta >= 0;

    // connector from the previous action's level (that column is to the LEFT)
    if (j > 0) {
      const ry = y(steps[j - 1].running);
      g.appendChild(
        svgEl('line', {
          x1: colX(j - 1) + barW / 2,
          x2: cx - barW / 2,
          y1: ry,
          y2: ry,
          stroke: cssVar('--text-muted'),
          'stroke-width': 1,
          'stroke-dasharray': '2 2',
        })
      );
    }

    g.appendChild(
      svgEl('rect', {
        x: cx - barW / 2,
        y: top,
        width: barW,
        height: Math.max(2, bot - top),
        rx: 2,
        fill: up ? green : red,
        'fill-opacity': anchor ? (step.kind === 'total' ? 0.95 : 0.55) : 0.85,
        stroke: anchor ? (up ? green : red) : 'none',
        'stroke-width': step.kind === 'total' ? 1.5 : 1,
      })
    );

    const valueText = anchor ? acctMoneyFull(step.running) : acctMoney(step.delta);
    g.appendChild(
      svgEl(
        'text',
        {
          x: cx,
          y: up ? top - 6 : bot + 13,
          'text-anchor': 'middle',
          fill: up ? 'var(--success-text)' : 'var(--critical)',
          'font-weight': anchor ? 700 : 500,
          'font-variant-numeric': 'tabular-nums',
        },
        valueText
      )
    );

    const lx = cx;
    const ly = height - margin.bottom + 14;
    g.appendChild(
      svgEl(
        'text',
        {
          x: lx,
          y: ly,
          'text-anchor': 'end',
          fill: 'var(--text-secondary)',
          'font-weight': anchor ? 650 : 400,
          transform: `rotate(-35 ${lx} ${ly})`,
        },
        step.label
      )
    );
  });
}

function renderTradeLogSummary(entry) {
  const host = $('tradelog-summary');
  clear(host);
  host.hidden = false;
  host.classList.toggle('closed', !entry.is_open);

  const cents = (value) => money(value, { cents: true });
  const perShare = (value) => (value === null || value === undefined ? '—' : '$' + value.toFixed(2));

  host.appendChild(
    tradeLogCell('Ticker', entry.underlying + (entry.capital_estimated ? ' ~' : ''), {
      help: formula([
        'Underlying stock symbol for this wheel.',
        '~ = some capital is a strike-based estimate (pre-export shares).',
      ]),
    })
  );
  host.appendChild(
    tradeLogCell('Name', entry.name || '—', {
      help: formula(['Issuer name, best-effort from the broker description. Cosmetic only.']),
    })
  );
  host.appendChild(
    tradeLogCell('Status', entry.status, {
      help: formula([
        'ACTIVE: something is still open (a contract or shares).',
        'CLOSED: flat, and never went through an assignment.',
        'ASSIGNED: flat now, but an assignment happened along the way.',
      ]),
    })
  );
  host.appendChild(
    tradeLogCell('Entries', String(entry.transactions.length), {
      help: formula(['Number of transaction rows in the table below.']),
    })
  );
  host.appendChild(
    tradeLogCell('Date range', [entry.start_date, `→ ${entry.end_date || 'current'}`], {
      foot: `${entry.days_active.toLocaleString('en-US')} day${entry.days_active === 1 ? '' : 's'}`,
      help: formula([
        "First trade → last trade, or 'current' while the wheel is open.",
        'The day count is calendar days from the first trade to that end.',
      ]),
    })
  );
  host.appendChild(
    tradeLogCell('Cost basis / share', perShare(entry.cost_basis_per_share), {
      help: formula([
        'Average purchase price of the shares still held.',
        'The raw tax-lot basis (what a 1099-B would show).',
        'A dash means the wheel holds no shares.',
      ]),
    })
  );
  host.appendChild(
    tradeLogCell('Break-even price', perShare(entry.break_even_price), {
      foot:
        entry.current_price === null || entry.break_even_price === null
          ? null
          : `now ${perShare(entry.current_price)} · ${perShare(
              Math.abs(entry.break_even_price - entry.current_price)
            )} ${entry.current_price >= entry.break_even_price ? 'above' : 'to go'}`,
      help: formula([
        'Stock price at which the whole campaign nets to $0:',
        'raw cost of the shares still held, less every other',
        'dollar the campaign has banked or paid (premium, fees,',
        'dividends, open options valued at expiry).',
        'A dash means the wheel holds no shares.',
      ]),
    })
  );
  host.appendChild(
    tradeLogCell('Shares held', (entry.shares_held || 0).toLocaleString('en-US'), {
      help: formula([
        'Shares still held from assignment(s),',
        'net of any sold or called away.',
        '0 once the wheel is flat.',
      ]),
    })
  );
  host.appendChild(
    tradeLogCell('Open contracts', String(entry.open_contracts || 0), {
      help: formula(['Short option contracts still open, summed across all legs.']),
    })
  );
  host.appendChild(
    tradeLogCell('Gross premium received', cents(entry.gross_premium_received), {
      help: formula([
        'Sum of credits taken in on every short open (STO).',
        'Not net of buy-backs (see Realized P&L for that).',
      ]),
    })
  );
  host.appendChild(
    tradeLogCell('Dividends', cents(entry.dividends), {
      help: formula(['Dividends received while this wheel held the stock.']),
    })
  );
  host.appendChild(
    tradeLogCell('Total fees & commissions', cents(entry.total_fees_commissions), {
      help: formula(['Σ (fees + commissions) over every row in the table below.']),
    })
  );
  host.appendChild(
    tradeLogCell('Capital committed now', money(entry.capital_committed_now), {
      help: formula([
        'Collateral tied up right now, the sum of:',
        'short-put collateral (strike × 100 × contracts),',
        'cost basis of any shares held,',
        'and strike × 100 for a short call whose backing',
        'shares are not in the data (marked ~).',
        '$0 once the wheel is flat.',
      ]),
    })
  );
  host.appendChild(
    tradeLogCell('Realized P&L', cents(entry.net_realized_pl), {
      tone: entry.net_realized_pl >= 0 ? 'pos' : 'neg',
      foot: `${cents(entry.option_realized_pl)} option · ${cents(entry.stock_realized_pl)} stock`,
      help: formula([
        'Closed positions only: option P/L plus realized stock P/L.',
        `= ${cents(entry.option_realized_pl)} + ${cents(entry.stock_realized_pl)}`,
        `= ${cents(entry.net_realized_pl)}`,
        'For the full picture incl. open shares and options,',
        'see P&L (mark-to-market) below.',
      ]),
    })
  );
  host.appendChild(
    tradeLogCell(
      'P&L (mark-to-market)',
      entry.mark_to_market_pl === null ? '—' : cents(entry.mark_to_market_pl),
      {
        tone:
          entry.mark_to_market_pl === null ? null : entry.mark_to_market_pl >= 0 ? 'pos' : 'neg',
        foot:
          entry.mark_to_market_pl === null
            ? 'no share price available'
            : `${cents(entry.net_realized_pl)} realized · ${cents(
                entry.stock_unrealized_pl
              )} shares · ${cents(entry.open_option_pl)} open options`,
        help: formula([
          'Where the campaign stands right now.',
          'Realized P&L + held shares marked to the latest close',
          '+ open option legs valued at expiry (long puts as a',
          'loss, short calls as a gain).',
          "An open long option's remaining time value is not",
          'marked, so a held protective put makes this conservative.',
        ]),
      }
    )
  );

  const days1 = (value) => (value === null || value === undefined ? '—' : value.toFixed(1));
  host.appendChild(
    tradeLogCell(
      'P&L / day held',
      entry.pl_per_day_held === null ? '—' : cents(entry.pl_per_day_held) + '/day',
      {
        foot: entry.closed_leg_count
          ? `${entry.closed_leg_count} closed legs over ${entry.total_days_held} days held`
          : null,
        help: formula([
          'Total option P&L divided by total days a position was held.',
          `= ${cents(entry.closed_leg_pl)} ÷ ${entry.total_days_held} days`,
          `= ${entry.pl_per_day_held === null ? '—' : cents(entry.pl_per_day_held)}/day`,
          'Closed legs only. Each roll segment counts on its own.',
        ]),
      }
    )
  );
  host.appendChild(
    tradeLogCell('Win rate', pct(entry.win_rate_pct), {
      foot: `${entry.wins} / ${entry.wins + entry.losses} closed legs`,
      help: formula([
        'Winning legs ÷ (winning + losing) closed legs.',
        'Open legs and exact break-evens are excluded from the count.',
      ]),
    })
  );
  host.appendChild(
    tradeLogCell('Avg days in trade', days1(entry.avg_days_in_trade), {
      help: formula(['Mean calendar days each closed leg of this wheel was held.']),
    })
  );
  host.appendChild(
    tradeLogCell('Annualized Wheel ROC', pct(entry.annualized_wheel_roc_pct), {
      tone:
        entry.annualized_wheel_roc_pct === null
          ? null
          : entry.annualized_wheel_roc_pct >= 0
          ? 'pos'
          : 'neg',
      help: formula([
        'Option P/L on the capital it tied up, scaled to a year.',
        '(Option P/L ÷ Avg collateral) × (365 ÷ Days active)',
        `= (${cents(entry.option_realized_pl)} ÷ ${money(entry.avg_collateral)})` +
          ` × (365 ÷ ${entry.days_active})`,
        `= ${pct(entry.annualized_wheel_roc_pct)}`,
        'Option P/L only, never stock P/L. Same figure the Dashboard shows.',
      ]),
    })
  );

  const note = $('tradelog-note');
  clear(note);
  if (entry.attribution_note) {
    note.hidden = false;
    note.appendChild(el('span', {}, entry.attribution_note));
  } else {
    note.hidden = true;
  }
}

function renderTradeLogTable(entry) {
  const host = $('tradelog-table');
  clear(host);
  const head = [
    'Type',
    'Date',
    'Expiration',
    'Strike',
    'Shares / Contracts',
    'Price / Premium',
    'Return %',
    'Initial CSP collateral',
    'Fees',
    'Commissions',
    'Net cash flow',
    'Cumulative cash flow',
  ];
  const HEAD_HELP = {
    Type: formula([
      'What the trade did:',
      'sell put, buy put, sell call, buy call,',
      'expire, assign, shares in / out, dividend.',
    ]),
    Date: formula([
      'Date the trade actually happened.',
      'The as-of date, not the ledger post date.',
    ]),
    Expiration: formula(['Option expiry.', 'Blank for stock and dividend rows.']),
    Strike: 'Option strike price.',
    'Shares / Contracts': formula([
      'Signed size of the fill:',
      'positive means bought / long,',
      'negative means sold / short.',
      'A dash means a dividend row.',
    ]),
    'Price / Premium': formula([
      'Per-share fill price for stock,',
      'or option premium per share, as the broker quoted it.',
      'Green +: cash received on this fill.',
      'Red -: cash paid on this fill.',
    ]),
    'Return %': formula([
      "The closing fill's return vs the premium at open.",
      'Short: (open minus close) ÷ open.',
      '  0.50 then a 0.25 buyback = 50%.',
      'Long: (close minus open) ÷ open.',
      '  1.00 then a 0.20 sale = -80%.',
      'Expiry and assignment count as a close at 0.',
      'Opening fills have no value here.',
    ]),
    'Initial CSP collateral': formula([
      'Strike × 100 × contracts.',
      'Shown on a cash-secured put open only.',
    ]),
    Fees: 'Regulatory and exchange fees on this fill.',
    Commissions: 'Broker commission on this fill.',
    'Net cash flow': formula([
      "The broker's Amount for this row.",
      'Already net of fees and commission.',
    ]),
    'Cumulative cash flow': formula([
      'Running sum of Net cash flow down the rows.',
      'A cash ledger, not a P&L figure.',
    ]),
  };
  const table = el('table');
  const thead = el('thead');
  const headRow = el('tr');
  head.forEach((label, index) => {
    const th = el('th', { class: index === 0 ? 'left' : '' }, label);
    setFormula(th, HEAD_HELP[label]);
    th.style.cursor = 'help';
    headRow.appendChild(th);
  });
  thead.appendChild(headRow);
  table.appendChild(thead);

  const cents = (value) => (value === null || value === undefined ? '—' : money(value, { cents: true }));
  const bare = (value) => (value === null || value === undefined ? '—' : '$' + value);
  // Long/bought positive with a leading +, short/sold negative with -.
  // No direction (a dividend) shows a dash.
  const signedQty = (value) =>
    value === null || value === undefined || value === 0
      ? '—'
      : (value > 0 ? '+' : '-') + Math.abs(value).toLocaleString('en-US');
  // Price / premium signed by whether the cash for this fill was received
  // (+, credit) or paid (-, debit) -- taken from the row's net cash flow.
  const signedPrice = (row) => {
    if (row.price === null || row.price === undefined) return '—';
    const flow = row.net_cash_flow;
    const prefix = typeof flow !== 'number' || flow === 0 ? '$' : flow > 0 ? '+$' : '-$';
    return prefix + row.price;
  };

  const tbody = el('tbody');
  for (const row of entry.transactions) {
    const tr = el('tr');
    // Row belongs to a position that is no longer active (a fully closed leg --
    // its open and its closes -- a sale, an expiry): grey the whole row.
    if (row.is_settled) tr.classList.add('settled');
    if (row.synthetic) {
      tr.classList.add('synthetic');
      tr.title = 'Synthesized at the strike: the broker export has no share leg for this assignment.';
    }
    const cells = [
      row.type,
      row.date,
      row.expiration || '—',
      bare(row.strike),
      signedQty(row.signed_quantity),
      signedPrice(row),
      typeof row.close_return_pct === 'number' ? pct(row.close_return_pct, 0) : '—',
      cents(row.initial_csp_collateral),
      cents(row.fees),
      row.commission === null || row.commission === undefined ? '—' : cents(row.commission),
      cents(row.net_cash_flow),
      cents(row.running_cash_flow),
    ];
    cells.forEach((value, index) => {
      const td = el('td', { class: index === 0 ? 'left' : 'num' }, value);
      if (index === 0 && (row.type === 'Buy Shares' || row.type === 'Sell Shares')) {
        td.classList.add('shares-type'); // stock fills read blue, apart from the option rows
      }
      if (index === 4 && typeof row.signed_quantity === 'number' && row.signed_quantity !== 0) {
        td.classList.add(row.signed_quantity > 0 ? 'pos' : 'neg');
      }
      if (
        index === 5 &&
        row.price !== null &&
        row.price !== undefined &&
        typeof row.net_cash_flow === 'number' &&
        row.net_cash_flow !== 0
      ) {
        td.classList.add(row.net_cash_flow > 0 ? 'pos' : 'neg');
      }
      if (index === 6 && typeof row.close_return_pct === 'number') {
        td.classList.add(row.close_return_pct >= 0 ? 'pos' : 'neg');
      }
      if (index === 10 && typeof row.net_cash_flow === 'number') {
        td.classList.add(row.net_cash_flow >= 0 ? 'pos' : 'neg');
      }
      if (index === 11 && typeof row.running_cash_flow === 'number') {
        td.classList.add(row.running_cash_flow >= 0 ? 'pos' : 'neg');
      }
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  host.appendChild(table);
  if (!entry.transactions.length) {
    host.appendChild(el('p', { class: 'hint' }, 'No transactions recorded for this wheel.'));
  }
}

function renderTradeLog() {
  const tickerSel = $('tradelog-ticker');
  clear(tickerSel);
  tickerSel.appendChild(el('option', { value: '' }, 'All tickers'));
  for (const ticker of tradeLogTickers()) {
    tickerSel.appendChild(el('option', { value: ticker }, ticker));
  }
  if (state.tradeLogTicker && !tradeLogTickers().includes(state.tradeLogTicker)) {
    state.tradeLogTicker = null;
  }
  tickerSel.value = state.tradeLogTicker || '';

  const wheels = orderedTradeLog();
  const pick = $('tradelog-pick');
  clear(pick);
  for (const wheel of wheels) {
    const label =
      `${wheel.cycle_id} · ${wheel.underlying}` +
      (wheel.name ? ` (${wheel.name})` : '') +
      ` · ${wheel.start_date} → ${wheel.end_date || 'current'}` +
      ` · ${wheel.transactions.length} entries`;
    // Closed (or assigned-and-flat) wheels are tinted salmon in the list.
    pick.appendChild(el('option', { value: wheel.cycle_id, class: wheel.is_open ? '' : 'closed' }, label));
  }

  const empty = $('tradelog-empty');
  if (!wheels.length) {
    empty.hidden = false;
    empty.textContent = 'No wheels in this account yet.';
    $('tradelog-summary').hidden = true;
    $('tradelog-bridge').hidden = true;
    $('tradelog-note').hidden = true;
    clear($('tradelog-table'));
    return;
  }

  // Nothing valid selected (first open, ticker just changed, prior wheel
  // filtered out) -> default to the first wheel in the list.
  if (!wheels.some((wheel) => wheel.cycle_id === state.tradeLogCycleId)) {
    state.tradeLogCycleId = wheels[0].cycle_id;
  }
  pick.value = state.tradeLogCycleId;

  const index = wheels.findIndex((wheel) => wheel.cycle_id === state.tradeLogCycleId);
  $('tradelog-prev').disabled = index <= 0;
  $('tradelog-next').disabled = index < 0 || index >= wheels.length - 1;

  const entry = index < 0 ? null : wheels[index];
  if (!entry) {
    empty.hidden = false;
    empty.textContent =
      'Pick a wheel above, or click one in the Dashboard’s “Wheel timelines” chart.';
    $('tradelog-summary').hidden = true;
    $('tradelog-bridge').hidden = true;
    $('tradelog-note').hidden = true;
    clear($('tradelog-table'));
    return;
  }

  empty.hidden = true;
  drawTradeLogBridge(entry);
  renderTradeLogSummary(entry);
  renderTradeLogTable(entry);
}

function render() {
  const data = state.data;
  if (!data) return;
  const {
    meta,
    portfolio,
    cycles,
    tickers,
    capital_series,
    pnl_series,
    cash_flow,
    wheel_state,
    reconciliation,
    net_worth,
    benchmark,
    wheel_return,
  } = data;

  // With several exports loaded the filenames are long and already listed in the
  // notice below, so the subtitle summarises rather than enumerating them.
  const label = meta.combined
    ? `${meta.sources.length} exports combined · ${meta.duplicates_removed} duplicate rows merged`
    : meta.source;
  $('subtitle').textContent =
    `${label} · ${meta.transactions_in_slice} of ${meta.transactions_total} transactions · ` +
    `${meta.data_first_date} → ${meta.data_last_date} · generated ${meta.generated_at.replace('T', ' ')}`;
  $('subtitle').title = meta.source;

  renderYearPresets(meta);

  renderChips('ticker-chips', meta.available_tickers, state.tickers, (value) => {
    if (state.tickers.has(value)) state.tickers.delete(value);
    else state.tickers.add(value);
    load();
  });
  renderChips('status-chips', meta.statuses, state.statuses, (value) => {
    if (state.statuses.has(value)) state.statuses.delete(value);
    else state.statuses.add(value);
    load();
  });

  renderNotices(meta, reconciliation);
  renderTiles(portfolio, reconciliation);
  renderNetWorth(net_worth, benchmark, wheel_return);

  drawCapital(capital_series, net_worth);
  drawWheelState(wheel_state);
  drawPnl(pnl_series);

  renderCashFlowTiles(cash_flow.trailing, cash_flow.months, pnl_series);
  drawCashFlow(cash_flow.months, cash_flow.trailing, pnl_series);
  drawCashFlowGap(cash_flow.months, pnl_series);

  const tickerNetPlFormula = (row) =>
    formula([
      'Net realized P/L = Premium collected (net) + Stock realized P/L',
      '',
      `= ${money(row.option_realized_pl, { cents: true })} + ${money(row.stock_realized_pl, { cents: true })}`,
      `= ${money(row.net_realized_pl, { cents: true })}`,
    ]);

  drawSignedBars('chart-ticker-pl', tickers, {
    valueOf: (row) => row.net_realized_pl,
    format: (value) => compactMoney(value),
    legendId: 'legend-ticker-pl',
    legendNote: 'Net of option cash and realized stock P/L, one bar per ticker. Hover a bar for its breakdown.',
    tipRows: (row) => [
      { label: 'Net realized P/L', value: money(row.net_realized_pl, { cents: true }) },
      { label: 'Premium collected (net)', value: money(row.option_realized_pl, { cents: true }) },
      { label: 'Premium collected (gross)', value: money(row.premium_received) },
      { label: 'Paid to close', value: money(row.premium_paid) },
      { label: 'Stock P/L', value: money(row.stock_realized_pl) },
      { label: 'Cycles', value: `${row.cycles} (${row.active} active)` },
      { label: 'Rolls / assignments', value: `${row.rolls} / ${row.assignments}` },
    ],
    tipFormula: tickerNetPlFormula,
    tableId: 'ticker-table',
    tableHead: ['Ticker', 'Cycles', 'Premium', 'Paid to close', 'Option P/L', 'PPD', 'Stock P/L', 'Net P/L', 'Rolls', 'Assign'],
    tableRow: (row) => [
      row.underlying,
      row.cycles,
      money(row.premium_received),
      money(row.premium_paid),
      { text: money(row.option_realized_pl, { cents: true }), title: wheelOptionPlFormula(row, 'Premium collected (net)') },
      {
        text: money(row.profit_per_day, { cents: true }) + '/day',
        title: formula([
          'Profit Per Day (PPD) = (Premium collected - Closeout cost) ÷ Days',
          '',
          `= ${money(row.option_realized_pl, { cents: true })} ÷ ${row.days_span}`,
          `= ${money(row.profit_per_day, { cents: true })}/day`,
        ]),
      },
      money(row.stock_realized_pl),
      { text: money(row.net_realized_pl, { cents: true }), title: tickerNetPlFormula(row) },
      row.rolls,
      row.assignments,
    ],
  });

  const tickerRocFormula = (row) =>
    row.annualized_wheel_roc_pct === null
      ? formula([
          'Annualized Wheel ROC (return on capital) =',
          '  (Wheel option P/L ÷ Avg capital) × (365 ÷ Days)',
          '',
          'N/A -- no capital committed.',
        ])
      : formula([
          ...wheelOptionPlFormula(row, 'Wheel option P/L').split('\n'),
          '',
          'Annualized Wheel ROC (return on capital) =',
          '  (Wheel option P/L ÷ Avg capital) × (365 ÷ Days)',
          '  Includes cash-secured-put, covered-call, and hedge legs; excludes stock P/L.',
          '',
          `= (${money(row.option_realized_pl, { cents: true })} ÷ ${money(row.avg_capital)}) × (365 ÷ ${row.days_span})`,
          `= ${pct(row.roi_on_avg_wheel_pct, 2)} × ${(365 / row.days_span).toFixed(2)}`,
          `= ${pct(row.annualized_wheel_roc_pct)}`,
        ]);

  const tickerNetOptionYieldFormula = (row) =>
    row.annualized_net_option_yield_pct === null
      ? formula([
          'Annualized Net Option Yield = (Option P/L ÷ Total initial collateral) × (365 ÷ Days)',
          '',
          'N/A -- no initial collateral committed.',
        ])
      : formula([
          'Denominator is total initial (day-one) collateral, summed across',
          '  every cycle -- not a time-weighted average.',
          '',
          'Annualized Net Option Yield =',
          '  (Option P/L ÷ Total initial collateral) × (365 ÷ Days)',
          `= (${money(row.option_realized_pl, { cents: true })} ÷ ${money(row.total_initial_collateral)}) × (365 ÷ ${row.days_span})`,
          `= ${pct(row.net_option_yield_pct, 2)} × ${(365 / row.days_span).toFixed(2)}`,
          `= ${pct(row.annualized_net_option_yield_pct)}`,
        ]);

  const tickerTotalPositionRoiFormula = (row) =>
    row.annualized_total_position_roi_pct === null
      ? formula([
          'Annualized Total Position ROI (return on investment) =',
          '  (Option P/L + Stock realized/unrealized P&L + Dividends) ÷ Total initial collateral × (365 ÷ Days)',
          '',
          'N/A -- no initial collateral committed.',
        ])
      : formula([
          'Everything this ticker has produced -- option P/L, realized AND',
          '  unrealized stock P/L, dividends -- against total day-one capital.',
          '  Open long-option (hedge) unrealized P/L is not included.',
          '',
          'Annualized Total Position ROI (return on investment) =',
          '  (Option P/L + Stock realized P/L + Stock unrealized P/L + Dividends) ÷ Total initial collateral × (365 ÷ Days)',
          `= (${money(row.option_realized_pl, { cents: true })} + ${money(row.stock_realized_pl, { cents: true })} + ${money(row.stock_unrealized_pl, { cents: true })} + ${money(row.dividends_received, { cents: true })}) ÷ ${money(row.total_initial_collateral)} × (365 ÷ ${row.days_span})`,
          `= ${pct(row.total_position_roi_pct, 2)} × ${(365 / row.days_span).toFixed(2)}`,
          `= ${pct(row.annualized_total_position_roi_pct)}`,
        ]);

  drawSignedBars('chart-roc', tickers.filter((row) => row.annualized_wheel_roc_pct !== null), {
    valueOf: (row) => row.annualized_wheel_roc_pct,
    format: (value) => pct(value, 0),
    legendId: 'legend-roc',
    legendNote: 'Tickers marked ~ include a strike-based proxy for stock held before this export.',
    tipRows: (row) => [
      { label: 'Annualized Wheel ROC', value: pct(row.annualized_wheel_roc_pct) },
      { label: 'Return on avg capital', value: pct(row.roi_on_avg_wheel_pct, 2) },
      { label: 'Avg capital', value: money(row.avg_capital) },
      { label: 'Peak capital', value: money(row.peak_capital) },
      { label: 'Option realized P/L', value: money(row.option_realized_pl, { cents: true }) },
      ...(row.hedge_realized_pl
        ? [{ label: 'incl. hedge P/L', value: money(row.hedge_realized_pl, { cents: true }) }]
        : []),
      ...(row.capital_estimated
        ? [{ label: 'Note', value: 'includes strike-based proxy' }]
        : []),
    ],
    tipFormula: tickerRocFormula,
    tableId: 'roc-table',
    tableHead: [
      'Ticker',
      'Avg capital',
      'Peak capital',
      'Capital now',
      'Option P/L',
      'Return on avg',
      'Annualized Wheel ROC',
      'Annualized Net Option Yield',
      'Annualized Total Position ROI',
      {
        text: 'Proxy',
        title: formula([
          '"yes": this ticker has a covered call backed by shares bought',
          'before this export begins -- the shares are real and the call is',
          'genuinely covered, but since the purchase itself is outside the',
          'export, its capital is estimated as strike × 100 rather than the',
          'real cost basis. Marked ~ next to the ticker name on the chart.',
        ]),
      },
    ],
    tableRow: (row) => [
      row.underlying,
      money(row.avg_capital),
      money(row.peak_capital),
      money(row.capital_now),
      money(row.option_realized_pl, { cents: true }),
      { text: pct(row.roi_on_avg_wheel_pct, 2), title: tickerRocFormula(row) },
      { text: pct(row.annualized_wheel_roc_pct), title: tickerRocFormula(row) },
      { text: pct(row.annualized_net_option_yield_pct), title: tickerNetOptionYieldFormula(row) },
      { text: pct(row.annualized_total_position_roi_pct), title: tickerTotalPositionRoiFormula(row) },
      row.capital_estimated ? 'yes' : '—',
    ],
  });

  drawTickerScatter(tickers);

  drawTimeline(cycles, meta.through);
  renderCycles(cycles);

  // The Trade Log is filter-independent (payload's `trade_log` is built from
  // full history), but re-render it so the picker tracks an account switch.
  renderTabs();
  if (state.activeTab === 'tradelog') renderTradeLog();
}

wireFilters();
refreshDatasets();
// load() must wait for refreshAccounts() to resolve data/accounts.json's
// default_account (if any) into state.account -- firing both in parallel
// would load "combined" first and only switch a moment later.
refreshAccounts().then(load);
