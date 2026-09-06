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
  // Open option positions table: within each symbol group (symbols stay
  // alphabetical), which column orders the rows and in which direction.
  openPosSort: { key: 'expiration', dir: 1 },
  // Covered-call candidates table: which column sorts it, and which direction.
  ccCandSort: { key: 'underlying', dir: 1 },
  // CSP-candidates table (inside the Cash for CSPs card): sort column + dir.
  cspCandSort: { key: 'stars', dir: -1 },
  // How the capital chart expresses its bands: 'value' (dollars) or 'share' (%
  // of the day's total). A view of one chart, not a filter -- it changes no data.
  capitalMode: 'value',
  // Row order for the Wheel timelines chart: 'time' (by start date, newest wheel
  // on top -- the default, the account's recent history first) or 'ticker' (A-Z,
  // a ticker's cycles together, best for lookup). View-only, changes no data.
  timelineSort: 'time',
  // Bucket size for the Periodic P/L histogram: 'month' or 'week'. Both are
  // already in the payload (data.period_pl.months / .weeks), so switching is
  // a redraw from already-fetched data, not a refetch.
  periodPlGranularity: 'month',
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

/**
 * Centered rolling mean with a window that shrinks toward each end, so the
 * first and last points come back exactly unchanged (radius 0 there) while the
 * interior is smoothed over up to `2 * maxRadius + 1` samples. Used to draw a
 * spiky running-total line as a clean trend without moving its endpoints or
 * touching the underlying values (tooltips/tables stay exact).
 */
function smoothSeries(values, maxRadius = 4) {
  return values.map((_, i) => {
    const r = Math.min(maxRadius, i, values.length - 1 - i);
    let sum = 0;
    for (let j = i - r; j <= i + r; j += 1) sum += values[j];
    return sum / (2 * r + 1);
  });
}

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
    // `valueClass` ('pos'/'neg') tints P&L green for a gain, red for a loss.
    line.appendChild(el('span', { class: 'tt-val' + (row.valueClass ? ' ' + row.valueClass : '') }, row.value));
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
/**
 * Show/hide the dashboard card that wraps a chart, so a narrow filter (one
 * dormant ticker, an empty date window) collapses the empty cards instead of
 * leaving a row of blank panels. Call with `hasData` true BEFORE measuring
 * chart width -- a hidden card has clientWidth 0.
 */
function toggleChartCard(svg, hasData) {
  // A chart nested in a `.card-subsection` (e.g. the four Cash Flow & P/L
  // Analytics quadrants) hides just its own sub-section; a chart that is the
  // whole card hides the card. Nearest wins.
  const box = svg && svg.closest ? svg.closest('.card-subsection, section.card') : null;
  if (box) box.hidden = !hasData;
}

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
// These labels are the single source of truth for how the committed-capital
// components are named -- the chart legend, the table headers, the aria text
// and every "Capital deployed" tooltip formula all read them from here, so a
// rename lands everywhere at once. They also match the wheel-state donut's
// wedges, so the two charts (side by side) read as one vocabulary: "shares
// held" is split into idle (orange, no call written) and covered-call
// backing (aqua), and the covered-call backing further into real cost basis
// and the pre-export strike estimate -- exactly the donut's split.
//
// Every committed-capital component gets a band and a colour chip -- nothing is
// "counted but invisible". Long-option debit and net spread collateral are
// usually a sliver (long debit peaks near 0.4% of committed capital, about a
// pixel), but a hairline yellow band plus a legend row still beats a blank
// space the reader can't reconcile. Yellow (`--series-4`) sits ABOVE put
// collateral so it never touches the orange idle-shares band -- the one weak
// colour pair the palette was validated against (see docs/DESIGN.md). A band
// that rounds to $0 draws nothing and is dropped from the legend too, so the
// two always agree.
const CAPITAL_BANDS = [
  { key: 'idle_stock', label: 'Idle shares (cost basis)', varName: '--series-2' },
  // Real cost basis of shares currently backing an open covered call. Sits
  // next to Idle shares so the two "shares held at known cost" bands read
  // together, then the estimate above them, then put collateral on top.
  { key: 'call_stock', label: 'Covered-call shares (cost basis)', varName: '--series-3' },
  // Same, but for shares bought before this export begins -- their real cost
  // basis isn't visible so the strike stands in (strike x 100). `estimate`
  // draws it a touch lighter with a dashed cap: "this slice is a guess, not a
  // number off your statements."
  { key: 'call', label: 'Covered-call shares (strike estimate)', varName: '--series-3', estimate: true },
  { key: 'put', label: 'Put collateral', varName: '--series-1' },
  { key: 'long', label: 'Long-option debit', varName: '--series-4' },
  { key: 'spread', label: 'Net spread collateral', varName: '--series-4' },
];

// The three neutral (no-hue) capital concepts, a grey ramp from lightest to
// strongest ink: Cash, then Unrealized, then the Total lines (`--text-primary`,
// applied inline where the two totals are drawn). Unlike the hued bands there
// is no daily history for cash/unrealized -- both are known only on a Portfolio
// Positions snapshot date, so neither is zero-filled; they show as isolated
// markers above the total line, not folded into the stack (see notDeployedByDate).
// The wheel-state donut uses these same two tokens for its Cash/Unrealized
// wedges, so the two charts read as one shared vocabulary.
const NOT_DEPLOYED_COLOR = '--text-muted'; // Cash
const UNREALIZED_COLOR = '--text-secondary'; // Unrealized

// Derived from CAPITAL_BANDS so the column headers, the tooltip formulas and
// the aria text can never drift from the legend labels. Order matches the
// stack, bottom -> top.
const CAPITAL_COMPONENT_LABELS = CAPITAL_BANDS.map((c) => c.label);
// The same sum, for every "Capital deployed" / "Initial cap" formula.
const CAPITAL_FORMULA_SUM = CAPITAL_COMPONENT_LABELS.join(' + ');
const CAPITAL_TABLE_HEAD = [
  'Date',
  ...CAPITAL_COMPONENT_LABELS,
  { text: 'Total committed', title: `Total committed = ${CAPITAL_COMPONENT_LABELS.join(' + ')}` },
  {
    text: 'Cash',
    title: 'Account cash not already reserved as put collateral, blank except on a Positions snapshot date.',
  },
  {
    text: 'Unrealized',
    title: 'Unrealized gains plus equity this history can\'t track, blank except on a Positions snapshot date.',
  },
  { text: 'Total value', title: 'Total committed + Cash + Unrealized; the whole account, cash and all.' },
];

/**
 * One row of the wheel-state donut's `.legend-stack` breakdown — the one
 * figured capital breakdown on the page (the Capital deployed chart above keeps
 * only a plain colour key). `kind`: 'square' (default) a filled colour chip,
 * 'line' a solid rule (a subtotal like Total committed), 'dashed' a dashed rule
 * (a grand total like Total value), 'none' an invisible spacer. `color` is a
 * resolved CSS value; `faded` dims a chip to ~45%; `indent` nests a component
 * under its group header; `strong` bolds the label.
 */
function capitalBreakdownRow(legend, label, value, { color, kind = 'square', faded, indent, strong } = {}) {
  const row = el('span');
  if (indent) row.style.paddingLeft = '16px';
  const swatch = el('i', kind === 'line' || kind === 'dashed' ? { class: 'line' } : {});
  if (kind === 'dashed') {
    swatch.style.background = 'none';
    swatch.style.height = '0';
    swatch.style.borderTop = `2px dashed ${color || 'currentColor'}`;
  } else if (kind === 'none') {
    swatch.style.visibility = 'hidden';
  } else if (color) {
    swatch.style.background = color;
    if (faded) swatch.style.opacity = '0.45';
  }
  row.appendChild(swatch);
  row.appendChild(strong ? el('strong', {}, label) : document.createTextNode(label));
  row.appendChild(el('b', { class: 'legend-value' }, money(value)));
  legend.appendChild(row);
}

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
/**
 * `{ amount, cash, unrealized }` per date, ``amount`` being the combined
 * figure described above and ``cash``/``unrealized`` its same split as the
 * "Where the wheel is right now" donut's own Cash/Unrealized
 * wedges (see `drawWheelState`): ``cash`` is that day's committed capital
 * point's ``put`` collateral subtracted from the snapshot's own
 * ``cash_total`` (never double-counted with the Cash Secured (puts) band),
 * ``unrealized`` is whatever of ``amount`` that leaves.
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
    let cashTotal = 0;
    let anyKnown = false;
    for (const timeline of timelines) {
      const latest = lastSnapshotAtOrBefore(timeline, asOf);
      if (latest) {
        totalValue += latest.total_value;
        cashTotal += latest.cash_total || 0;
        anyKnown = true;
      }
    }
    if (!anyKnown) continue;
    const amount = totalValue - point.total;
    if (amount <= 1e-9) continue;
    const cash = Math.min(Math.max(cashTotal - point.put, 0), amount);
    map.set(asOf, { amount, cash, unrealized: amount - cash });
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
    toggleChartCard(svg, false);
    return;
  }
  toggleChartCard(svg, true);

  const share = state.capitalMode === 'share';
  // Share mode's y-axis is 0-100% of that day's *committed* capital -- there
  // is no room in that scale for a quantity measured against total account
  // value instead, so the overlay is $-mode only (see the legend note below).
  const notDeployed = share ? new Map() : notDeployedByDate(points, netWorth);
  // Positions snapshots are sparse -- often only a handful of dates. With two
  // or more, connect them so Cash + Unrealized reads as one continuous shaded
  // estimate across the snapshot range rather than isolated single-day
  // slivers. Straight linear interpolation between consecutive snapshots,
  // never extrapolated past the first or last (the account value there simply
  // isn't known). One snapshot alone can't be connected -- it stays a dot.
  const ndAnchors = [...notDeployed.keys()]
    .sort()
    .map((d) => ({ t: parseDay(d).getTime(), ...notDeployed.get(d) }));
  const ndInterp =
    ndAnchors.length >= 2
      ? points.map((p) => {
          const t = parseDay(p.date).getTime();
          if (t < ndAnchors[0].t || t > ndAnchors[ndAnchors.length - 1].t) return { cash: 0, unrealized: 0 };
          const hi = ndAnchors.findIndex((a) => a.t >= t);
          const a = ndAnchors[hi - 1] || ndAnchors[0];
          const b = ndAnchors[hi] || ndAnchors[0];
          const f = b.t === a.t ? 0 : (t - a.t) / (b.t - a.t);
          return {
            cash: a.cash + (b.cash - a.cash) * f,
            unrealized: a.unrealized + (b.unrealized - a.unrealized) * f,
          };
        })
      : null;
  const ndTopAt = (i) =>
    ndInterp
      ? ndInterp[i].cash + ndInterp[i].unrealized
      : notDeployed.get(points[i].date)?.amount || 0;
  const last = points[points.length - 1];
  const margin = { top: 12, right: 64, bottom: 30, left: 62 };
  const width = chartWidth(svg);
  const height = 288;
  const yMax = share ? 100 : Math.max(...points.map((p, i) => p.total + ndTopAt(i))) * 1.06 || 1;

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
    const estimate = CAPITAL_BANDS[index].estimate;
    const runs = bandRuns(lower, upper);
    const top = points.map((p, i) => `${x(p.date)},${y(upper[i])}`);
    const bottom = points.map((p, i) => `${x(p.date)},${y(lower[i])}`).reverse();
    group.appendChild(
      svgEl('path', {
        d: `M${top.join('L')}L${bottom.join('L')}Z`,
        fill: colors[index],
        // The strike-estimate band draws only slightly lighter than the rest --
        // enough to read as "this figure is a guess" alongside its dashed cap,
        // but still clearly a shaded band, not a blank gap.
        'fill-opacity': estimate ? BAND_WASH * 0.8 : BAND_WASH,
        stroke: 'none',
      })
    );
    // 2. A 2px cap in the band's own hue along its top edge -- an edge, not an
    //    outline, and it keeps a band that thins to a pixel still visible.
    //    Restricted to the runs where this band has height: drawn full width,
    //    every band's cap would land on the same line wherever the bands above
    //    are empty, and the last one painted would misreport what is on top.
    //    The estimate band's cap is dashed, the second half of the "guess" flag.
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
        'stroke-dasharray': estimate ? '4,3' : 'none',
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

  // 4. The total. Every component is now banded, so this line normally rests
  //    right on the top cap; it still earns its keep as the ink reference the
  //    Cash / Unrealized fill builds up from, and on a day whose capital is a
  //    single hairline band (all long debit, say) it is the only firm mark.
  //    In share mode it would be a flat line at ~100% carrying no level, so the
  //    axis does the job instead and the shortfall to 100% shows the same thing.
  if (!share) {
    group.appendChild(
      svgEl('path', {
        d: 'M' + points.map((p) => `${x(p.date)},${y(p.total)}`).join('L'),
        fill: 'none',
        stroke: cssVar('--text-primary'),
        'stroke-width': 1.5,
        'stroke-linejoin': 'round',
      })
    );
  }

  // 5. Cash + Unrealized -- the shaded area between committed capital and the
  //    account's Total value, stacked Cash then Unrealized with the same two
  //    colors the "Where the wheel is right now" donut uses
  //    (see `notDeployedByDate` / `drawWheelState`). The Positions snapshots
  //    (dots) are the only real readings; with two or more they are connected
  //    by a dashed Total-value line so the fill spans the whole snapshot
  //    range as one interpolated estimate, not isolated single-day slivers.
  //    A lone snapshot can't be connected, so it stays just its dots.
  if (ndInterp || notDeployed.size) {
    const cash = points.map((p, i) => (ndInterp ? ndInterp[i].cash : notDeployed.get(p.date)?.cash || 0));
    const unrealized = points.map((p, i) =>
      ndInterp ? ndInterp[i].unrealized : notDeployed.get(p.date)?.unrealized || 0
    );
    const cashLower = points.map((p) => p.total);
    const cashUpper = points.map((p, i) => cashLower[i] + cash[i]);
    const unrUpper = points.map((p, i) => cashUpper[i] + unrealized[i]);

    const fillRun = (lower, upper, color) => {
      for (const run of bandRuns(lower, upper)) {
        const top = run.map((i) => `${x(points[i].date)},${y(upper[i])}`);
        const bottom = run.map((i) => `${x(points[i].date)},${y(lower[i])}`).reverse();
        group.appendChild(
          svgEl('path', {
            d: `M${top.join('L')}L${bottom.join('L')}Z`,
            fill: color,
            'fill-opacity': BAND_WASH,
            stroke: 'none',
          })
        );
      }
    };
    fillRun(cashLower, cashUpper, cssVar(NOT_DEPLOYED_COLOR));
    fillRun(cashUpper, unrUpper, cssVar(UNREALIZED_COLOR));

    // The connecting Total-value line, along the top of the area. `--text-primary`
    // (the strong ink both "total" lines use), dashed to say the stretch between
    // two real snapshot dots is interpolated.
    for (const run of bandRuns(cashLower, unrUpper)) {
      group.appendChild(
        svgEl('path', {
          d: 'M' + run.map((i) => `${x(points[i].date)},${y(unrUpper[i])}`).join('L'),
          fill: 'none',
          stroke: cssVar('--text-primary'),
          'stroke-width': 1.5,
          'stroke-dasharray': '4,3',
          'stroke-linejoin': 'round',
        })
      );
    }

    // Dots: the real snapshot readings only, at each sub-band's true peak.
    for (const [date, nd] of notDeployed) {
      const point = points.find((p) => p.date === date);
      if (!point) continue;
      for (const stop of [
        { v: nd.cash, top: point.total + nd.cash, color: cssVar(NOT_DEPLOYED_COLOR) },
        { v: nd.unrealized, top: point.total + nd.cash + nd.unrealized, color: cssVar(UNREALIZED_COLOR) },
      ]) {
        if (!(stop.v > 1e-9)) continue;
        group.appendChild(
          svgEl('circle', {
            cx: x(date),
            cy: y(stop.top),
            r: 4,
            fill: stop.color,
            stroke: surface,
            'stroke-width': 2,
          })
        );
      }
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
        { label: 'Total committed', value: money(point.total) },
      ];
      if (nd !== undefined) {
        rows.push(
          { label: 'Cash', value: money(nd.cash), color: cssVar(NOT_DEPLOYED_COLOR) },
          { label: 'Unrealized', value: money(nd.unrealized), color: cssVar(UNREALIZED_COLOR) },
          { label: 'Total value', value: money(point.total + nd.amount) }
        );
      }
      showTooltip(
        at,
        longDate(point.date),
        rows,
        formula([
          'Total committed = Σ of every band above',
          `= ${CAPITAL_BANDS.map((c) => money(point[c.key] || 0)).join(' + ')}`,
          `= ${money(point.total)}`,
          ...(nd !== undefined
            ? [
                '',
                'Total value (Positions snapshot only) = Total committed + Cash + Unrealized',
                `= ${money(point.total)} + ${money(nd.cash)} + ${money(nd.unrealized)} = ${money(point.total + nd.amount)}`,
              ]
            : []),
        ])
      );
    },
  });

  svg.setAttribute(
    'aria-label',
    `Committed capital by day, stacked: ${CAPITAL_BANDS.map((b) => b.label).join(', ')}. ` +
      `Latest total ${money(last.total)} on ${longDate(last.date)}.` +
      (ndAnchors.length
        ? ` Positions snapshots add Cash and Unrealized on top, up to a Total value of ` +
          `${money(last.total + (notDeployed.get(last.date)?.amount || ndAnchors[ndAnchors.length - 1].amount))}` +
          `${ndAnchors.length >= 2 ? ', interpolated between snapshot dates' : ''}.`
        : '')
  );

  // Legend: a plain colour key only -- swatch + band name, no figures. The one
  // breakdown with numbers (Total committed / Cash / Unrealized / Total value)
  // lives in the wheel-state donut's legend right below: its components are
  // these same bands grouped by wheel phase, and reconcile to this chart's
  // latest day to the cent. A band that is $0 on the latest day (and so draws
  // nothing) is left out of the key too.
  CAPITAL_BANDS.forEach((band, i) => {
    if (Math.round(last[band.key] || 0) === 0) return;
    const item = el('span');
    const swatch = el('i');
    swatch.style.background = colors[i];
    if (band.estimate) swatch.style.opacity = '0.45';
    item.appendChild(swatch);
    item.appendChild(document.createTextNode(band.label));
    legend.appendChild(item);
  });
  if (share) {
    legend.appendChild(el('span', { class: 'legend-note' }, 'Switch to $ to add Cash and Unrealized.'));
  } else if (ndAnchors.length >= 2) {
    legend.appendChild(el('span', { class: 'legend-note' }, `Dots = ${ndAnchors.length} snapshots; band between is interpolated.`));
  }

  buildTable(
    'capital-table',
    CAPITAL_TABLE_HEAD,
    points.map((point) => {
      const nd = notDeployed.get(point.date);
      return [
        point.date,
        ...CAPITAL_BANDS.map((c) => money(point[c.key] || 0)),
        money(point.total),
        nd === undefined ? '—' : money(nd.cash),
        nd === undefined ? '—' : money(nd.unrealized),
        nd === undefined ? '—' : money(point.total + nd.amount),
      ];
    })
  );
}

/* ----------------------------------------- chart: wheel-state snapshot donut */

// One ring, one wedge per leaf capital component, in wheel-phase order. Colour
// is bound to the phase (docs/DESIGN.md's shared vocabulary), so the two
// "Covered-call shares" components read as one group by shared hue + adjacency
// -- no second ring needed to bracket them. `estimate: true` means the dollar
// figure is a strike-based stand-in, drawn faded. `synthetic` components (Cash,
// Unrealized) come from the Positions snapshot, not `wheel_state`.
const WHEEL_STATE_PHASE_META = {
  puts: { label: 'Cash-Secured Puts', varName: '--series-1' },
  holding: { label: 'Holding Shares', varName: '--series-2' },
  calls: { label: 'Covered-call shares', varName: '--series-3' },
  other: { label: 'Hedged / Other', varName: '--series-4' },
  cash: { label: 'Cash', varName: NOT_DEPLOYED_COLOR },
  unrealized: { label: 'Unrealized', varName: UNREALIZED_COLOR },
};

const WHEEL_STATE_COMPONENTS = [
  { key: 'put_collateral', phase: 'puts', label: 'Put collateral' },
  { key: 'holding_cost_basis', phase: 'holding', label: 'Idle shares (cost basis)' },
  { key: 'calls_cost_basis', phase: 'calls', label: 'Covered-call shares (cost basis)' },
  { key: 'calls_strike_estimate', phase: 'calls', label: 'Covered-call shares (strike estimate)', estimate: true },
  { key: 'long_option_debit', phase: 'other', label: 'Long-option debit' },
  { key: 'spread_collateral', phase: 'other', label: 'Net spread collateral' },
  { key: 'cash', phase: 'cash', label: 'Cash', synthetic: true },
  { key: 'unrealized', phase: 'unrealized', label: 'Unrealized', synthetic: true },
];

/**
 * Donut: the whole account right now. This is the *one* capital breakdown with
 * figures -- the Capital deployed chart above keeps only a colour key, because
 * this donut's components ARE that chart's bands (same labels), just grouped by
 * wheel phase instead of by instrument, and they reconcile to that chart's
 * latest day to the cent. Wedges of a phase share a colour and sit together;
 * there is no separate summary ring (for four of the six phases it would just
 * repeat one wedge).
 *
 * The components (label = matching Capital deployed band):
 *   Put collateral
 *   Idle shares (cost basis)                 -- no call written
 *   Covered-call shares (cost basis)         -- real basis
 *   Covered-call shares (strike estimate)    -- pre-export proxy, faded
 *   Long-option debit + Net spread collateral (phase "Hedged / Other")
 *   Cash / Unrealized -- from `net_worth`; Total value minus deployed capital,
 *     split so neither overclaims (Cash = account cash not already reserved as
 *     put collateral; Unrealized = the market-value-vs-cost-basis remainder).
 *     Only shown once a Positions snapshot is loaded, and dated to that
 *     snapshot, which can trail the chart's latest transaction day by a few
 *     days -- the committed components still match exactly.
 *
 * `wheelState` is `wheel_state_breakdown()` (wheel/metrics.py), computed from
 * the capital-scoped cycle set so the deployed total always matches the
 * "Capital deployed" figure regardless of the active date filter.
 */
function drawWheelState(wheelState, netWorth) {
  const svg = $('chart-wheel-state');
  const legend = $('legend-wheel-state');
  clear(legend);
  legend.classList.add('legend-stack');

  const rawBuckets = (wheelState && wheelState.buckets) || {};
  const rawParts = (wheelState && wheelState.parts) || {};
  const activeCycles = (wheelState && wheelState.active_cycles) || 0;

  const phaseMeta = {};
  for (const key of Object.keys(WHEEL_STATE_PHASE_META)) {
    const b = rawBuckets[key] || {};
    phaseMeta[key] = { cycles: b.cycles || 0, tickers: b.tickers || [], amount: b.amount || 0 };
  }

  const deployed =
    phaseMeta.puts.amount + phaseMeta.calls.amount + phaseMeta.holding.amount + phaseMeta.other.amount;
  const netWorthTotals = netWorth && netWorth.available ? netWorth.combined || netWorth : null;
  const totalValue = netWorthTotals && netWorthTotals.total_value != null ? netWorthTotals.total_value : null;
  const cashTotal = netWorthTotals && netWorthTotals.cash_total != null ? netWorthTotals.cash_total : null;
  const gap = totalValue !== null ? Math.max(totalValue - deployed, 0) : 0;
  const cash = cashTotal !== null ? Math.min(Math.max(cashTotal - phaseMeta.puts.amount, 0), gap) : 0;
  const unrealized = Math.max(gap - cash, 0);

  const hasParts = Object.keys(rawParts).length > 0;
  let components;
  if (hasParts) {
    const amt = { ...rawParts, cash, unrealized };
    components = WHEEL_STATE_COMPONENTS.map((c) => ({
      ...c,
      amount: amt[c.key] || 0,
      phaseLabel: WHEEL_STATE_PHASE_META[c.phase].label,
      colorVar: WHEEL_STATE_PHASE_META[c.phase].varName,
    })).filter((c) => c.amount > 1e-9);
  } else {
    // Older payload with no `parts`: one wedge per phase bucket + cash/unrealized.
    components = ['puts', 'holding', 'calls', 'other', 'cash', 'unrealized']
      .map((key) => {
        const m = WHEEL_STATE_PHASE_META[key];
        const amount = key === 'cash' ? cash : key === 'unrealized' ? unrealized : phaseMeta[key].amount;
        return {
          key,
          phase: key,
          label: m.label,
          amount,
          phaseLabel: m.label,
          colorVar: m.varName,
          synthetic: key === 'cash' || key === 'unrealized',
        };
      })
      .filter((c) => c.amount > 1e-9);
  }

  const grandTotal = components.reduce((s, c) => s + c.amount, 0);
  const tableHead = ['Phase', 'Component', 'Capital', 'Share of total'];

  // This donut shares the Capital deployed card, so an empty state hides only
  // its own subsection -- never the whole card and the history chart with it.
  const block = $('wheel-state-block');
  if (!activeCycles || grandTotal <= 1e-9) {
    clear(svg);
    svg.removeAttribute('aria-label');
    buildTable('wheel-state-table', tableHead, []);
    if (block) block.hidden = true;
    return;
  }
  if (block) block.hidden = false;

  // Every wedge/row is named by its component label -- the exact same label the
  // Capital deployed colour key uses for the matching band, so the two read as
  // one vocabulary. The phase name is kept only for the bold group header that
  // sits above a phase with more than one visible component (Covered-call
  // shares) and for the table's Phase column.
  const phaseCount = {};
  components.forEach((c) => (phaseCount[c.phase] = (phaseCount[c.phase] || 0) + 1));
  const displayLabel = (c) => c.label;

  // Nested in the (full-width) Capital deployed card, but the donut is a
  // fixed-radius mark -- its `.chart-scroll` is capped ~460px, so ask for a
  // width that fits rather than the 520 chart minimum and a downscale.
  const width = chartWidth(svg, 320);
  const rOuter = 100;
  const rInner = 56;
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
  const arcPoint = (a, r) => [cx + r * Math.cos(a), cy + r * Math.sin(a)];
  const wedgePath = (start, end) => {
    if (end - start >= Math.PI * 2 - 1e-6) {
      return (
        `M${cx - rOuter},${cy} A${rOuter},${rOuter} 0 1 1 ${cx + rOuter},${cy} A${rOuter},${rOuter} 0 1 1 ${cx - rOuter},${cy} Z ` +
        `M${cx - rInner},${cy} A${rInner},${rInner} 0 1 0 ${cx + rInner},${cy} A${rInner},${rInner} 0 1 0 ${cx - rInner},${cy} Z`
      );
    }
    const [x1, y1] = arcPoint(start, rOuter);
    const [x2, y2] = arcPoint(end, rOuter);
    const [x3, y3] = arcPoint(end, rInner);
    const [x4, y4] = arcPoint(start, rInner);
    const large = end - start > Math.PI ? 1 : 0;
    return `M${x1},${y1} A${rOuter},${rOuter} 0 ${large} 1 ${x2},${y2} L${x3},${y3} A${rInner},${rInner} 0 ${large} 0 ${x4},${y4} Z`;
  };

  const tableRows = [];
  let angle = -Math.PI / 2; // 12 o'clock, clockwise
  components.forEach((c) => {
    const share = c.amount / grandTotal;
    const end = angle + share * Math.PI * 2;
    const color = cssVar(c.colorVar);
    const path = svgEl('path', {
      class: 'mark',
      d: wedgePath(angle, end),
      fill: color,
      'fill-opacity': c.estimate ? 0.42 : 1,
      stroke: surface,
      'stroke-width': 2,
      'stroke-linejoin': 'round',
    });
    group.appendChild(path);

    const meta = phaseMeta[c.phase] || { cycles: 0, tickers: [] };
    attachTip(
      path,
      displayLabel(c),
      [
        { label: c.label, value: money(c.amount) },
        { label: 'Share of total', value: pct(share * 100, 1) },
        ...(phaseCount[c.phase] > 1 ? [{ label: 'Part of', value: c.phaseLabel }] : []),
        ...(c.synthetic
          ? []
          : [
              { label: 'Cycles with capital here', value: String(meta.cycles) },
              { label: 'Tickers', value: [...meta.tickers].sort().join(', ') || '—' },
            ]),
      ],
      c.key === 'cash'
        ? formula([
            'Cash = account cash - collateral already reserved for open',
            '  cash-secured puts (a hold against this same cash, not separate',
            "  money, so it's netted out here)",
            `= ${money(cashTotal)} - ${money(phaseMeta.puts.amount)} = ${money(c.amount)}`,
          ])
        : c.key === 'unrealized'
          ? formula([
              'Unrealized = Total value (Positions snapshot) - Capital deployed - Cash',
              `= ${money(totalValue)} - ${money(deployed)} - ${money(cash)} = ${money(c.amount)}`,
              '',
              "Unrealized gains vs. cost basis, plus equity this history can't track.",
            ])
          : c.estimate
            ? formula([
                `${c.label} = strike × 100 × contracts, a stand-in for shares bought`,
                "  before this export begins so their real cost basis isn't known.",
                '  Drawn faded because it is an estimate.',
                `= ${money(c.amount)} of ${money(grandTotal)} total = ${pct(share * 100, 1)}`,
              ])
            : formula([
                `${c.label} = ${money(c.amount)} of ${money(grandTotal)} total = ${pct(share * 100, 1)}`,
              ])
    );

    tableRows.push([c.phaseLabel, c.label, money(c.amount), pct(share * 100, 1)]);
    angle = end;
  });

  group.appendChild(
    svgEl(
      'text',
      { x: cx, y: cy - 4, 'text-anchor': 'middle', style: 'fill: var(--text-primary); font-weight: 700; font-size: 16px;' },
      compactMoney(grandTotal)
    )
  );
  group.appendChild(
    svgEl('text', { x: cx, y: cy + 14, 'text-anchor': 'middle', class: 'tick-label' }, 'total')
  );

  svg.setAttribute(
    'aria-label',
    `${totalValue !== null ? 'Account value if liquidated today' : 'Current wheel capital'}: ${components
      .map((c) => `${displayLabel(c)} ${pct((c.amount / grandTotal) * 100, 0)}`)
      .join(', ')}. Total ${money(grandTotal)}, ${activeCycles} active cycle(s).`
  );

  // The page's one figured capital breakdown: the deployed components (same
  // labels as the Capital deployed bands), then a Total committed subtotal,
  // then Cash / Unrealized, then Total value -- every figure rounded to whole
  // dollars and summed from the rounded parts so the column always adds up.
  const r = (v) => Math.round(v);
  const row = (label, value, opts) => capitalBreakdownRow(legend, label, value, opts);

  const asOfIso = netWorth && netWorth.as_of;
  legend.appendChild(
    el(
      'span',
      { class: 'legend-caption' },
      `${activeCycles} active cycle(s)` + (asOfIso ? ` · as of ${longDate(String(asOfIso).slice(0, 10))}` : '')
    )
  );
  const shownPhaseHeader = new Set();
  let committed = 0;
  components
    .filter((c) => !c.synthetic)
    .forEach((c) => {
      committed += r(c.amount);
      if (phaseCount[c.phase] > 1) {
        if (!shownPhaseHeader.has(c.phase)) {
          shownPhaseHeader.add(c.phase);
          const subtotal = components
            .filter((x) => x.phase === c.phase)
            .reduce((s, x) => s + r(x.amount), 0);
          row(c.phaseLabel, subtotal, { color: cssVar(c.colorVar), strong: true });
        }
        row(c.label, r(c.amount), { color: cssVar(c.colorVar), faded: c.estimate, indent: true });
      } else {
        row(displayLabel(c), r(c.amount), { color: cssVar(c.colorVar) });
      }
    });
  row('Total committed', committed, { kind: 'line', color: cssVar('--text-primary'), strong: true });

  const cashR = r(cash);
  const unrealR = r(unrealized);
  if (cashR > 0 || unrealR > 0) {
    row('Cash', cashR, { color: cssVar(NOT_DEPLOYED_COLOR) });
    row('Unrealized', unrealR, { color: cssVar(UNREALIZED_COLOR) });
    row('Total value', committed + cashR + unrealR, {
      kind: 'dashed',
      color: cssVar('--text-primary'),
      strong: true,
    });
  }

  buildTable('wheel-state-table', tableHead, tableRows);
}

/* --------------------------------------------- chart: cumulative P/L lines */

function drawPnl(series) {
  const svg = $('chart-pnl');
  const legend = $('legend-pnl');
  clear(legend);
  if (!series.length) {
    clear(svg);
    toggleChartCard(svg, false);
    return;
  }
  toggleChartCard(svg, true);

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
      'Dated to when cash actually settles; premium sold, dividends',
      '  received, a roll\'s debit; not to when the underlying position',
      '  finally closes.',
    ]),
  },
  {
    text: 'Wheel realized P/L',
    title: formula([
      'This month\'s share of realized wheel P/L (profit/loss): option P/L',
      '  plus stock P/L, dated to when a leg actually closes or a share lot',
      '  is sold, never to when the premium was originally collected.',
    ]),
  },
  'Avg collateral',
  'Yield %',
];

const monthLabel = (period) => {
  const [year, month] = period.split('-').map(Number);
  return new Date(year, month - 1, 1).toLocaleDateString('en-US', { month: 'short', year: 'numeric' });
};

// `period` here is an ISO week-start date ("YYYY-MM-DD", a Monday) from
// `wheel.cashflow.weekly_cashflow_series`. Short form ("Aug 4") for a
// crowded x-axis; the range form ("Aug 4 – 10, 2025", or "Aug 28 – Sep 3,
// 2025" across a month boundary) for tooltips and the table, where there is
// room to be unambiguous about which seven days a row covers.
const weekLabel = (period) => {
  const [year, month, day] = period.split('-').map(Number);
  return new Date(year, month - 1, day).toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
};

const weekRangeLabel = (weekStart, weekEnd) => {
  const [sy, sm, sd] = weekStart.split('-').map(Number);
  const [ey, em, ed] = weekEnd.split('-').map(Number);
  const start = new Date(sy, sm - 1, sd);
  const end = new Date(ey, em - 1, ed);
  const startText = start.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
  const endText = end.toLocaleDateString('en-US', {
    month: sm === em ? undefined : 'short',
    day: 'numeric',
    year: 'numeric',
  });
  return `${startText}, ${endText}`;
};

// Re-derives an ISO week anchor on the client, to line `pnl_series` (daily,
// dated to when a leg closes) up with the weekly cash-flow rows the backend
// already bucketed. `Date#getDay()` is 0 for Sunday, so `(getDay() + 6) % 7`
// is 0 for Monday -- the offset back to the week's Monday, matching
// `wheel.cashflow._week_start`.
const weekStartKey = (iso) => {
  const [year, month, day] = iso.slice(0, 10).split('-').map(Number);
  const dt = new Date(year, month - 1, day);
  dt.setDate(dt.getDate() - ((dt.getDay() + 6) % 7));
  const mm = String(dt.getMonth() + 1).padStart(2, '0');
  const dd = String(dt.getDate()).padStart(2, '0');
  return `${dt.getFullYear()}-${mm}-${dd}`;
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
 * The weekly counterpart of `monthlyWheelPl`: the same daily `pnl_series`
 * bucketed to ISO week starts (`weekStartKey`) so it lines up 1:1 with the
 * weekly cash-flow rows from `wheel.cashflow.weekly_cashflow_series`, whose
 * `period` is already a Monday ISO date. Used only by the cash-flow-vs-wheel
 * gap chart, which trades the monthly chart's collateral/yield context for
 * finer x-axis resolution.
 */
function weeklyWheelPl(rows, pnlSeries) {
  const byWeek = new Map();
  for (const point of pnlSeries || []) {
    const period = weekStartKey(point.date);
    const slot = byWeek.get(period) || { option_pl: 0, stock_pl: 0, total_pl: 0 };
    slot.option_pl += point.option_pl;
    slot.stock_pl += point.stock_pl;
    slot.total_pl += point.total_pl;
    byWeek.set(period, slot);
  }
  return rows.map((row) => byWeek.get(row.period) || { option_pl: 0, stock_pl: 0, total_pl: 0 });
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
    toggleChartCard(svg, false);
    return;
  }
  toggleChartCard(svg, true);

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
        `= ${row.monthly_yield_pct === null ? 'N/A; no collateral committed this month' : pct(row.monthly_yield_pct, 2)}`,
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
      `${monthLabel(row.period)}, wheel realized P/L`,
      [
        { label: 'Premium collected (net)', value: money(wp.option_pl, { cents: true }) },
        { label: 'Stock P/L', value: money(wp.stock_pl, { cents: true }) },
        { label: 'Wheel realized P/L', value: money(wp.total_pl, { cents: true }) },
        { label: 'Net cash flow (this month)', value: money(row.net_cash_flow, { cents: true }) },
      ],
      formula([
        'Wheel realized P/L = Premium collected (net) + Stock P/L',
        '  Dated to when a leg closes or a share lot is sold,',
        '  not to when premium was sold (unlike Net cash flow).',
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
  cashSwatchItem.appendChild(document.createTextNode('Net cash flow; blue credit, red debit'));
  legend.appendChild(cashSwatchItem);

  const wheelSwatchItem = el('span');
  const wheelSwatch = el('i');
  wheelSwatch.style.background = wheelColor;
  wheelSwatchItem.appendChild(wheelSwatch);
  wheelSwatchItem.appendChild(document.createTextNode('Wheel realized P/L'));
  legend.appendChild(wheelSwatchItem);

  if (avgIncome !== null && avgIncome !== undefined) {
    legend.appendChild(
      el('span', { class: 'legend-note' }, `Dashed red = avg monthly income (${money(avgIncome, { cents: true })}).`)
    );
  }
  if (avgWheelPl !== null && avgWheelPl !== undefined) {
    legend.appendChild(
      el('span', { class: 'legend-note' }, `Dotted orange = avg wheel realized P/L (${money(avgWheelPl, { cents: true })}).`)
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

/* ------------------------------------------------- chart: periodic P/L */

const PERIOD_PL_TABLE_HEAD = [
  'Period',
  {
    text: 'Net Premium',
    title: 'Realized option P/L (CSP + covered-call + hedge legs) that closed in this period.',
  },
  { text: 'Closed P/L', title: 'Realized stock P/L from shares sold or called away in this period.' },
  { text: 'Net P/L', title: 'Net Premium + Closed P/L, realized only; matches Net Realized P/L elsewhere.' },
];

// One fixed identity color per metric, regardless of sign -- a bar's own
// height/direction from the zero line already shows profit vs. loss
// unambiguously, so color here answers "which metric," not "up or down,"
// the same discipline drawCashFlow's fixed wheel-color bar already follows.
const PERIOD_PL_SERIES = [
  { key: 'net_premium', label: 'Net Premium', varName: '--series-1' },
  { key: 'closed_pl', label: 'Closed P/L', varName: '--series-2' },
  { key: 'net_pl', label: 'Net P/L', varName: '--series-3' },
];

const periodPlLabel = (row, granularity) => (granularity === 'week' ? weekLabel(row.period) : monthLabel(row.period));
const periodPlRangeLabel = (row, granularity) =>
  granularity === 'week' ? weekRangeLabel(row.week_start, row.week_end) : monthLabel(row.period);

/**
 * One grouped-bar cluster per period -- Net Premium, Closed P/L, and their
 * realized-only sum Net P/L. Each series keeps a fixed identity color; sign
 * is read from a bar's own direction off the zero line, never from color.
 * Granularity ('week'/'month') is a view toggle, not a filter -- both series
 * are already in `periodPl` (data.period_pl), so switching redraws from
 * already-fetched data.
 */
function drawPeriodPl(periodPl) {
  const svg = $('chart-period-pl');
  const legend = $('legend-period-pl');
  clear(legend);
  const granularity = state.periodPlGranularity;
  const rows = (periodPl && periodPl[granularity === 'week' ? 'weeks' : 'months']) || [];

  if (!rows.length) {
    clear(svg);
    svg.removeAttribute('aria-label');
    buildTable('period-pl-table', PERIOD_PL_TABLE_HEAD, []);
    toggleChartCard(svg, false);
    return;
  }
  toggleChartCard(svg, true);

  const margin = { top: 14, right: 20, bottom: 30, left: 62 };
  const width = chartWidth(svg);
  const height = 260;
  const values = rows.flatMap((row) => PERIOD_PL_SERIES.map((series) => row[series.key] || 0));
  const yMin = Math.min(0, ...values) * 1.15;
  const yMax = Math.max(0, ...values, 1) * 1.15;

  const { group, plotWidth, plotHeight, y } = frame(svg, { width, height, margin, yMin, yMax });
  const zeroY = y(0);
  group.appendChild(
    svgEl('line', { class: 'axis-line', x1: margin.left, x2: margin.left + plotWidth, y1: zeroY, y2: zeroY })
  );

  const slot = plotWidth / rows.length;
  const groupGap = 2;
  const groupWidth = Math.max(16, Math.min(56, slot * 0.7));
  const barWidth = Math.max(2, (groupWidth - groupGap * (PERIOD_PL_SERIES.length - 1)) / PERIOD_PL_SERIES.length);
  const colors = PERIOD_PL_SERIES.map((series) => cssVar(series.varName));

  rows.forEach((row, index) => {
    const cx = margin.left + slot * (index + 0.5);
    const groupStart = cx - groupWidth / 2;

    PERIOD_PL_SERIES.forEach((series, seriesIndex) => {
      const value = row[series.key] || 0;
      const x = groupStart + seriesIndex * (barWidth + groupGap);
      const top = value >= 0 ? y(value) : zeroY;
      const barHeight = Math.max(Math.abs(y(value) - zeroY), value === 0 ? 0 : 1.5);
      const rect = svgEl('rect', {
        class: 'mark',
        x,
        y: top,
        width: barWidth,
        height: barHeight,
        rx: 2,
        fill: colors[seriesIndex],
      });
      group.appendChild(rect);

      attachTip(
        rect,
        periodPlRangeLabel(row, granularity),
        PERIOD_PL_SERIES.map((s, i) => ({
          label: s.label,
          value: money(row[s.key], { cents: true, sign: true }),
          color: colors[i],
          valueClass: (row[s.key] || 0) < 0 ? 'neg' : 'pos',
        })),
        series.key === 'net_pl'
          ? formula([
              'Net P/L = Net Premium + Closed P/L',
              `= ${money(row.net_premium, { cents: true })} + ${money(row.closed_pl, { cents: true })}`,
              `= ${money(row.net_pl, { cents: true })}`,
            ])
          : undefined
      );
    });
  });

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
        periodPlLabel(row, granularity)
      )
    );
  });

  PERIOD_PL_SERIES.forEach((series, i) => {
    const item = el('span');
    const swatch = el('i');
    swatch.style.background = colors[i];
    item.appendChild(swatch);
    item.appendChild(document.createTextNode(series.label));
    legend.appendChild(item);
  });
  legend.appendChild(
    el(
      'span',
      { class: 'legend-note' },
      'Above zero = profit, below = loss. Color = metric, not sign.'
    )
  );

  svg.setAttribute(
    'aria-label',
    `Periodic P/L, ${rows.length} ${granularity === 'week' ? 'week(s)' : 'month(s)'} from ` +
      `${periodPlLabel(rows[0], granularity)} to ${periodPlLabel(rows[rows.length - 1], granularity)}. ` +
      'Hover or focus a bar for its breakdown.'
  );

  buildTable(
    'period-pl-table',
    PERIOD_PL_TABLE_HEAD,
    rows.map((row) => [
      periodPlRangeLabel(row, granularity),
      money(row.net_premium, { cents: true, sign: true }),
      money(row.closed_pl, { cents: true, sign: true }),
      money(row.net_pl, { cents: true, sign: true }),
    ])
  );
}

/* --------------------------------------- chart: cash-flow vs. wheel P/L gap */

const GAP_TABLE_HEAD = ['Week', 'Net cash flow', 'Wheel realized P/L', 'Weekly gap', 'Cumulative gap'];

/**
 * The running difference between cash actually collected (net cash flow,
 * dated to settlement) and wheel P/L actually realized (dated to close) --
 * see that chart's own hint for why the two diverge. Bucketed by ISO week,
 * not month, for finer x-axis resolution: bars are the weekly gap
 * (diverging, one categorical slot per week); the line overlaid on the same
 * x positions is its running total, drawn as a rolling-mean trend so the
 * sawtooth from lumpy weekly realizations doesn't drown the signal -- a
 * sustained rise means collected premium is piling up in still-open positions
 * faster than it's being realized, not necessarily a problem but the thing
 * worth watching for. The dot, its label, every tooltip and the table keep
 * the exact running total; only the line is smoothed. Weekly cash-flow rows
 * come from `wheel.cashflow.weekly_cashflow_series`
 * (via `cash_flow.weeks`); weekly wheel P/L is `pnl_series` rebucketed by
 * `weeklyWheelPl`.
 */
function drawCashFlowGap(rows, pnlSeries) {
  const svg = $('chart-gap');
  const legend = $('legend-gap');
  clear(legend);
  if (!rows.length) {
    clear(svg);
    svg.removeAttribute('aria-label');
    buildTable('gap-table', GAP_TABLE_HEAD, []);
    toggleChartCard(svg, false);
    return;
  }
  toggleChartCard(svg, true);

  const wheelPl = weeklyWheelPl(rows, pnlSeries);
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
      weekRangeLabel(row.week_start, row.week_end),
      [
        { label: 'Net cash flow', value: money(row.net_cash_flow, { cents: true }) },
        { label: 'Wheel realized P/L', value: money(wheelPl[index].total_pl, { cents: true }) },
        { label: 'Weekly gap', value: money(value, { cents: true }) },
        { label: 'Cumulative gap', value: money(cumGaps[index], { cents: true }) },
      ],
      formula([
        'Weekly gap = Net cash flow - Wheel realized P/L',
        `= ${money(row.net_cash_flow, { cents: true })} - ${money(wheelPl[index].total_pl, { cents: true })}`,
        `= ${money(value, { cents: true })}`,
        '',
        "Cumulative gap = running total of every week's gap so far",
        `= ${money(cumGaps[index], { cents: true })}`,
      ])
    );
  });

  // The running total is a sawtooth -- the weekly gaps it sums land in lumps.
  // Draw it as a shrinking-window centered mean so it reads as a clean trend;
  // endpoints stay exact, and the dot/label/tooltips/table all keep the true
  // running total.
  const smoothGaps = smoothSeries(cumGaps, 4);
  const linePath = centers.map((cx, index) => `${cx},${y(smoothGaps[index])}`).join('L');
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
        weekLabel(row.period)
      )
    );
  });

  svg.setAttribute(
    'aria-label',
    `Cash flow versus wheel P/L gap, ${rows.length} week(s). Cumulative gap now ` +
      `${money(cumGaps[lastIndex], { cents: true })}. Hover a bar for its week, or the line's end for the running total.`
  );

  const barSwatchItem = el('span');
  const barSwatch = el('i');
  barSwatch.style.background = `linear-gradient(90deg, ${positive} 50%, ${negative} 50%)`;
  barSwatchItem.appendChild(barSwatch);
  barSwatchItem.appendChild(
    document.createTextNode('Weekly gap; blue: cash flow ahead, red: P/L ahead')
  );
  legend.appendChild(barSwatchItem);

  const lineSwatchItem = el('span');
  const lineSwatch = el('i', { class: 'line' });
  lineSwatch.style.background = lineColor;
  lineSwatchItem.appendChild(lineSwatch);
  lineSwatchItem.appendChild(document.createTextNode('Cumulative gap (smoothed)'));
  lineSwatchItem.appendChild(el('b', { class: 'legend-value' }, money(cumGaps[lastIndex], { cents: true })));
  legend.appendChild(lineSwatchItem);
  legend.appendChild(
    el('span', { class: 'legend-note' }, 'Line is a rolling-mean trend; hover a bar or open the table for each week’s exact running total.')
  );

  buildTable(
    'gap-table',
    GAP_TABLE_HEAD,
    rows.map((row, index) => [
      weekRangeLabel(row.week_start, row.week_end),
      money(row.net_cash_flow, { cents: true }),
      money(wheelPl[index].total_pl, { cents: true }),
      money(gaps[index], { cents: true }),
      money(cumGaps[index], { cents: true }),
    ])
  );
}

const PPD_TABLE_HEAD = ['Week', "Week's option P/L", "Week's PPD", 'Cumulative option P/L', 'Days', 'PPD to date'];

const perDay = (value) => (value === null || value === undefined ? '—' : money(value, { cents: true }) + '/day');

/**
 * Wheel PPD by week. The cumulative-PPD line is the headline: its right edge is
 * the current PPD (equal to the Performance tile), read straight off the y-axis
 * via a dashed marker at that level. Weekly bars are secondary texture.
 *
 * `svgId` / `legendId` / `tableId` are parameterised so the Trade Log can reuse
 * this for a single wheel.
 */
function drawPpd(rows, { svgId = 'chart-ppd', legendId = 'legend-ppd', tableId = 'ppd-table' } = {}) {
  const svg = $(svgId);
  const legend = legendId ? $(legendId) : null;
  // Only the dashboard instance owns a whole card; the Trade Log's copy sits in
  // a shared card and is shown/hidden by drawTradeLogPpd instead.
  const ownsCard = svgId === 'chart-ppd';
  if (legend) clear(legend);
  if (!rows || !rows.length) {
    clear(svg);
    svg.removeAttribute('aria-label');
    if (tableId) buildTable(tableId, PPD_TABLE_HEAD, []);
    if (ownsCard) toggleChartCard(svg, false);
    return;
  }
  if (ownsCard) toggleChartCard(svg, true);

  const weekly = rows.map((row) => row.weekly_ppd);
  const cum = rows.map((row) => row.cum_ppd);
  const lastIndex = rows.length - 1;
  const currentPpd = cum[lastIndex];

  const margin = { top: 14, right: 52, bottom: 30, left: 62 };
  const width = chartWidth(svg);
  const height = 260;
  const values = [...weekly, ...cum, 0];
  const yMin = Math.min(...values) * 1.15;
  const yMax = Math.max(...values, 1) * 1.15;

  const { group, plotWidth, plotHeight, y } = frame(svg, {
    width,
    height,
    margin,
    yMin,
    yMax,
    yFormat: (v) => compactMoney(v) + '/d',
  });

  const slot = plotWidth / rows.length;
  const barWidth = Math.max(3, Math.min(22, slot * 0.4));
  const positive = cssVar('--pos');
  const negative = cssVar('--neg');
  const lineColor = cssVar('--series-1');
  const zeroY = y(0);
  const centers = rows.map((_, index) => margin.left + slot * (index + 0.5));

  group.appendChild(
    svgEl('line', { class: 'axis-line', x1: margin.left, x2: margin.left + plotWidth, y1: zeroY, y2: zeroY })
  );

  rows.forEach((row, index) => {
    const cx = centers[index];
    const value = weekly[index];
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
      'fill-opacity': 0.55,
    });
    group.appendChild(rect);
    attachTip(
      rect,
      weekRangeLabel(row.week_start, row.week_end),
      [
        { label: "Week's realized option P/L", value: money(row.option_pl, { cents: true }) },
        { label: "Week's PPD", value: perDay(row.weekly_ppd) },
        { label: 'PPD to date', value: perDay(row.cum_ppd) },
      ],
      formula([
        "Week's PPD = realized option P/L that week ÷ 7",
        `= ${money(row.option_pl, { cents: true })} ÷ 7`,
        `= ${perDay(row.weekly_ppd)}`,
        '',
        'PPD to date = cumulative option P/L ÷ days since the first cycle opened',
        `= ${money(row.cum_option_pl, { cents: true })} ÷ ${row.cum_days}`,
        `= ${perDay(row.cum_ppd)}`,
      ])
    );
  });

  // Dashed marker at the current PPD level, so it can be read off the y-axis.
  group.appendChild(
    svgEl('line', {
      x1: margin.left,
      x2: margin.left + plotWidth,
      y1: y(currentPpd),
      y2: y(currentPpd),
      stroke: lineColor,
      'stroke-width': 1,
      'stroke-dasharray': '3 3',
      'stroke-opacity': 0.6,
    })
  );

  const linePath = centers.map((cx, index) => `${cx},${y(cum[index])}`).join('L');
  group.appendChild(
    svgEl('path', {
      d: 'M' + linePath,
      fill: 'none',
      stroke: lineColor,
      'stroke-width': 2.5,
      'stroke-linejoin': 'round',
      'stroke-linecap': 'round',
    })
  );
  group.appendChild(
    svgEl('circle', {
      cx: centers[lastIndex],
      cy: y(currentPpd),
      r: 4.5,
      fill: lineColor,
      stroke: cssVar('--surface-1'),
      'stroke-width': 2,
    })
  );
  group.appendChild(
    svgEl(
      'text',
      {
        x: Math.min(centers[lastIndex] + 8, margin.left + plotWidth + margin.right - 4),
        y: y(currentPpd) + 3.5,
        'text-anchor': centers[lastIndex] + 8 > margin.left + plotWidth ? 'end' : 'start',
        fill: 'var(--text-primary)',
        'font-weight': 700,
      },
      compactMoney(currentPpd) + '/d'
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
        weekLabel(row.period)
      )
    );
  });

  svg.setAttribute(
    'aria-label',
    `Wheel PPD by week, ${rows.length} week(s). PPD to date is now ${perDay(currentPpd)}. ` +
      `Hover a bar for that week, or the line for the running figure.`
  );

  if (legend) {
    const lineItem = el('span');
    const lineSwatch = el('i', { class: 'line' });
    lineSwatch.style.background = lineColor;
    lineItem.appendChild(lineSwatch);
    lineItem.appendChild(document.createTextNode('PPD to date'));
    lineItem.appendChild(el('b', { class: 'legend-value' }, perDay(currentPpd)));
    legend.appendChild(lineItem);

    const barItem = el('span');
    const barSwatch = el('i');
    barSwatch.style.background = `linear-gradient(90deg, ${positive} 50%, ${negative} 50%)`;
    barSwatch.style.opacity = '0.55';
    barItem.appendChild(barSwatch);
    barItem.appendChild(document.createTextNode("This week's PPD"));
    legend.appendChild(barItem);
  }

  if (tableId) {
    buildTable(
      tableId,
      PPD_TABLE_HEAD,
      rows.map((row) => [
        weekRangeLabel(row.week_start, row.week_end),
        money(row.option_pl, { cents: true }),
        perDay(row.weekly_ppd),
        money(row.cum_option_pl, { cents: true }),
        String(row.cum_days),
        perDay(row.cum_ppd),
      ])
    );
  }
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
              'N/A, no collateral committed in the selected range.',
            ])
          : formula([
              'Annualized cash-on-cash return =',
              '  (Avg monthly income × 12) ÷ Avg collateral × 100',
              '  Avg collateral is a time-weighted average over every day in this range.',
              '',
              `= (${money(trailing.avg_monthly_income, { cents: true })} × 12) ÷ ${money(trailing.avg_collateral)} × 100`,
              `= ${pct(trailing.annualized_cash_on_cash_return_pct, 2)}`,
              '',
              'Counts all cash actually collected, including premium on still-open',
              '  legs, and dividends; not just profit/loss on legs already closed.',
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
        'Positive: premium collected is running ahead of what has been realized,',
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
  { valueOf, format, tipRows, tipFormula, tableId, tableHead, tableRow, legendId, legendNote, onRowClick }
) {
  const svg = $(svgId);
  const legend = legendId ? $(legendId) : null;
  if (legend) clear(legend);
  if (!rows.length) {
    clear(svg);
    toggleChartCard(svg, false);
    return;
  }
  toggleChartCard(svg, true);

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

    const nameLabel = svgEl(
      'text',
      {
        x: margin.left - 10,
        y: top + rowHeight / 2 + 4,
        'text-anchor': 'end',
        fill: 'var(--text-secondary)',
        'font-weight': 600,
      },
      row.underlying + (row.capital_estimated ? ' ~' : '')
    );
    if (onRowClick) {
      nameLabel.style.cursor = 'pointer';
      nameLabel.addEventListener('click', () => onRowClick(row));
    }
    group.appendChild(nameLabel);

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
    if (onRowClick) {
      // `.hit` already shows a pointer cursor and takes focus (attachTip sets
      // tabindex); wire both mouse and keyboard so the click-through is
      // reachable either way.
      hit.addEventListener('click', () => onRowClick(row));
      hit.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          onRowClick(row);
        }
      });
    }
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
    for (const note of [].concat(legendNote || [])) {
      legend.appendChild(el('span', { class: 'legend-note' }, note));
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
    toggleChartCard(svg, false);
    return;
  }
  toggleChartCard(svg, true);

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
            'N/A, no capital committed.',
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

// Same capital vocabulary as the Capital-deployed chart and the wheel-state
// donut: put collateral = slot 1, covered-call shares = slot 3, long-option
// debit = slot 4. Held shares get slot 2 (see drawTimelineStock).
const LEG_STYLES = {
  CSP: { label: 'Cash-secured put', varName: '--series-1' },
  COVERED_CALL: { label: 'Covered call', varName: '--series-3' },
  LONG_PUT: { label: 'Long put', varName: '--series-4' },
  LONG_CALL: { label: 'Long call', varName: '--series-4' },
};

/**
 * One share lot on the Wheel-timelines chart: a bar over the days it was held
 * (--series-2, the orange the "Capital deployed" chart and the "Holding Shares"
 * donut wedge both use for shares held at cost basis), a filled square at the
 * buy, and an outlined square at each sale / call-away. This is the only mark a
 * plain buy-and-hold cycle has.
 */
function drawTimelineStock(group, cycle, lot, y, laneHeight, x, through, surface, onClick) {
  const color = cssVar('--series-2');
  const sells = (lot.disposals || []).slice().sort((a, b) => (a.date < b.date ? -1 : 1));
  const held = lot.remaining > 1e-9 || !sells.length;
  const endIso = held ? through : sells[sells.length - 1].date;
  const x1 = x(lot.acquired);
  const x2 = Math.max(x(endIso), x1 + 3);
  const mid = y + (laneHeight - 1) / 2;

  group.appendChild(
    svgEl('rect', {
      class: 'mark',
      x: x1,
      y: y + 1.5,
      width: x2 - x1,
      height: laneHeight - 4,
      rx: 3,
      fill: color,
      'fill-opacity': lot.remaining > 1e-9 ? 0.35 : 0.75,
      stroke: surface,
      'stroke-width': 1.5,
    })
  );
  // ■ bought
  const s = 3.4;
  group.appendChild(
    svgEl('rect', { x: x1 - s, y: mid - s, width: s * 2, height: s * 2, fill: color, stroke: surface, 'stroke-width': 1.5 })
  );
  // □ sold / called away
  for (const d of sells) {
    const sx = x(d.date);
    group.appendChild(
      svgEl('rect', {
        x: sx - s,
        y: mid - s,
        width: s * 2,
        height: s * 2,
        fill: surface,
        stroke: color,
        'stroke-width': 1.75,
      })
    );
  }

  const realized = sells.reduce((sum, d) => sum + (typeof d.realized === 'number' ? d.realized : 0), 0);
  const showPl = sells.length && lot.basis_known !== false;

  // `lot.source` alone under-detects assignment: a broker-supplied
  // "YOU BOUGHT ASSIGNED PUTS ..." row is booked as an ordinary PURCHASE. Match
  // the acquire date against the cycle's ACQUIRE assignment events instead.
  const acquireDates = new Set(
    (cycle.assignment_events || []).filter((a) => a.direction === 'ACQUIRE').map((a) => a.date)
  );
  const assigned = lot.source === 'PUT_ASSIGNMENT' || acquireDates.has(lot.acquired);
  const preHistory = lot.source === 'PRE_HISTORY' || lot.basis_known === false;
  const strategy = assigned
    ? 'Assigned shares'
    : preHistory
    ? 'Shares (basis pre-dates export)'
    : cycle.kind === 'hold'
    ? 'Buy-and-hold'
    : 'Bought shares';

  const hit = svgEl('rect', { class: 'hit', x: x1 - 8, y: y - 2, width: x2 - x1 + 16, height: laneHeight + 4 });
  attachTip(
    hit,
    `${cycle.underlying} shares`,
    [
      { label: 'Strategy', value: strategy, color },
      { label: 'Bought', value: `${lot.acquired} · ${lot.shares} sh @ ${lot.basis_per_share ?? '—'}` },
      ...sells.map((d) => ({ label: 'Sold', value: `${d.date} · ${d.shares} sh @ ${d.price ?? '—'}` })),
      ...(showPl
        ? [
            {
              label: 'Realized P/L',
              value: money(realized, { cents: true, sign: true }),
              valueClass: realized > 0 ? 'pos' : realized < 0 ? 'neg' : '',
            },
          ]
        : []),
      { label: lot.remaining > 1e-9 ? 'Still held' : 'Position', value: lot.remaining > 1e-9 ? `${lot.remaining} shares` : 'closed' },
    ],
    null
  );
  hit.addEventListener('click', onClick);
  group.appendChild(hit);
}

function drawTimeline(cycles, through) {
  const svg = $('chart-timeline');
  const legend = $('legend-timeline');
  clear(legend);
  if (!cycles.length) {
    clear(svg);
    return;
  }

  // Label each row with the *real* wheel id and status from the Trade Log
  // (built from full history), so the two views agree even when a date filter
  // has renumbered or clipped the cycles here -- a wheel still open in full
  // history must not read "closed" just because the window cuts off its tail.
  const byStart = (a, b) => parseDay(a.start_date) - parseDay(b.start_date) || a.cycle_id.localeCompare(b.cycle_id);
  // 'time' order puts the newest wheel on top, so recent activity reads first.
  const byStartDesc = (a, b) => -byStart(a, b);
  const byTicker = (a, b) => a.underlying.localeCompare(b.underlying) || byStart(a, b);
  const ordered = cycles
    .slice()
    .map((cycle) => {
      const match = matchTradeLogWheel(cycle);
      return match
        ? {
            ...cycle,
            cycle_id: match.cycle_id,
            status: match.status,
            is_wheel: match.is_wheel,
            kind: match.kind,
          }
        : cycle;
    })
    .sort(state.timelineSort === 'time' ? byStartDesc : byTicker);

  const endOf = (leg) => leg.close_date || through;
  const laneHeight = 11;
  const margin = { top: 10, right: 18, bottom: 30, left: 108 };
  const DAY = 86400000;

  // Pack each cycle's items -- option legs *and* share lots -- into sub-lanes so
  // overlapping positions stay legible. A share lot spans its acquisition to
  // its last disposal, or to `through` while any of it is still held; a
  // buy-and-hold cycle (no legs) shows only these.
  const laid = ordered.map((cycle) => {
    const items = [];
    for (const leg of cycle.legs) {
      items.push({
        kind: 'leg',
        leg,
        start: parseDay(leg.open_date).getTime(),
        finish: parseDay(endOf(leg)).getTime(),
      });
    }
    for (const lot of cycle.share_lots || []) {
      if (!(lot.shares > 0)) continue;
      const sells = (lot.disposals || []).map((d) => parseDay(d.date).getTime());
      const held = lot.remaining > 1e-9 || !sells.length;
      items.push({
        kind: 'stock',
        lot,
        start: parseDay(lot.acquired).getTime(),
        finish: held ? parseDay(through).getTime() : Math.max(...sells),
      });
    }
    items.sort((a, b) => a.start - b.start);

    const laneEnds = [];
    const placed = items.map((item) => {
      let lane = laneEnds.findIndex((end) => end <= item.start);
      if (lane === -1) {
        lane = laneEnds.length;
        laneEnds.push(0);
      }
      laneEnds[lane] = item.finish + DAY / 2;
      return { item, lane };
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
    rowHit.addEventListener('click', () => openTradeLog(row.cycle));
    group.appendChild(rowHit);

    // Status cues on the wheel id: CLOSED (terminal) is struck through;
    // NO_ACTIVITY (flat but resumable) gets an amber dot in the gutter;
    // ACTIVE gets nothing.
    const status = row.cycle.status;
    const nonWheel = row.cycle.is_wheel === false;
    const label = svgEl(
      'text',
      {
        x: margin.left - 10,
        y: top + rowHeight / 2 + 4,
        'text-anchor': 'end',
        fill: nonWheel || status === 'CLOSED' ? 'var(--text-muted)' : 'var(--text-secondary)',
        'font-weight': 600,
        'text-decoration': status === 'CLOSED' ? 'line-through' : 'none',
      },
      nonWheel ? row.cycle.cycle_id + ' ◇' : row.cycle.cycle_id
    );
    label.style.cursor = 'pointer';
    const titleText = nonWheel
      ? row.cycle.kind === 'hold'
        ? 'Buy-and-hold (non-wheel): shares only, no option ever written. Excluded from wheel-return figures.'
        : 'Directional (non-wheel): long options only. Excluded from wheel-return figures.'
      : statusLabel(status);
    label.appendChild(svgEl('title', {}, titleText));
    label.addEventListener('click', () => openTradeLog(row.cycle));
    group.appendChild(label);

    if (status === 'NO_ACTIVITY') {
      const dot = svgEl('circle', {
        cx: margin.left - 5,
        cy: top + rowHeight / 2 + 1,
        r: 3.2,
        fill: cssVar('--warning'),
      });
      dot.appendChild(svgEl('title', {}, 'NO ACTIVITY'));
      group.appendChild(dot);
    }

    row.placed.forEach(({ item, lane }) => {
      const y = top + lane * laneHeight;

      if (item.kind === 'stock') {
        drawTimelineStock(group, row.cycle, item.lot, y, laneHeight, x, through, surface, () =>
          openTradeLog(row.cycle, item.lot.acquired)
        );
        return;
      }

      const leg = item.leg;
      const style = LEG_STYLES[leg.strategy] || LEG_STYLES.CSP;
      const color = cssVar(style.varName);
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
        {
          label: 'Realized P/L',
          value: money(leg.realized_pl, { cents: true, sign: true }),
          valueClass: leg.realized_pl > 0 ? 'pos' : leg.realized_pl < 0 ? 'neg' : '',
        },
        { label: 'Collateral', value: money(leg.collateral) },
        ...(leg.opened_by_roll ? [{ label: 'Opened by', value: 'roll ' + leg.opened_by_roll }] : []),
        ...(closedByRolls.length ? [{ label: 'Closed by', value: 'roll ' + closedByRolls.join(', ') }] : []),
      ], legCollateralFormula(leg));
      hit.addEventListener('click', () => openTradeLog(row.cycle, leg.open_date));
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
  const stockItem = el('span');
  const stockSwatch = el('i');
  stockSwatch.style.background = cssVar('--series-2');
  stockItem.appendChild(stockSwatch);
  stockItem.appendChild(document.createTextNode('Shares held'));
  legend.appendChild(stockItem);
  legend.appendChild(el('span', {}, '▲ sell-open  ▼ buy-close  ◆ assign  ■ buy  □ sell'));
  legend.appendChild(el('span', {}, 'Faded = still open'));

  const statusItem = el('span');
  const naDot = el('i');
  naDot.style.background = cssVar('--warning');
  naDot.style.borderRadius = '50%';
  naDot.style.width = '8px';
  naDot.style.height = '8px';
  const closedText = el('span', {}, 'DCH-2026-1');
  closedText.style.textDecoration = 'line-through';
  closedText.style.color = 'var(--text-muted)';
  statusItem.append(
    naDot,
    document.createTextNode(' no activity   '),
    closedText,
    document.createTextNode(' closed')
  );
  legend.appendChild(statusItem);

  buildTable(
    'timeline-table',
    ['Cycle', 'Symbol', 'Strategy', 'Opened', 'Closed', 'Contracts', 'Outcome', 'Realized P/L'],
    ordered.flatMap((cycle) => {
      const legRows = cycle.legs.map((leg) => [
        cycle.cycle_id,
        leg.symbol,
        leg.strategy,
        leg.open_date,
        leg.close_date || '—',
        leg.contracts,
        leg.outcome,
        money(leg.realized_pl, { cents: true }),
      ]);
      const stockRows = (cycle.share_lots || [])
        .filter((lot) => lot.shares > 0)
        .map((lot) => {
          const sells = (lot.disposals || []).slice().sort((a, b) => (a.date < b.date ? -1 : 1));
          const realized = sells.reduce((sum, d) => sum + (typeof d.realized === 'number' ? d.realized : 0), 0);
          return [
            cycle.cycle_id,
            cycle.underlying,
            'SHARES',
            lot.acquired,
            lot.remaining > 1e-9 || !sells.length ? '—' : sells[sells.length - 1].date,
            lot.shares,
            lot.remaining > 1e-9 ? 'HELD' : 'SOLD',
            sells.length ? money(realized, { cents: true }) : '—',
          ];
        });
      return [...legRows, ...stockRows];
    })
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
        `  ${CAPITAL_FORMULA_SUM}`,
        `= ${CAPITAL_BANDS.map((c) => money(day1[c.key] || 0)).join(' + ')}`,
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
          'N/A, no capital committed in this cycle.',
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
          'N/A, no initial collateral committed in this cycle.',
        ])
      : formula([
          'Net Option Yield = Option P/L ÷ Initial collateral',
          '  Premium only, vs. day-one capital (not a time-weighted average).',
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
          'N/A, no initial collateral committed in this cycle.',
        ])
      : formula([
          'Total Position ROI = (Option P/L + Stock realized P/L +',
          '  Stock unrealized P/L + Dividends) ÷ Initial collateral',
          '  Open long-option (hedge) P/L is not marked; no quote feed.',
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
      ? formula(['Avg days in trade = mean(days held), over closed legs', '', 'N/A, no closed legs yet.'])
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
    '  Option premium only, never includes stock profit/loss.',
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
  ACTIVE: 'ACTIVE: something is still open; a short leg, a long hedge, or shares held.',
  NO_ACTIVITY: 'NO ACTIVITY: flat right now, but still within the same calendar year as the latest trade in the book; another put or call on this ticker would resume this wheel.',
  CLOSED: 'CLOSED: terminal. The year has turned since the last trade, or the stock was called away.',
};

const STATUS_LABELS = { ACTIVE: 'ACTIVE', NO_ACTIVITY: 'NO ACTIVITY', CLOSED: 'CLOSED' };
function statusLabel(status) {
  return STATUS_LABELS[status] || String(status || '').replace(/_/g, ' ');
}

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
    const tr = el('tr', {
      class:
        'cycle-row' +
        (state.expanded.has(cycle.cycle_id) ? ' open' : '') +
        (cycle.is_wheel === false ? ' non-wheel' : ''),
    });

    const idCell = el('td', { class: 'left ticker-cell' });
    idCell.appendChild(document.createTextNode(cycle.cycle_id));
    if (cycle.capital_estimated) {
      const flag = el('span', { class: 'est-flag', title: 'Includes a strike-based capital proxy' }, '~');
      idCell.appendChild(flag);
    }
    tr.appendChild(idCell);

    const statusCell = el('td', { class: 'left' });
    const statusBadge = el('span', { class: 'badge ' + cycle.status }, statusLabel(cycle.status));
    setFormula(statusBadge, CYCLE_STATUS_MEANING[cycle.status] || null);
    statusCell.appendChild(statusBadge);
    if (cycle.is_wheel === false) {
      const isHold = cycle.kind === 'hold';
      const tag = el('span', { class: 'badge DIRECTIONAL' }, isHold ? 'buy & hold' : 'directional');
      setFormula(
        tag,
        (isHold
          ? 'Shares only, no option has ever been written against them. Selling a covered call makes it a wheel. '
          : 'Long options only; no cash-secured put, covered call, shares or assignment. ') +
          'P&L counts toward every total, but the wheel-return ratios (Wheel ROC, PPD, ' +
          'Net Option Yield) are withheld and show as a dash.'
      );
      statusCell.appendChild(tag);
    }
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
          ' , a matched same-day short + long position whose collateral is',
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
        '  on their own, a covered call adds nothing on top of shares',
        '  already held.',
      ]);
    }
    return formula([
      ...pairedNote,
      'Collateral = Strike × 100 × Naked contracts',
      '  (proxy: these shares pre-date the export, so their real cost',
      '  basis is not visible, the strike stands in for it)',
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
  return formula(['Collateral = $0, this leg commits no capital.']);
}

/**
 * Right side of a Spread's netted collateral figure — mirrors
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
          'automatically, collateral nets to the strike distance instead of the short leg\'s full ' +
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
            'The raw assignment/purchase price, what a 1099-B would show.',
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
              : 'Tax basis unknown, shares pre-date this export.',
            lot.net_adjusted_cost_basis === null
              ? 'N/A, no strike to net against.'
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
          'no earlier than the one closed, re-entering the identical contract just closed is a round ' +
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
            'OUT_AND_UP / OUT_AND_DOWN: both, later expiry and a moved strike.',
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
      el('h3', {}, `Assignments (${cycle.assignment_events.length}), share legs synthesized at strike`)
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
              'N/A, no capital has been committed in this window yet.',
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
              'N/A, no capital was actively backing an open put or covered call',
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
              'N/A, no initial collateral committed in this window.',
            ])
          : formula([
              'Annualized Net Option Yield =',
              '  (Option P/L ÷ Total initial collateral) × (365 ÷ Days)',
              '  Denominator sums every cycle\'s day-one capital, not a time-weighted avg.',
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
              'N/A, no initial collateral committed in this window.',
            ])
          : formula([
              'Annualized Total Position ROI =',
              '  (Option P/L + Stock realized P/L + Stock unrealized P/L + Dividends)',
              '  ÷ Total initial collateral × (365 ÷ Days)',
              '  Open long-option (hedge) P/L is not marked; no quote feed.',
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
        `  ${CAPITAL_FORMULA_SUM}`,
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
              'N/A, no closed leg has a decided (non-zero) P/L yet.',
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
        '  Decided = a closed leg with a win or a loss (not open, not exact break-even).',
        '',
        `= ${portfolio.avg_days_in_trade === null ? 'N/A, no decided legs yet' : portfolio.avg_days_in_trade.toFixed(1) + ' days'}`,
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
          `than one file, ${detail}.`
      )
    );
  }

  for (const warning of meta.parse_warnings || []) {
    if (/transposed|newest-first|re-posted/.test(warning)) {
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

/* --------------------------------------------------------------- open hedges
 *
 * A bought protective put/call that is still open needs a decision: keep
 * selling premium against it, or wind it down before its time value is gone.
 * The banner is filter-independent and only renders when at least one such
 * leg exists, so it stays invisible until it matters. See
 * ``wheel/api.py``'s ``_open_hedge_entry`` for the phase / message rules.
 */
/** One hedge row, shared by the dashboard banner and the Trade Log block. */
function buildHedgeRow(h, { showAccount = true } = {}) {
  const row = el('div', { class: 'hedge-row ' + h.phase });
  row.style.cursor = 'pointer';
  row.addEventListener('click', () =>
    openTradeLog({
      underlying: h.underlying,
      account_id: h.account_id || null,
      start_date: h.opened,
      end_date: null,
      last_activity: h.opened,
    })
  );

  const main = el('div', { class: 'hedge-row-main' });
  main.appendChild(el('span', { class: 'hedge-chip' }, h.headline));
  const acct = showAccount && h.account_id ? h.account_id + ' · ' : '';
  main.appendChild(
    el('span', { class: 'hedge-title' }, `${acct}${h.underlying} · ${h.label} · exp ${h.expiry}`)
  );
  row.appendChild(main);

  // Facts line: what it cost, where the whole wheel stands right now (the
  // honest "are we winning" number), and how much time is left.
  const econ = el('div', { class: 'hedge-econ' });
  econ.appendChild(el('span', {}, `Hedge cost ${money(h.cost)}`));
  if (typeof h.wheel_pl_now === 'number') {
    econ.appendChild(
      el(
        'span',
        { class: h.wheel_pl_now >= 0 ? 'pos' : 'neg' },
        `wheel P&L now ${money(h.wheel_pl_now, { sign: true })}`
      )
    );
  }
  econ.appendChild(el('span', {}, `${h.days_to_expiry} days of protection left`));
  row.appendChild(econ);

  // Secondary, deliberately muted: premium written while the hedge has been
  // open is NOT proof it is paid for -- that premium may now be underwater
  // stock. Shown for context only.
  if (h.is_wheel && typeof h.premium_written_since === 'number') {
    const detail = el(
      'div',
      { class: 'hedge-detail' },
      `Short-put premium since ${h.opened}: ${money(h.premium_written_since, { sign: true })} (context only)`
    );
    row.appendChild(detail);
  } else if (!h.is_wheel) {
    row.appendChild(el('div', { class: 'hedge-detail' }, 'Directional, no wheel premium is offsetting its cost.'));
  }

  row.appendChild(el('div', { class: 'hedge-msg' }, h.message));
  return row;
}

function renderHedgeBanner() {
  const host = $('hedge-banner');
  clear(host);
  const hedges = (state.data && state.data.open_hedges) || [];
  if (!hedges.length) {
    host.hidden = true;
    return;
  }
  host.hidden = false;
  host.appendChild(
    el('div', { class: 'hedge-banner-head' }, hedges.length === 1 ? 'Open hedge' : `Open hedges (${hedges.length})`)
  );
  for (const h of hedges) host.appendChild(buildHedgeRow(h));
}

/** The same hedge card, scoped to the wheel on screen, under its Insights. */
function renderTradeLogHedge(entry) {
  const host = $('tradelog-hedge');
  clear(host);
  const all = (state.data && state.data.open_hedges) || [];
  const mine = all.filter((h) => h.cycle_id === entry.cycle_id);
  if (!mine.length) {
    host.hidden = true;
    return;
  }
  host.hidden = false;
  for (const h of mine) host.appendChild(buildHedgeRow(h, { showAccount: false }));
}

/* ------------------------------------------------------ net worth & benchmark */

function renderNetWorthTiles(netWorth, benchmark, wheelReturn, wheelState) {
  const host = $('networth-tiles');
  clear(host);

  // The combined view nests per-account totals under `.combined`; a single
  // account's payload already has these fields at the top level.
  const totals = netWorth.combined || netWorth;

  // "True capital deployed" is a cost-basis/collateral reconstruction from
  // the transaction history, never the broker's own mark-to-market Total
  // value -- the two answer different questions and are not expected to
  // match. `untracked_equity_value` (Dashboard._build_net_worth) is the one
  // quantifiable piece of that gap: Positions-snapshot shares this history
  // has no real lot for at all (bought before every export loaded begins),
  // which count fully in Total value but essentially not in Deployed. The
  // rest of the gap -- idle cash, unrealized gains marked to market only in
  // Total value, short options' mark-to-market vs. their collateral -- isn't
  // separable into its own number, so the foot line names it without a figure.
  const untracked = totals.untracked_equity_value || 0;
  const deployedFoot =
    untracked > 1
      ? `of ${money(totals.total_value)} total value; ${money(untracked)} untracked equity, rest is cash + unrealized gains`
      : `of ${money(totals.total_value)} total value, rest is cash + unrealized gains`;

  // Total value and Cash are read verbatim off the broker's Positions
  // snapshot -- nothing computed to show a formula for, but four similarly-
  // named dollar figures (this pair, plus True capital deployed here and the
  // portfolio's own Capital deployed tile above) need a foot line each so a
  // reader isn't left guessing which is which.
  const tiles = [
    {
      label: 'Total value',
      value: money(totals.total_value, { cents: true }),
      foot: 'Broker Positions snapshot, cash + every holding.',
    },
    {
      label: 'Cash',
      value: money(totals.cash_total, { cents: true }),
      foot: 'Uninvested cash; part of Total value.',
    },
    {
      label: 'True capital deployed',
      value: money(totals.wheel_capital_deployed),
      foot: deployedFoot,
      formula: formula([
        "Today's committed wheel capital =",
        `  ${CAPITAL_FORMULA_SUM}`,
        `= ${money(totals.wheel_capital_deployed)}`,
        '',
        "Dated to the broker's Positions export (the as-of date), which can",
        '  trail a few days behind the latest transaction on file.',
      ]),
    },
  ];

  // "Cash for new CSPs" (liquid cash minus collateral already securing open
  // puts) lives in its own card up near Covered-call candidates -- see
  // `renderCspCash` -- so "what we can do next" reads in one place. Not
  // repeated here.

  // Independent of the SPY replay below -- no Positions/Yahoo price data
  // needed, just the wheel's own transaction history -- so it renders
  // whenever it has enough of its own activity, whether or not the
  // whole-account benchmark comparison below is available.
  if (wheelReturn && wheelReturn.available) {
    const events = wheelReturn.cash_flow_events || [];
    const span = events.length ? `${events[0].date} → ${events[events.length - 1].date}` : '—';
    const wb = wheelReturn.benchmark;
    const hasBench = wb && wb.xirr_pct !== null && wb.xirr_pct !== undefined;

    if (hasBench) {
      // One tile, one comparison: the wheel's own dated flows measured two
      // ways -- as run, and replayed into SPY -- plus the dollar difference.
      const beat = (wheelReturn.value_added ?? 0) >= 0;
      tiles.push({
        label: 'Wheel vs. S&P 500 (XIRR)',
        value: `${pct(wheelReturn.xirr_pct, 0)} ${beat ? '›' : '‹'} ${pct(wb.xirr_pct, 0)}`,
        foot: `Wheel vs. the same dated flows in SPY; value added ${money(wheelReturn.value_added, { cents: true, sign: true })}.`,
        tone: beat ? 'pos' : 'neg',
        formula: formula([
          "The wheel's own cash flows, every option-leg open/close and every",
          '  wheel-active share buy/sell, on their real dates; measured two ways:',
          '',
          `Wheel-only return (XIRR)       ${pct(wheelReturn.xirr_pct, 1)}`,
          `  actual money-weighted return on those ${events.length} flows (${span}),`,
          `  ${money(wheelReturn.terminal_value)} still committed today.`,
          `Same flows replayed into SPY   ${pct(wb.xirr_pct, 1)}`,
          `  each flow bought/sold SPY at that day's price instead;`,
          `  SPY holding valued ${money(wb.terminal_value)} on ${wheelReturn.as_of}.`,
          `Value added                    ${money(wheelReturn.value_added, { cents: true, sign: true })}`,
          `  = ${money(wheelReturn.terminal_value)} - ${money(wb.terminal_value)} terminal value.`,
          '',
          'Same timing on both sides, so it isolates strategy from when money',
          '  moved. Not risk-adjusted; fast turnover inflates XIRR vs. buy-and-',
          '  hold. Spreads not netted; dividends excluded.',
        ]),
      });
    } else {
      tiles.push({
        label: 'Wheel-only return (XIRR)',
        value: pct(wheelReturn.xirr_pct, 1),
        foot: `${span} · ${money(wheelReturn.terminal_value)} still committed`,
        tone: (wheelReturn.xirr_pct ?? 0) >= 0 ? 'pos' : 'neg',
        formula: formula([
          'XIRR (money-weighted annualized return): the single rate that makes',
          '  every dated cash flow; each option-leg open/close, each wheel-',
          '  active share purchase/sale, discount to zero against the',
          '  capital still committed today.',
          `${events.length} events, ${span} = ${pct(wheelReturn.xirr_pct, 1)}`,
          '',
          'Not risk-adjusted; fast turnover inflates this vs. buy-and-hold.',
          'Spreads not netted; dividends excluded.',
        ]),
      });
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
      label: 'Whole-account return (XIRR)',
      value: pct(benchmark.actual.xirr_pct, 1),
      foot: 'Entire account since the opening balance. Not a wheel figure.',
      tone: (benchmark.actual.xirr_pct ?? 0) >= 0 ? 'pos' : 'neg',
      formula: formula([
        'Money-weighted return (XIRR): the annualized rate r solving',
        '  Σ amount_i ÷ (1 + r)^((date_i - date_0) / 365) = 0',
        `  over ${events.length} cash-flow event(s), the opening balance`,
        '  plus every external deposit/withdrawal found in the transaction',
        `  history, ${span}; valued against the account's`,
        `  terminal value of ${money(benchmark.actual.terminal_value)} on ${benchmark.as_of}.`,
        '',
        'Covers the ENTIRE account, and starts at the opening balance in',
        '  data/accounts.json, which can pre-date the wheel. For the',
        '  wheel-vs-SPY question use the two tiles above instead.',
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

function renderNetWorth(netWorth, benchmark, wheelReturn, wheelState) {
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
  renderNetWorthTiles(netWorth, benchmark, wheelReturn, wheelState);

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
      el('div', { class: 'dataset-unsupported' }, `${name} (${dataset.size_kb} KB), ${dataset.reason}`)
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

function renderChips(hostId, values, selected, onToggle, labelFn) {
  const host = $(hostId);
  clear(host);
  for (const value of values) {
    const chip = el('button', { class: 'chip', type: 'button' }, labelFn ? labelFn(value) : value);
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
      sourceStatus(`${files.length} file(s) ready, press Load to evaluate: ${names}`);
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

  $('timeline-sort').addEventListener('click', (event) => {
    const toTime = event.currentTarget.getAttribute('aria-pressed') !== 'true';
    event.currentTarget.setAttribute('aria-pressed', toTime ? 'true' : 'false');
    event.currentTarget.textContent = toTime ? 'Sort by ticker' : 'Sort by time';
    state.timelineSort = toTime ? 'time' : 'ticker';
    if (state.data) drawTimeline(state.data.cycles, state.data.meta.through);
  });

  $('period-pl-granularity').addEventListener('click', (event) => {
    const toWeekly = event.currentTarget.getAttribute('aria-pressed') !== 'true';
    event.currentTarget.setAttribute('aria-pressed', toWeekly ? 'true' : 'false');
    event.currentTarget.textContent = toWeekly ? 'Monthly' : 'Weekly';
    state.periodPlGranularity = toWeekly ? 'week' : 'month';
    if (state.data) drawPeriodPl(state.data.period_pl || {});
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
    .filter(
      (wheel) =>
        wheel.cycle_id === state.tradeLogCycleId || // the current selection is never filtered out
        ((!ticker || wheel.underlying === ticker) && tradeLogInWindow(wheel))
    )
    .sort(
      (a, b) => a.underlying.localeCompare(b.underlying) || a.cycle_id.localeCompare(b.cycle_id)
    );
}

/** Tickers with a wheel displayable under the date window (plus the selected
 *  wheel's ticker, so a click-through selection never drops its own option). */
function tradeLogTickers() {
  const wheels = (state.data && state.data.trade_log && state.data.trade_log.wheels) || [];
  const tickers = new Set(wheels.filter(tradeLogInWindow).map((wheel) => wheel.underlying));
  const selected = wheels.find((wheel) => wheel.cycle_id === state.tradeLogCycleId);
  if (selected) tickers.add(selected.underlying);
  return [...tickers].sort();
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
  // A new top-level view -- don't leave the reader parked wherever the old
  // (often much taller) page was scrolled.
  const tabs = document.querySelector('nav.tabs');
  if (tabs) tabs.scrollIntoView({ block: 'start' });
}

/**
 * Resolve a Dashboard cycle (possibly renumbered by a date filter) to its real
 * Trade Log wheel: same ticker + account, then the wheel whose date span
 * contains a probe date that is definitely inside the one the user aimed at --
 * a specific leg's open date if given, otherwise the cycle's last activity.
 */
function matchTradeLogWheel(cycle, atDate) {
  const wheels = (state.data && state.data.trade_log && state.data.trade_log.wheels) || [];
  const same = wheels.filter(
    (w) =>
      w.underlying === cycle.underlying && (cycle.account_id == null || w.account_id === cycle.account_id)
  );
  const probe = atDate || cycle.last_activity || cycle.end_date || cycle.start_date;
  const byStart = (a, b) => (a.start_date < b.start_date ? 1 : -1);
  return (
    same.find((w) => w.start_date <= probe && (w.end_date === null || probe <= w.end_date)) ||
    same
      .filter(
        (w) => w.start_date <= (cycle.end_date || '9999') && (w.end_date || '9999') >= cycle.start_date
      )
      .sort(byStart)[0] ||
    same.slice().sort(byStart)[0] ||
    null
  );
}

/** Click-through from the Dashboard's Wheel-timelines chart. */
function openTradeLog(cycle, atDate) {
  const match = matchTradeLogWheel(cycle, atDate);
  if (match) {
    state.tradeLogCycleId = match.cycle_id;
    state.tradeLogTicker = match.underlying; // scope the picker to this ticker
  }
  switchTab('tradelog');
}

/**
 * Click-through from a per-ticker Dashboard chart (Realized P/L by ticker,
 * Annualized Wheel ROC): open the Trade Log scoped to this ticker. The wheel
 * picker is left to `renderTradeLog`, which defaults to the ticker's most
 * recent wheel -- clearing `tradeLogCycleId` first is required because
 * `orderedTradeLog` pins the *current* selection even across a ticker change,
 * so a stale id from a previous click would otherwise stay selected.
 */
function openTradeLogForTicker(row) {
  state.tradeLogTicker = row.underlying;
  state.tradeLogCycleId = null;
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
 * Rule-based commentary: strengths (green check) and things to improve (amber
 * arrow), from an `{strengths, improvements}` object -- see `wheel/insights.py`.
 * Renders into `host`, hidden when there is nothing to say.
 */
function renderInsights(host, insights, heading) {
  clear(host);
  const data = insights || { strengths: [], improvements: [] };
  const lines = [
    ...(data.strengths || []).map((text) => ['good', '✓', text]),
    ...(data.improvements || []).map((text) => ['improve', '▸', text]),
  ];
  if (!lines.length) {
    host.hidden = true;
    return;
  }
  host.hidden = false;
  host.appendChild(el('div', { class: 'ti-head' }, heading));
  const list = el('ul', { class: 'ti-list' });
  for (const [cls, mark, text] of lines) {
    const li = el('li', { class: 'ti-' + cls });
    li.appendChild(el('span', { class: 'ti-mark' }, mark));
    li.appendChild(el('span', {}, text));
    list.appendChild(li);
  }
  host.appendChild(list);
}

function renderTradeLogInsights(entry) {
  renderInsights($('tradelog-insights'), entry.insights, 'Insights');
}

/** Book-level commentary on the Dashboard, from `data.insights`. */
function renderDashboardInsights() {
  renderInsights($('dashboard-insights'), state.data && state.data.insights, 'Portfolio insights');
}

/* -------------------------------------------------- open option positions
 *
 * One sortable row per open covered call / cash-secured put across every
 * wheel -- `data.open_positions`, built by `_build_open_positions` in
 * wheel/api.py. Filter-independent (an open contract needs watching whatever
 * date window is on screen), so it is not redrawn on filter changes beyond
 * the single render() pass. Symbols stay alphabetical and their rows stay
 * contiguous; a column click re-orders the rows *within* each symbol group.
 */
const OPEN_POS_COLUMNS = [
  { key: 'underlying', label: 'Symbol', left: true },
  { key: 'type', label: 'Type', left: true },
  { key: 'strike', label: 'Strike' },
  { key: 'expiration', label: 'Expiration' },
  { key: 'breakeven', label: 'Breakeven' },
  { key: 'wheel_breakeven', label: 'Wheel Breakeven' },
  { key: 'moneyness_pct', label: 'ITM/OTM (%)' },
  { key: 'last_close', label: 'Last Close' },
  { key: 'last_close_pct', label: 'Last Close %' },
  { key: 'signed_contracts', label: 'Qty' },
  { key: 'net_premium', label: 'Net Premium' },
  { key: 'cycle_id', label: 'Wheel', left: true },
  { key: 'annualized_yield_pct', label: 'Annualized Yield' },
];

function signedPct(value, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return (value > 0 ? '+' : '') + value.toFixed(digits) + '%';
}

const toneOf = (value) =>
  value === null || value === undefined || Number.isNaN(value) ? '' : value >= 0 ? 'pos' : 'neg';

const OP_TYPE_LABEL = {
  CSP: 'Cash-secured put (premium received)',
  CC: 'Covered call (premium received)',
  LP: 'Long put, protective hedge or directional (premium paid)',
  LC: 'Long call, protective hedge or directional (premium paid)',
};

function openPositionRow(row, isGroupStart, groupSize) {
  const isLong = row.side === 'LONG';
  const tr = el('tr', {
    class: 'op-row' + (isGroupStart ? ' op-group-start' : '') + (isLong ? ' op-long' : ''),
  });

  const symCell = el('td', { class: 'left ticker-cell' }, isGroupStart ? row.underlying : '');
  if (isGroupStart) {
    const bits = [row.name].filter(Boolean);
    if (groupSize > 1) bits.push(`${groupSize} open positions`);
    if (bits.length) symCell.title = bits.join(' · ');
  }
  tr.appendChild(symCell);

  const typeCell = el('td', { class: 'left' });
  typeCell.appendChild(
    el('span', { class: 'badge op-type op-type-' + row.type, title: OP_TYPE_LABEL[row.type] || row.type }, row.type)
  );
  tr.appendChild(typeCell);

  tr.appendChild(el('td', { class: 'num' }, money(row.strike, { cents: true })));

  // Expiration, with a warning glyph when the contract is inside a week.
  const nearExpiry =
    row.days_to_expiry !== null && row.days_to_expiry !== undefined && row.days_to_expiry < 8;
  const expCell = el('td', { class: 'num' + (nearExpiry ? ' op-near-expiry' : '') });
  if (row.expiration) {
    const dow = parseDay(row.expiration).toLocaleDateString('en-US', { weekday: 'short' });
    expCell.appendChild(document.createTextNode(`${row.expiration} · ${dow} · ${row.days_to_expiry}d`));
    if (nearExpiry) {
      expCell.appendChild(
        el(
          'span',
          { class: 'op-expiry-warn', title: `Near expiration: ${row.days_to_expiry} day(s) left` },
          ' ⚠'
        )
      );
    }
  } else {
    expCell.appendChild(document.createTextNode('—'));
  }
  tr.appendChild(expCell);

  // Both break-even cells carry ONE signal: is the entire wheel in profit or
  // underwater? That is the last close vs. the wheel break-even (for a
  // share-less CSP wheel, which has no wheel break-even, its own position
  // break-even stands in). No color without a reference and a price -- and a
  // standalone directional long (no wheel behind it) stays uncolored: its
  // break-even isn't a wheel-profit signal, and a long put/call flips which
  // side of it is "good."
  const wheelRef = row.wheel_breakeven === null || row.wheel_breakeven === undefined
    ? row.breakeven
    : row.wheel_breakeven;
  const standaloneLong = isLong && (row.wheel_breakeven === null || row.wheel_breakeven === undefined);
  const wheelUnderwater =
    standaloneLong ||
    wheelRef === null || wheelRef === undefined || row.last_close === null || row.last_close === undefined
      ? null
      : row.last_close < wheelRef;
  const breakEvenTone = wheelUnderwater === null ? '' : wheelUnderwater ? 'neg' : 'pos';
  const wheelStatus =
    wheelUnderwater === null
      ? ''
      : wheelUnderwater
        ? ', whole wheel underwater (last close below the wheel break-even)'
        : ', whole wheel in profit (last close above the wheel break-even)';

  const bePositionNote = {
    CSP: 'This put alone: strike - premium/share.',
    CC: 'This call alone: backing-share cost basis - premium/share.',
    LP: 'This long put alone: strike - cost/share (you profit below it).',
    LC: 'This long call alone: strike + cost/share (you profit above it).',
  };
  const beCell = el('td', { class: 'num ' + breakEvenTone }, money(row.breakeven, { cents: true }));
  beCell.title = (bePositionNote[row.type] || '') + wheelStatus;
  tr.appendChild(beCell);

  const wheelBeCell = el('td', { class: 'num ' + breakEvenTone }, money(row.wheel_breakeven, { cents: true }));
  wheelBeCell.title =
    'The whole wheel: raw cost of shares still held, less every dollar the cycle has banked ' +
    '(premium, realized P/L, dividends). A dash when the cycle holds no shares yet.' +
    wheelStatus;
  tr.appendChild(wheelBeCell);

  // OTM is favorable for a short (it expires worthless, you keep the premium);
  // ITM is favorable for a long (it has intrinsic value). Same number, opposite
  // "good" side -- so the tone is keyed to side, not to the raw sign.
  const moneynessFavorable =
    row.moneyness_pct === null || row.moneyness_pct === undefined
      ? null
      : (row.moneyness_pct >= 0) !== isLong;
  const moneyness =
    row.moneyness_pct === null || row.moneyness_pct === undefined
      ? el('td', { class: 'num' }, '—')
      : el(
          'td',
          { class: 'num ' + (moneynessFavorable ? 'pos' : 'neg') },
          `${row.in_the_money ? 'ITM' : 'OTM'} ${Math.abs(row.moneyness_pct).toFixed(2)}%`
        );
  moneyness.title = isLong
    ? 'Strike vs. last close. In-the-money (green) is where a long put/call has intrinsic value.'
    : 'Strike vs. last close. Positive = out-of-the-money cushion; negative = in-the-money (assignment risk).';
  tr.appendChild(moneyness);

  tr.appendChild(el('td', { class: 'num' }, money(row.last_close, { cents: true })));
  tr.appendChild(el('td', { class: 'num ' + toneOf(row.last_close_pct) }, signedPct(row.last_close_pct)));

  tr.appendChild(el('td', { class: 'num' }, row.signed_contracts));
  tr.appendChild(el('td', { class: 'num ' + toneOf(row.net_premium) }, money(row.net_premium, { cents: true, sign: true })));

  const wheelCell = el('td', { class: 'left op-wheel' });
  wheelCell.appendChild(
    row.cycle_id ? wheelLink(row.cycle_id, row.underlying) : document.createTextNode('—')
  );
  tr.appendChild(wheelCell);

  const yieldCell = el('td', { class: 'num ' + toneOf(row.annualized_yield_pct) }, pct(row.annualized_yield_pct));
  yieldCell.title =
    'Net premium ÷ (strike × 100 × contracts), scaled to a year over the contract\'s open→expiry span.';
  tr.appendChild(yieldCell);

  return tr;
}

function renderOpenPositions() {
  const table = $('open-positions-table');
  if (!table) return;
  const card = $('open-positions-card');
  const rows = (state.data && state.data.open_positions) || [];
  if (card) card.hidden = rows.length === 0;
  clear(table);
  if (!rows.length) return;

  const { key, dir } = state.openPosSort;

  const thead = el('thead');
  const headRow = el('tr');
  for (const column of OPEN_POS_COLUMNS) {
    const th = el('th', { class: `sortable${column.left ? ' left' : ''}` }, column.label);
    if (key === column.key) th.textContent = column.label + (dir === 1 ? ' ▲' : ' ▼');
    th.addEventListener('click', () => {
      if (state.openPosSort.key === column.key) state.openPosSort.dir *= -1;
      else state.openPosSort = { key: column.key, dir: column.key === 'underlying' ? 1 : -1 };
      renderOpenPositions();
    });
    headRow.appendChild(th);
  }
  thead.appendChild(headRow);
  table.appendChild(thead);

  // Sorting the Symbol column is a straight A-Z / Z-A of the groups, with each
  // symbol's rows in expiry order. Sorting any other column sorts the rows
  // inside every group by that column AND sorts the groups themselves by their
  // now-leading (first) row -- so a header click visibly reorders the whole
  // table, while a symbol's positions still sit together as one block.
  const rowKey = key === 'underlying' ? 'expiration' : key;
  const rowDir = key === 'underlying' ? 1 : dir;

  // Null / NaN always sinks to the bottom, whichever direction is active.
  const rowCmp = (a, b) => {
    const av = a[rowKey];
    const bv = b[rowKey];
    const aNil = av === null || av === undefined || Number.isNaN(av);
    const bNil = bv === null || bv === undefined || Number.isNaN(bv);
    if (aNil || bNil) return (aNil ? 1 : 0) - (bNil ? 1 : 0);
    const base = typeof av === 'string' ? av.localeCompare(bv) : av - bv;
    return base * rowDir;
  };

  const groups = new Map();
  for (const row of rows) {
    if (!groups.has(row.underlying)) groups.set(row.underlying, []);
    groups.get(row.underlying).push(row);
  }
  const ordered = [...groups.values()].map((group) => group.slice().sort(rowCmp));
  ordered.sort((a, b) => {
    if (key === 'underlying') return a[0].underlying.localeCompare(b[0].underlying) * dir;
    return rowCmp(a[0], b[0]) || a[0].underlying.localeCompare(b[0].underlying);
  });

  const tbody = el('tbody');
  for (const group of ordered) {
    group.forEach((row, index) => tbody.appendChild(openPositionRow(row, index === 0, group.length)));
  }
  table.appendChild(tbody);
}

/* -------------------------------------------- table: covered-call candidates */

const CC_CAND_COLUMNS = [
  { key: 'underlying', label: 'Symbol', left: true },
  { key: 'target_cc_strike', label: 'Target price for CC' },
  { key: 'contracts_available', label: 'Qty' },
  { key: 'breakeven', label: 'Breakeven' },
  { key: 'wheel_breakeven', label: 'Wheel Breakeven' },
  { key: 'last_close', label: 'Last Close' },
  { key: 'cost_basis_per_share', label: 'Avg Cost Basis' },
  { key: 'unrealized_pl', label: 'Gain / Loss' },
  { key: 'shares_held', label: 'Shares' },
  { key: 'wheel', label: 'Current Wheel', left: true },
  { key: 'days_to_earnings', label: 'Earnings' },
  { key: 'sector', label: 'Sector', left: true },
];

/**
 * A ticker's wheel id rendered as a jump-to-Trade-Log control. Click or
 * Enter/Space switches to the Trade Log tab with that exact wheel selected.
 */
function wheelLink(cycleId, underlying) {
  const link = el(
    'span',
    { class: 'wheel-link', role: 'button', tabindex: '0', title: 'Open this wheel in the Trade Log' },
    cycleId
  );
  const go = () => {
    state.tradeLogCycleId = cycleId;
    state.tradeLogTicker = underlying || null;
    switchTab('tradelog');
  };
  link.addEventListener('click', go);
  link.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      go();
    }
  });
  return link;
}

const shares = (value) =>
  value === null || value === undefined || Number.isNaN(value)
    ? '—'
    : Number(value).toLocaleString('en-US', { maximumFractionDigits: 3 });

/**
 * Positions holding shares with no covered call written against them, from
 * `data.cc_candidates` (see `_build_cc_candidates` in wheel/api.py). Positions
 * with 100+ shares lead the table and carry a "Target price for CC" (the
 * greatest of cost basis, both break-evens and the last close, rounded up to
 * the next $0.50) and a negative "Qty" (the covered-call position that could
 * be opened). Sub-100 lots are listed after them for visibility, with those
 * two cells blank. Sortable.
 */
function renderCcCandidates() {
  const table = $('cc-candidates-table');
  if (!table) return;
  const card = $('cc-candidates-card');
  const rows = (state.data && state.data.cc_candidates) || [];
  if (card) card.hidden = rows.length === 0;
  clear(table);
  if (!rows.length) return;

  const { key, dir } = state.ccCandSort;

  const thead = el('thead');
  const headRow = el('tr');
  for (const column of CC_CAND_COLUMNS) {
    const th = el('th', { class: `sortable${column.left ? ' left' : ''}` }, column.label);
    if (key === column.key) th.textContent = column.label + (dir === 1 ? ' ▲' : ' ▼');
    th.addEventListener('click', () => {
      if (state.ccCandSort.key === column.key) state.ccCandSort.dir *= -1;
      else state.ccCandSort = { key: column.key, dir: column.key === 'underlying' ? 1 : -1 };
      renderCcCandidates();
    });
    headRow.appendChild(th);
  }
  thead.appendChild(headRow);
  table.appendChild(thead);

  const sorted = rows.slice().sort((a, b) => {
    // The Symbol column keeps actionable (100+ share) rows above the rest.
    if (key === 'underlying') {
      if (a.meets_threshold !== b.meets_threshold) return a.meets_threshold ? -1 : 1;
      return a.underlying.localeCompare(b.underlying) * dir;
    }
    const av = a[key];
    const bv = b[key];
    const aNil = av === null || av === undefined || Number.isNaN(av);
    const bNil = bv === null || bv === undefined || Number.isNaN(bv);
    if (aNil || bNil) return (aNil ? 1 : 0) - (bNil ? 1 : 0);
    const base = typeof av === 'string' ? av.localeCompare(bv) : av - bv;
    return base * dir || a.underlying.localeCompare(b.underlying);
  });

  const tbody = el('tbody');
  for (const row of sorted) {
    const { cell: earnCell } = earningsCell(row);
    const tr = el('tr', {
      class: 'op-row' + (row.meets_threshold ? '' : ' cc-below-100'),
    });
    const symCell = el('td', { class: 'left ticker-cell' }, row.underlying);
    if (row.name) symCell.title = row.name;
    tr.appendChild(symCell);

    const targetCell = el('td', { class: 'num cc-target' }, money(row.target_cc_strike, { cents: true }));
    targetCell.title = formula([
      'Target price for CC = greatest of:',
      `  cost basis      ${money(row.cost_basis_per_share, { cents: true })}`,
      `  breakeven       ${money(row.breakeven, { cents: true })}`,
      `  wheel breakeven ${money(row.wheel_breakeven, { cents: true })}`,
      `  last close      ${money(row.last_close, { cents: true })}`,
      '  ...then rounded up to the next $0.50',
      `= ${money(row.target_cc_strike, { cents: true })}`,
      '',
      'The lowest strike worth writing a call at: an assignment sells',
      'the shares for at least their cost (keeping every premium already',
      'collected) and never below the current market.',
    ]);
    tr.appendChild(targetCell);

    const qtyCell = el(
      'td',
      { class: 'num' },
      row.contracts_available === null || row.contracts_available === undefined
        ? '—'
        : String(row.contracts_available)
    );
    if (row.contracts_available) {
      const n = Math.abs(row.contracts_available);
      qtyCell.title = `${n} covered call${n === 1 ? '' : 's'} writable (${n * 100} of ${shares(row.shares_held)} shares)`;
    }
    tr.appendChild(qtyCell);

    tr.appendChild(el('td', { class: 'num' }, money(row.breakeven, { cents: true })));
    tr.appendChild(el('td', { class: 'num' }, money(row.wheel_breakeven, { cents: true })));
    tr.appendChild(el('td', { class: 'num' }, money(row.last_close, { cents: true })));
    tr.appendChild(el('td', { class: 'num' }, money(row.cost_basis_per_share, { cents: true })));

    // Total gain/loss on the shares vs. raw cost basis, marked to the last
    // close: dollars and percent in one cell, green up / red down.
    const gl = row.unrealized_pl;
    const glText =
      gl === null || gl === undefined
        ? '—'
        : money(gl, { cents: true, sign: true }) +
          (row.unrealized_pl_pct === null || row.unrealized_pl_pct === undefined
            ? ''
            : ` · ${signedPct(row.unrealized_pl_pct, 1)}`);
    const glCell = el('td', { class: 'num ' + toneOf(gl) }, glText);
    if (gl !== null && gl !== undefined) {
      glCell.title = formula([
        'Gain / Loss = Shares × (Last close − Cost basis)',
        `= ${shares(row.shares_held)} × (${money(row.last_close, { cents: true })} − ${money(row.cost_basis_per_share, { cents: true })})`,
        `= ${money(gl, { cents: true, sign: true })}`,
        'Against the raw average cost basis — premium already collected is not netted in.',
      ]);
    }
    tr.appendChild(glCell);

    // Whole-share count for the eye; the exact (sometimes fractional) figure
    // sits in the tooltip -- fractional-share buys and DRIP dust leave odd
    // remainders that don't matter for a 100-lot covered call.
    const rounded = Math.round(row.shares_held);
    const sharesCell = el('td', { class: 'num' }, rounded.toLocaleString('en-US'));
    if (Math.abs(row.shares_held - rounded) > 1e-6) {
      sharesCell.title = `${shares(row.shares_held)} exact`;
    }
    tr.appendChild(sharesCell);

    const wheelCell = el('td', { class: 'left op-wheel' });
    wheelCell.appendChild(row.wheel ? wheelLink(row.wheel, row.underlying) : document.createTextNode('—'));
    tr.appendChild(wheelCell);

    tr.appendChild(earnCell);
    tr.appendChild(el('td', { class: 'left' }, row.sector || '—'));

    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
}

/**
 * "Dry powder" for writing a new cash-secured put: liquid account cash only,
 * minus the collateral already securing open puts. Reads the broker Positions
 * snapshot's `cash_total` (pure cash -- never unrealized gains or equity
 * value) and nets out `wheel_state.buckets.puts.amount` (current CSP
 * collateral, a hold against that same cash, not a separate pot). Shown as a
 * card right under Covered-call candidates so "what we can do next" is one
 * glance: CC candidates + this.
 */
function renderCspCash() {
  const card = $('csp-cash-card');
  const host = $('csp-cash-tiles');
  if (!host) return;
  const data = state.data;
  const netWorth = data && data.net_worth;
  const totals = netWorth && netWorth.available ? netWorth.combined || netWorth : null;
  const cashTotal = totals && totals.cash_total !== null && totals.cash_total !== undefined ? totals.cash_total : null;
  const totalValue = totals && totals.total_value !== null && totals.total_value !== undefined ? totals.total_value : null;

  clear(host);
  if (cashTotal === null) {
    if (card) card.hidden = true;
    return;
  }
  if (card) card.hidden = false;

  const ws = data && data.wheel_state;
  const putCollateral = (ws && ws.buckets && ws.buckets.puts && ws.buckets.puts.amount) || 0;
  const available = Math.max(cashTotal - putCollateral, 0);
  const pct = totalValue ? (100 * available) / totalValue : null;

  const tiles = [
    {
      label: 'Cash for new CSPs',
      value: money(available, { cents: true }),
      primary: true,
      foot:
        putCollateral > 1e-9
          ? `${money(cashTotal)} liquid cash − ${money(putCollateral)} securing open puts`
          : `${money(cashTotal)} liquid cash, none reserved against open puts`,
      formula: formula([
        'Cash for new CSPs =',
        '  Liquid account cash − collateral already securing open cash-secured puts',
        '  (never includes unrealized gains, equity value, or shares held)',
        `= ${money(cashTotal, { cents: true })} − ${money(putCollateral, { cents: true })}`,
        `= ${money(available, { cents: true })}`,
      ]),
    },
    {
      label: 'Share of account',
      value: pct === null ? '—' : pct.toFixed(1) + '%',
      foot: totalValue ? `of ${money(totalValue)} total account value` : 'total account value unknown',
      formula:
        pct === null
          ? null
          : formula([
              'Share of account = Cash for new CSPs ÷ Total account value',
              `= ${money(available, { cents: true })} ÷ ${money(totalValue, { cents: true })}`,
              `= ${pct.toFixed(1)}%`,
            ]),
    },
  ];

  for (const tile of tiles) {
    const node = el('div', { class: 'tile' + (tile.primary ? ' primary' : '') });
    node.appendChild(el('div', { class: 'label' }, tile.label));
    node.appendChild(el('div', { class: 'value' }, tile.value));
    node.appendChild(el('div', { class: 'foot' }, tile.foot));
    setFormula(node, tile.formula);
    host.appendChild(node);
  }

  renderCspCandidates(available);
}

const CSP_CAND_COLUMNS = [
  { key: 'underlying', label: 'Symbol', left: true },
  { key: 'stars', label: 'Signal' },
  { key: 'contracts', label: 'Qty' },
  { key: 'cash_per_contract', label: 'Cash / Contract' },
  { key: 'avg_annualized_roc_pct', label: 'Avg Ann. ROC' },
  { key: 'monthly_premium_pct', label: 'Mo. Prem %' },
  { key: 'ppd', label: 'PPD' },
  { key: 'last_close', label: 'Last Close' },
  { key: 'wheels', label: 'Past Wheels' },
  { key: 'net_realized_pl', label: 'Realized P/L' },
  { key: 'days_to_earnings', label: 'Earnings' },
  { key: 'sector', label: 'Sector', left: true },
];

const EARNINGS_WARN_DAYS = 14;

/**
 * Re-grade a set of CSP-candidate rows on a curve so the whole 0-5 star range
 * gets used -- and graded against *these* rows only. The backend hands every
 * past profitable ticker with its absolute `star_breakdown.raw_stars`; the
 * dashboard then filters to the names the current free cash can actually sell
 * a contract on, and the curve runs over that shown list, not the whole book
 * (so a name that isn't even displayed can't anchor the low end).
 *
 * When the curve engages, it's a straight min/max stretch across the shown
 * set: the weakest name maps to 0 stars, the strongest to 5, everyone else
 * linearly between -- so the whole 0-5 range is always visible on the list.
 * It only engages with >= 3 rows and a real range to stretch; a shortlist
 * bunched within half a star (or fewer than three names) keeps plain absolute
 * rounding rather than blowing noise up into a full spread, and the pull ramps
 * in between 0.5 and 1.5 stars of range so a nearly-flat list isn't yanked to
 * the extremes. Mutates each row's `.stars`, and `.star_breakdown.stars` /
 * `.curve` for the tooltip. Idempotent (always reads `raw_stars`), so it's
 * safe to re-run every render.
 */
function spreadStars(rows) {
  if (!rows.length) return;
  const raw = (r) => {
    const bk = r.star_breakdown || {};
    const s = bk.raw_stars != null ? bk.raw_stars : r.stars;
    return Math.max(0, Math.min(5, s || 0));
  };
  const scores = rows.map(raw);
  const n = scores.length;
  const lo = Math.min(...scores);
  const hi = Math.max(...scores);
  const span = hi - lo;
  // 0 below half a star of range, full stretch by 1.5 -- linear between.
  const pull = n >= 3 ? Math.max(0, Math.min(1, (span - 0.5) / 1)) : 0;

  rows.forEach((r, i) => {
    const s = scores[i];
    let graded;
    if (pull <= 0) {
      graded = Math.round(s);
    } else {
      const stretched = ((s - lo) / span) * 5;
      graded = Math.round(Math.max(0, Math.min(5, pull * stretched + (1 - pull) * s)));
    }
    r.stars = graded;
    if (r.star_breakdown) {
      r.star_breakdown = { ...r.star_breakdown, stars: graded, curve: { n, applied: pull > 0 } };
    }
  });
}

/** A 0-5 star widget for the CSP recommender -- whole stars only, and only
 * the earned ones are drawn (no empty placeholders). */
function starWidget(stars) {
  const n = Math.max(0, Math.min(5, Math.round(stars || 0)));
  const wrap = el('span', { class: 'stars', 'aria-label': `${n} of 5 stars` });
  if (!n) return el('span', { class: 'stars none' }, '—');
  for (let i = 0; i < n; i += 1) wrap.appendChild(el('span', {}, '★'));
  return wrap;
}

/** The plain-text breakdown behind a Signal rating (see `csp_star_score`). */
function cspStarTooltip(bk) {
  if (!bk) return null;
  const v = bk.values || {};
  const c = bk.components || {};
  const bar = (score) => {
    const n = Math.max(0, Math.min(10, Math.round((score || 0) * 10)));
    return '█'.repeat(n) + '░'.repeat(10 - n);
  };
  const line = (label, valueText, keyName) => {
    const comp = c[keyName] || { score: 0, weight: 0 };
    return `  ${label.padEnd(15)}${String(valueText).padStart(9)}  ${bar(comp.score)}  ${comp.score.toFixed(2)} ·${Math.round(comp.weight * 100)}%`;
  };
  const mod = (x) => (x > 0 ? `+${x.toFixed(1)}` : x < 0 ? `−${Math.abs(x).toFixed(1)}` : '±0');
  const inline = (x) => (x > 0 ? ` + ${x.toFixed(1)}` : x < 0 ? ` − ${Math.abs(x).toFixed(1)}` : '');
  const em = bk.modifiers && bk.modifiers.earnings ? bk.modifiers.earnings : { stars: 0, note: '' };
  const sm = bk.modifiers && bk.modifiers.sector ? bk.modifiers.sector : { stars: 0, note: '' };
  const pct1 = (x) => (x === null || x === undefined ? '—' : x.toFixed(1) + '%');
  const curve = bk.curve || {};
  const rawLine =
    `  base ${bk.base_stars}${inline(em.stars)}${inline(sm.stars)} = ${bk.raw_stars} raw` +
    (curve.applied ? `, graded on a curve across ${curve.n} candidates` : '');
  return formula([
    `Signal ${bk.stars} / 5`,
    rawLine,
    '',
    'Past wheels',
    line('ROC', pct(v.roc_pct), 'roc'),
    line('Monthly prem', pct(v.monthly_premium_pct, 2), 'monthly_premium'),
    line('PPD yield', pct1(v.ppd_yield_pct), 'ppd_yield'),
    line('Realized P/L', money(v.net_realized_pl), 'profit'),
    line('Win rate', v.win_rate === null || v.win_rate === undefined ? '—' : Math.round(v.win_rate * 100) + '%', 'win_rate'),
    line('Consistency', (v.wheels || 0) + ' wh', 'consistency'),
    line('Recency', v.days_since_last_wheel === null || v.days_since_last_wheel === undefined ? '—' : v.days_since_last_wheel + 'd', 'recency'),
    '',
    'Market now',
    line('Volatility', v.vol_annual_pct === null || v.vol_annual_pct === undefined ? '—' : Math.round(v.vol_annual_pct) + '%/y', 'volatility'),
    line('Price pos.', v.price_position === null || v.price_position === undefined ? '—' : v.price_position.toFixed(2), 'price_position'),
    '',
    'Adjustments',
    `  ${mod(em.stars).padEnd(6)} ${em.note || ''}`,
    `  ${mod(sm.stars).padEnd(6)} ${sm.note || ''}`,
  ]);
}

/**
 * `<td>` for a "next earnings" date. When it's inside the warning window
 * (0..14 days out) the cell itself turns amber and gets a ` ⚠` glyph -- the
 * same treatment Expiration gets in Open option positions -- rather than
 * tinting the whole row. Shared by the CSP- and CC-candidate tables.
 */
function earningsCell(row) {
  const dte = row.days_to_earnings;
  const soon = dte !== null && dte !== undefined && dte >= 0 && dte <= EARNINGS_WARN_DAYS;
  const cell = el(
    'td',
    { class: 'num' + (soon ? ' earnings-soon' : '') },
    row.earnings_date ? longDate(row.earnings_date) : '—'
  );
  if (row.earnings_date) {
    cell.title =
      dte >= 0
        ? `${row.earnings_date} · ${dte}d away` +
          (soon ? ' — within 14 days, hold off on writing here' : '')
        : `${row.earnings_date} · reported ${-dte}d ago (earnings.json is stale)`;
    if (soon) {
      cell.appendChild(
        el('span', { class: 'earnings-warn-icon', title: `Earnings in ${dte} day(s)` }, ' ⚠')
      );
    }
  }
  return { cell, soon };
}

/**
 * Inside the Cash for CSPs card: tickers wheeled at a net profit before, each
 * priced at its last close and shown only when `available` cash could secure
 * at least one 100-share put at that price. "Qty" is how many such contracts
 * the cash could cover. "Signal" flags a ticker whose past wheels paid well
 * for the capital; a row whose earnings land within 14 days is amber. Data:
 * `data.csp_candidates` (`_build_csp_candidates`).
 */
function renderCspCandidates(available) {
  const table = $('csp-candidates-table');
  const hint = $('csp-candidates-hint');
  if (!table) return;
  clear(table);

  const base = (state.data && state.data.csp_candidates) || [];
  const rows = base
    .map((row) => ({
      ...row,
      contracts:
        row.last_close && row.last_close > 0 ? Math.floor(available / (row.last_close * 100)) : 0,
      cash_per_contract: row.last_close && row.last_close > 0 ? row.last_close * 100 : null,
    }))
    .filter((row) => row.contracts >= 1);

  // Grade the 0-5 curve against the names actually shown, not the whole book.
  spreadStars(rows);

  if (hint) hint.hidden = rows.length === 0;
  if (!rows.length) return;

  const { key, dir } = state.cspCandSort;

  const thead = el('thead');
  const headRow = el('tr');
  for (const column of CSP_CAND_COLUMNS) {
    const th = el('th', { class: `sortable${column.left ? ' left' : ''}` }, column.label);
    if (key === column.key) th.textContent = column.label + (dir === 1 ? ' ▲' : ' ▼');
    th.addEventListener('click', () => {
      if (state.cspCandSort.key === column.key) state.cspCandSort.dir *= -1;
      else state.cspCandSort = { key: column.key, dir: column.key === 'underlying' ? 1 : -1 };
      renderCspCash();
    });
    headRow.appendChild(th);
  }
  thead.appendChild(headRow);
  table.appendChild(thead);

  const sorted = rows.slice().sort((a, b) => {
    const av = a[key];
    const bv = b[key];
    const aNil = av === null || av === undefined || Number.isNaN(av);
    const bNil = bv === null || bv === undefined || Number.isNaN(bv);
    if (aNil || bNil) return (aNil ? 1 : 0) - (bNil ? 1 : 0);
    const cmp = typeof av === 'string' ? av.localeCompare(bv) : av - bv;
    return cmp * dir || a.underlying.localeCompare(b.underlying);
  });

  const tbody = el('tbody');
  for (const row of sorted) {
    const { cell: earnCell } = earningsCell(row);
    const tr = el('tr', { class: 'op-row' });

    const symCell = el('td', { class: 'left ticker-cell' }, row.underlying);
    if (row.name) symCell.title = row.name;
    const unvetted = (row.vetting && row.vetting.unvetted) || [];
    if (unvetted.length) {
      const flag = el('sup', { class: 'unvetted-flag' }, '?');
      flag.title = 'Not fully vetted:\n· ' + unvetted.join('\n· ');
      symCell.appendChild(flag);
    }
    tr.appendChild(symCell);

    const sigCell = el('td', { class: 'num csp-star' });
    sigCell.appendChild(starWidget(row.stars));
    setFormula(
      sigCell,
      cspStarTooltip(row.star_breakdown) ||
        `Signal ${row.stars === null || row.stars === undefined ? '—' : row.stars} / 5`
    );
    tr.appendChild(sigCell);

    const qtyCell = el('td', { class: 'num' }, String(row.contracts));
    qtyCell.title = formula([
      'Qty ≈ Cash for new CSPs ÷ (last close × 100)',
      `= ${money(available, { cents: true })} ÷ (${money(row.last_close, { cents: true })} × 100)`,
      `= ${row.contracts} contract${row.contracts === 1 ? '' : 's'}`,
      '',
      'A rough at-the-money sizing; a real strike / collateral differs.',
    ]);
    tr.appendChild(qtyCell);

    const cpcCell = el('td', { class: 'num' }, money(row.cash_per_contract));
    cpcCell.title = formula([
      'Cash / Contract = last close × 100',
      `= ${money(row.last_close, { cents: true })} × 100`,
      `= ${money(row.cash_per_contract, { cents: true })}`,
      '',
      'Collateral to secure one at-the-money put; a lower strike needs less.',
    ]);
    tr.appendChild(cpcCell);

    tr.appendChild(
      el('td', { class: 'num ' + toneOf(row.avg_annualized_roc_pct) }, pct(row.avg_annualized_roc_pct))
    );

    const moCell = el('td', { class: 'num' }, pct(row.monthly_premium_pct, 2));
    moCell.title = 'Gross premium ÷ avg collateral, per 30 days, over this ticker\'s past wheels.';
    tr.appendChild(moCell);

    const ppdCell = el('td', { class: 'num' }, row.ppd === null || row.ppd === undefined ? '—' : money(row.ppd, { cents: true }) + '/day');
    ppdCell.title = 'Blended profit-per-day: total option P/L ÷ total days active, past wheels.';
    tr.appendChild(ppdCell);

    tr.appendChild(el('td', { class: 'num' }, money(row.last_close, { cents: true })));

    tr.appendChild(el('td', { class: 'num' }, row.wheels));

    const plCell = el('td', { class: 'num ' + toneOf(row.net_realized_pl) }, money(row.net_realized_pl, { cents: true, sign: true }));
    plCell.title = 'Net realized P/L across every past wheel on this ticker.';
    tr.appendChild(plCell);

    tr.appendChild(earnCell);
    tr.appendChild(el('td', { class: 'left' }, row.sector || '—'));

    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
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

/** PPD-by-week for the one wheel on screen. Hidden for non-wheel cycles and
 *  wheels with no realized option P/L yet. */
function drawTradeLogPpd(entry) {
  const host = $('tradelog-ppd');
  const rows = (entry && entry.ppd_series) || [];
  if (!rows.length) {
    host.hidden = true;
    clear($('chart-tradelog-ppd'));
    return;
  }
  host.hidden = false;
  drawPpd(rows, {
    svgId: 'chart-tradelog-ppd',
    legendId: 'legend-tradelog-ppd',
    tableId: null,
  });
}

/**
 * Break-even after every transaction, in ledger order -- the same
 * `running_break_even` column from the table above, drawn as a line so the
 * grind down (premium coming in) and the jumps (shares bought) read at a
 * glance. A dashed rule marks the current stock price; the last point is the
 * wheel's Break-even price. Hidden when the wheel never holds a whole share.
 */
function drawTradeLogBreakeven(entry) {
  const host = $('tradelog-breakeven');
  const svg = $('chart-tradelog-breakeven');
  const rows = (entry.transactions || []).filter(
    (r) => typeof r.running_break_even === 'number'
  );
  if (rows.length < 2) {
    host.hidden = true;
    clear(svg);
    return;
  }
  host.hidden = false;

  const be = rows.map((r) => r.running_break_even);
  const cur = typeof entry.current_price === 'number' ? entry.current_price : null;

  // A pathological opener (a token 1-share buy while big puts are sold) can
  // throw break-even to a wild negative for a row or two. Scale the y-axis to
  // the bulk of the series with a median/MAD clamp so those outliers sit at the
  // edge instead of flattening everything meaningful; the line is clipped to
  // the plot so it never spills, and off-scale dots are simply not drawn.
  const sorted = [...be].sort((a, b) => a - b);
  const med = sorted[sorted.length >> 1];
  const devs = sorted.map((v) => Math.abs(v - med)).sort((a, b) => a - b);
  const mad = devs[devs.length >> 1] || Math.abs(med) * 0.1 || 1;
  const within = be.filter((v) => Math.abs(v - med) <= 6 * mad);
  let lo = Math.min(...within, cur === null ? Infinity : cur);
  let hi = Math.max(...within, cur === null ? -Infinity : cur);
  if (!isFinite(lo) || !isFinite(hi)) {
    lo = Math.min(...be);
    hi = Math.max(...be);
  }
  const pad = (hi - lo) * 0.12 || 1;
  lo -= pad;
  hi += pad;

  const margin = { top: 14, right: 58, bottom: 30, left: 62 };
  const width = chartWidth(svg);
  const height = 260;
  const priceLabel = (v) => (v < 0 ? '-$' : '$') + Math.abs(v).toFixed(2);
  const { group, plotWidth, plotHeight, y } = frame(svg, {
    width,
    height,
    margin,
    yMin: lo,
    yMax: hi,
    yFormat: priceLabel,
  });

  // Clip the line to the plot box so a far-off-scale segment can't draw over
  // the axis labels.
  const clipId = 'tlbe-clip';
  const defs = svgEl('defs');
  const clip = svgEl('clipPath', { id: clipId });
  clip.appendChild(
    svgEl('rect', { x: margin.left, y: margin.top, width: plotWidth, height: plotHeight })
  );
  defs.appendChild(clip);
  svg.insertBefore(defs, svg.firstChild);

  const slot = plotWidth / rows.length;
  const centers = rows.map((_, i) => margin.left + slot * (i + 0.5));
  const lineColor = cssVar('--series-1');
  const clampY = (v) => y(Math.max(lo, Math.min(hi, v)));

  // Current stock price -- the gap between this and the line is what is left to
  // recover.
  if (cur !== null && cur >= lo && cur <= hi) {
    const cy = y(cur);
    group.appendChild(
      svgEl('line', {
        x1: margin.left,
        x2: margin.left + plotWidth,
        y1: cy,
        y2: cy,
        stroke: cssVar('--text-muted'),
        'stroke-width': 1,
        'stroke-dasharray': '4 3',
        'stroke-opacity': 0.7,
      })
    );
    group.appendChild(
      svgEl(
        'text',
        {
          x: margin.left + plotWidth + 4,
          y: cy + 3.5,
          'text-anchor': 'start',
          fill: 'var(--text-muted)',
          'font-weight': 600,
        },
        'now ' + priceLabel(cur)
      )
    );
  }

  group.appendChild(
    svgEl('path', {
      d: 'M' + centers.map((cx, i) => `${cx},${y(be[i])}`).join('L'),
      fill: 'none',
      stroke: lineColor,
      'stroke-width': 2.5,
      'stroke-linejoin': 'round',
      'stroke-linecap': 'round',
      'clip-path': `url(#${clipId})`,
    })
  );

  rows.forEach((r, i) => {
    const cx = centers[i];
    if (be[i] >= lo && be[i] <= hi) {
      group.appendChild(svgEl('circle', { cx, cy: y(be[i]), r: 2.2, fill: lineColor }));
    }
    const hit = svgEl('circle', { cx, cy: clampY(be[i]), r: 7, fill: 'transparent' });
    attachTip(
      hit,
      `${r.type} · ${r.date}`,
      [
        { label: 'Break-even after this fill', value: priceLabel(r.running_break_even) },
        { label: 'Cumulative cash flow', value: money(r.running_cash_flow, { cents: true }) },
      ],
      formula([
        'Break-even = minus Cumulative cash flow ÷ shares held',
        `= ${money(-r.running_cash_flow, { cents: true })} ÷ shares held`,
        `= ${priceLabel(r.running_break_even)}`,
      ])
    );
    group.appendChild(hit);
  });

  // Endpoint = the summary's Break-even price.
  const lastCx = centers[centers.length - 1];
  const lastVal = be[be.length - 1];
  const lastCy = clampY(lastVal);
  group.appendChild(
    svgEl('circle', {
      cx: lastCx,
      cy: lastCy,
      r: 4.5,
      fill: lineColor,
      stroke: cssVar('--surface-1'),
      'stroke-width': 2,
    })
  );
  group.appendChild(
    svgEl(
      'text',
      {
        x: Math.min(lastCx + 8, margin.left + plotWidth + margin.right - 4),
        y: lastCy - 8,
        'text-anchor': lastCx + 8 > margin.left + plotWidth ? 'end' : 'start',
        fill: 'var(--text-primary)',
        'font-weight': 700,
      },
      priceLabel(lastVal)
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
  const maxLabels = Math.max(2, Math.floor(plotWidth / 74));
  const step = Math.max(1, Math.ceil(rows.length / maxLabels));
  rows.forEach((r, i) => {
    if (i % step !== 0 && i !== rows.length - 1) return;
    group.appendChild(
      svgEl(
        'text',
        {
          class: 'tick-label',
          x: centers[i],
          y: margin.top + plotHeight + 16,
          'text-anchor': 'middle',
        },
        dayLabel(r.date)
      )
    );
  });

  svg.setAttribute(
    'aria-label',
    `Break-even after each of ${rows.length} transactions, ending at ${priceLabel(lastVal)}` +
      (cur !== null ? `, with the stock now at ${priceLabel(cur)}.` : '.')
  );
}

/**
 * Which half of the wheel this cycle is in right now: selling cash-secured
 * puts while flat (waiting for assignment or expiry), or writing covered
 * calls while holding the shares an assignment left behind (waiting to be
 * called away). Derived from fields the Trade Log entry already carries --
 * no separate payload needed. A non-wheel cycle (directional/buy-and-hold)
 * has no put/call phase to show; CLOSED and NO_ACTIVITY (dormant) wheels
 * have nothing open right now either, so none of the three spin.
 */
function wheelStageOf(entry) {
  if (!entry.is_wheel) {
    return {
      cls: 'nonwheel',
      spin: false,
      label: entry.kind === 'hold' ? 'Buy-and-hold' : 'Directional',
      sub: 'Not a wheel, no cash-secured put / covered call phase applies.',
    };
  }
  if (entry.status === 'CLOSED') {
    return { cls: 'closed', spin: false, label: 'Closed', sub: 'This wheel is done.' };
  }
  if (entry.status === 'NO_ACTIVITY') {
    return {
      cls: 'dormant',
      spin: false,
      label: 'Dormant',
      sub: 'Flat for now, a new put on this ticker resumes it.',
    };
  }
  if (entry.shares_held > 1e-9) {
    return {
      cls: 'cc',
      spin: true,
      label: 'Covered Call',
      sub: `Holding ${Math.round(entry.shares_held).toLocaleString('en-US')} sh, writing calls against them.`,
    };
  }
  return {
    cls: 'csp',
    spin: true,
    label: 'Cash-Secured Put',
    sub: 'Selling puts, waiting for assignment or expiry.',
  };
}

function renderTradeLogStage(entry) {
  const host = $('tradelog-stage');
  if (!entry) {
    host.hidden = true;
    return;
  }
  host.hidden = false;
  const stage = wheelStageOf(entry);
  host.className = 'wheel-stage ' + stage.cls + (stage.spin ? ' spin' : '');
  $('tradelog-stage-label').textContent = stage.label;
  $('tradelog-stage-sub').textContent = stage.sub;
  attachTip(
    host,
    'Wheel stage',
    [{ label: 'Phase', value: stage.label }],
    formula([
      'Cash-Secured Put: flat, a put is selling for premium',
      '  while waiting for assignment or expiry.',
      'Covered Call: shares are held, a call is selling',
      '  against them while waiting to be called away.',
      'Dormant: flat with nothing open, a new put resumes it.',
      'Closed: terminal, this wheel is done.',
    ])
  );
}

function renderTradeLogSummary(entry) {
  const host = $('tradelog-summary');
  clear(host);
  host.hidden = false;
  host.classList.toggle('closed', entry.status === 'CLOSED');
  host.classList.toggle('no-activity', entry.status === 'NO_ACTIVITY');

  const cents = (value) => money(value, { cents: true });
  const perShare = (value) => (value === null || value === undefined ? '—' : '$' + value.toFixed(2));

  // Wheel-return ratios are withheld for a non-wheel cycle -- show "n/a" with a
  // pointer to the Kind cell rather than a bare dash.
  const notWheel = entry.is_wheel === false;
  const kindLabel =
    entry.kind === 'hold'
      ? 'Buy-and-hold (non-wheel)'
      : entry.kind === 'directional'
      ? 'Directional (non-wheel)'
      : 'Wheel';
  const wheelHelp = formula([
    'Not a wheel (see "Kind" above), so the wheel-return ratios',
    'do not apply here. The P&L is still real.',
  ]);

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
    tradeLogCell('Status', statusLabel(entry.status), {
      help: formula([
        'ACTIVE: something is still open (a contract or shares).',
        'NO ACTIVITY: flat, but still the same calendar year as the latest trade in the book.',
        'A new put or call on this ticker would resume the wheel.',
        'CLOSED: terminal. The year has turned, or the stock was called away.',
      ]),
    })
  );
  host.appendChild(
    tradeLogCell('Kind', kindLabel, {
      help: formula(
        entry.kind === 'hold'
          ? [
              'Plain buy-and-hold: shares were bought but no option has ever',
              'been written against them; no cash-secured put, no covered call.',
              'Not a wheel. Its P&L counts toward every total, but the wheel-return',
              'ratios are withheld. Selling a covered call turns it into a wheel.',
            ]
          : entry.kind === 'directional'
          ? [
              'Directional: only long options were bought, no cash-secured put,',
              'no covered call, no shares. Not a wheel; its P&L counts but the',
              'wheel-return ratios are withheld (they would annualize a short-dated',
              'premium bet into nonsense).',
            ]
          : [
              'This cycle is running the wheel: it sold cash-secured puts and/or',
              'covered calls, and may have taken assignment of shares.',
            ]
      ),
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
      foot:
        entry.capital_committed_pct === null || entry.capital_committed_pct === undefined
          ? null
          : `${pct(entry.capital_committed_pct, 1)} of ${entry.capital_committed_pct_of}`,
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
      notWheel ? 'n/a' : entry.pl_per_day_held === null ? '—' : cents(entry.pl_per_day_held) + '/day',
      {
        foot:
          notWheel || !entry.closed_leg_count
            ? null
            : `${entry.closed_leg_count} closed legs over ${entry.total_days_held} days held`,
        help: notWheel
          ? wheelHelp
          : formula([
              'Total option P&L divided by total days a position was held.',
              `= ${cents(entry.closed_leg_pl)} ÷ ${entry.total_days_held} days`,
              `= ${entry.pl_per_day_held === null ? '—' : cents(entry.pl_per_day_held)}/day`,
              'Closed legs only. Each roll segment counts on its own.',
            ]),
      }
    )
  );
  host.appendChild(
    tradeLogCell('Win rate', notWheel ? 'n/a' : pct(entry.win_rate_pct), {
      foot: notWheel ? null : `${entry.wins} / ${entry.wins + entry.losses} closed legs`,
      help: notWheel
        ? wheelHelp
        : formula([
            'Winning legs ÷ (winning + losing) closed legs.',
            'Open legs and exact break-evens are excluded from the count.',
          ]),
    })
  );
  host.appendChild(
    tradeLogCell('Avg days in trade', days1(entry.avg_days_in_trade), {
      help: formula([
        'Mean calendar days a closed leg of this wheel was held.',
        `= ${entry.avg_days_in_trade === null ? 'N/A, no closed legs yet' : entry.avg_days_in_trade.toFixed(1) + ' days'}`,
      ]),
    })
  );
  host.appendChild(
    tradeLogCell('Annualized Wheel ROC', notWheel ? 'n/a' : pct(entry.annualized_wheel_roc_pct), {
      tone:
        notWheel || entry.annualized_wheel_roc_pct === null
          ? null
          : entry.annualized_wheel_roc_pct >= 0
          ? 'pos'
          : 'neg',
      help: notWheel
        ? wheelHelp
        : formula([
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
    'Break-even',
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
    'Break-even': formula([
      'Sell price that would zero the campaign here.',
      'Negative Cumulative cash flow ÷ shares held.',
      'Falls as premium and dividends come in.',
      'Dash while under one share is on the book.',
      'Last share-holding row = Break-even price above.',
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
  // Per-share price, formatted like the summary's Break-even price so the two
  // can be read against each other.
  const perShare = (value) =>
    value === null || value === undefined ? '—' : '$' + value.toFixed(2);
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
    if (row.is_open_long) {
      tr.classList.add('open-hedge');
      tr.title = 'Open long option with no matching close yet, a live protective/directional leg.';
    }
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
      perShare(row.running_break_even),
    ];
    cells.forEach((value, index) => {
      const td = el('td', { class: index === 0 ? 'left' : 'num' }, value);
      if (index === 0 && (row.type === 'Buy Shares' || row.type === 'Sell Shares' || row.type === 'Dividend')) {
        td.classList.add('shares-type'); // stock fills and dividends read blue, apart from the option rows
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
    // Terminal (CLOSED) wheels are tinted salmon in the list; dormant
    // (NO_ACTIVITY) wheels are tinted amber.
    const optClass = wheel.status === 'CLOSED' ? 'closed' : wheel.status === 'NO_ACTIVITY' ? 'no-activity' : '';
    pick.appendChild(el('option', { value: wheel.cycle_id, class: optClass }, label));
  }

  const empty = $('tradelog-empty');
  if (!wheels.length) {
    empty.hidden = false;
    empty.textContent = 'No wheels in this account yet.';
    $('tradelog-summary').hidden = true;
    $('tradelog-bridge').hidden = true;
    $('tradelog-ppd').hidden = true;
    $('tradelog-breakeven').hidden = true;
    $('tradelog-stage').hidden = true;
    $('tradelog-insights').hidden = true;
    $('tradelog-hedge').hidden = true;
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
    $('tradelog-ppd').hidden = true;
    $('tradelog-breakeven').hidden = true;
    $('tradelog-stage').hidden = true;
    $('tradelog-insights').hidden = true;
    $('tradelog-hedge').hidden = true;
    $('tradelog-note').hidden = true;
    clear($('tradelog-table'));
    return;
  }

  empty.hidden = true;
  renderTradeLogStage(entry);
  renderTradeLogInsights(entry);
  renderTradeLogHedge(entry);
  drawTradeLogBridge(entry);
  drawTradeLogPpd(entry);
  renderTradeLogSummary(entry);
  renderTradeLogTable(entry);
  drawTradeLogBreakeven(entry);
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
    period_pl,
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
  renderChips(
    'status-chips',
    meta.statuses,
    state.statuses,
    (value) => {
      if (state.statuses.has(value)) state.statuses.delete(value);
      else state.statuses.add(value);
      load();
    },
    statusLabel
  );

  renderNotices(meta, reconciliation);
  renderTiles(portfolio, reconciliation);
  renderHedgeBanner();
  renderDashboardInsights();
  renderOpenPositions();
  renderCcCandidates();
  renderCspCash();
  renderNetWorth(net_worth, benchmark, wheel_return, wheel_state);

  drawCapital(capital_series, net_worth);
  drawWheelState(wheel_state, net_worth);
  drawPnl(pnl_series);

  renderCashFlowTiles(cash_flow.trailing, cash_flow.months, pnl_series);
  drawCashFlow(cash_flow.months, cash_flow.trailing, pnl_series);
  drawPeriodPl(period_pl || {});
  drawCashFlowGap(cash_flow.weeks || [], pnl_series);
  drawPpd(data.ppd_series || []);

  const tickerNetPlFormula = (row) =>
    formula([
      'Net realized P/L = Premium collected (net) + Stock realized P/L',
      '',
      `= ${money(row.option_realized_pl, { cents: true })} + ${money(row.stock_realized_pl, { cents: true })}`,
      `= ${money(row.net_realized_pl, { cents: true })}`,
    ]);

  // Drop tickers that realized nothing in the window -- a dormant-but-funded
  // wheel, or one with only still-open legs. `ticker_summary` keeps them (the
  // capital/ROC charts want a funded-but-idle position visible), but here a
  // $0 bar is a full row and hit target carrying no information, and it pads
  // the middle of the sorted ranking. Same spirit as the ROC chart's own
  // `annualized_wheel_roc_pct !== null` filter below. Rounding to cents guards
  // against float dust in the raw backend sum. If every ticker is zero the
  // chart goes blank, which is honest for "nothing realized yet".
  const realizedTickers = tickers.filter((row) => Math.round(row.net_realized_pl * 100) !== 0);

  drawSignedBars('chart-ticker-pl', realizedTickers, {
    valueOf: (row) => row.net_realized_pl,
    format: (value) => compactMoney(value),
    legendId: 'legend-ticker-pl',
    legendNote: [

    ],
    onRowClick: openTradeLogForTicker,
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
      row.profit_per_day === null
        ? {
            text: '—',
            title: formula([
              'Profit Per Day (PPD) = wheel option P/L ÷ Days',
              '',
              'N/A; this ticker never sold a put or call, so it has no',
              '  wheel premium to spread over the days held (buy-and-hold).',
            ]),
          }
        : {
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
          'N/A; this ticker never sold a put or call (buy-and-hold), so it',
          '  has no wheel option return, or no wheel capital was committed.',
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
          'N/A; this ticker never sold a put or call (buy-and-hold), or no',
          '  initial collateral was committed.',
        ])
      : formula([
          'Denominator is total initial (day-one) collateral, summed across',
          '  every cycle, not a time-weighted average.',
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
          'N/A, no initial collateral committed.',
        ])
      : formula([
          'Everything this ticker has produced; option P/L, realized AND',
          '  unrealized stock P/L, dividends; against total day-one capital.',
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
    legendNote: [

    ],
    onRowClick: openTradeLogForTicker,
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
          'before this export begins, the shares are real and the call is',
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
