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
  const prefix = value < 0 ? '−$' : sign ? '+$' : '$';
  return value === 0 ? '$0' : prefix + text;
}

function compactMoney(value) {
  const abs = Math.abs(value);
  const unit = abs >= 1e6 ? [1e6, 'M'] : abs >= 1e3 ? [1e3, 'k'] : [1, ''];
  const scaled = value / unit[0];
  const digits = unit[0] === 1 ? 0 : Math.abs(scaled) < 10 ? 1 : 0;
  return (value < 0 ? '−$' : '$') + Math.abs(scaled).toFixed(digits) + unit[1];
}

function pct(value, digits = 1) {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return value.toFixed(digits) + '%';
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

/* ------------------------------------------------------------------ tooltip */

const tooltip = $('tooltip');

function showTooltip(event, title, rows) {
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
function attachTip(node, title, rows) {
  const show = (event) => showTooltip(event, title, rows);
  node.addEventListener('pointerenter', show);
  node.addEventListener('pointermove', moveTooltip);
  node.addEventListener('pointerleave', hideTooltip);
  node.setAttribute('tabindex', '0');
  node.addEventListener('focus', () => {
    const box = node.getBoundingClientRect();
    showTooltip({ clientX: box.left + box.width / 2, clientY: box.top }, title, rows);
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

/** Draw x-axis labels for a time scale without letting them collide. */
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
  const maxLabels = Math.max(2, Math.floor(plotWidth / 78));
  const step = Math.max(1, Math.ceil(dates.length / maxLabels));
  for (let i = 0; i < dates.length; i += step) {
    group.appendChild(
      svgEl(
        'text',
        { class: 'tick-label', x: x(dates[i]), y: yBase + 16, 'text-anchor': 'middle' },
        dayLabel(dates[i])
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
// never given a band: it peaks at 0.4% of committed capital, about one pixel, so
// a swatch for it would point at nothing findable.
const CAPITAL_EXCLUDED = { key: 'long', label: 'Long-option debit' };

const CAPITAL_TABLE_HEAD = [
  'Date',
  'Shares held',
  'Put collateral',
  'Short calls',
  'Long debit',
  'Total',
];

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

function drawCapital(points) {
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
  const last = points[points.length - 1];
  const margin = { top: 12, right: 64, bottom: 30, left: 62 };
  const width = chartWidth(svg);
  const height = 288;
  const yMax = share ? 100 : Math.max(...points.map((p) => p.total)) * 1.06 || 1;

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
      showTooltip(at, longDate(point.date), [
        ...CAPITAL_BANDS.map((band, i) => ({
          label: band.label,
          value: readout(band.key),
          color: colors[i],
        })),
        { label: CAPITAL_EXCLUDED.label, value: readout(CAPITAL_EXCLUDED.key) },
        { label: 'Total committed', value: money(point.total) },
      ]);
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

  // No swatch on this one, deliberately: it is not a band.
  legend.appendChild(
    el(
      'span',
      { class: 'legend-note' },
      `${CAPITAL_EXCLUDED.label} is counted in the total but never exceeds half a percent, ` +
        `so it is not banded — ${money(last[CAPITAL_EXCLUDED.key])} today. See the table.`
    )
  );

  buildTable(
    'capital-table',
    CAPITAL_TABLE_HEAD,
    points.map((point) => [
      point.date,
      money(point.stock),
      money(point.put),
      money(point.call),
      money(point.long),
      money(point.total),
    ])
  );
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
      showTooltip(at, longDate(point.date), [
        ...lines.map((line, i) => ({
          label: line.label,
          value: money(point[line.key], { cents: true }),
          color: colors[i],
        })),
        { label: 'Full wheel P/L that day', value: money(point.total_pl, { cents: true }) },
      ]);
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

/* --------------------------------------------- chart: horizontal bar (sign) */

/**
 * Ranked horizontal bars whose colour encodes sign via the diverging pair.
 * Used for both P/L and ROC; `format` renders the value label and tooltip.
 */
function drawSignedBars(svgId, rows, { valueOf, format, tipRows, tableId, tableHead, tableRow }) {
  const svg = $(svgId);
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
    attachTip(hit, row.underlying, tipRows(row));
    group.appendChild(hit);
  });

  if (tableId) {
    buildTable(tableId, tableHead, sorted.map(tableRow));
  }
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

  laid.forEach((row, index) => {
    const top = rowTops[index];
    const rowHeight = row.lanes * laneHeight;

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
        row.cycle.cycle_id
      )
    );

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
      ]);
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

function buildTable(containerId, head, rows) {
  const container = $(containerId);
  if (!container) return;
  clear(container);
  const table = el('table');
  const thead = el('thead');
  const headRow = el('tr');
  head.forEach((label, index) => headRow.appendChild(el('th', { class: index === 0 ? 'left' : '' }, label)));
  thead.appendChild(headRow);
  table.appendChild(thead);

  const tbody = el('tbody');
  for (const row of rows) {
    const tr = el('tr');
    row.forEach((cell, index) =>
      tr.appendChild(el('td', { class: index === 0 ? 'left' : 'num' }, cell))
    );
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  container.appendChild(table);
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
  { key: 'net_realized_pl', label: 'Net P/L' },
  { key: 'initial_collateral', label: 'Initial cap' },
  { key: 'avg_collateral', label: 'Avg cap' },
  { key: 'roi_pct', label: 'ROI' },
  { key: 'annualized_roc_premium_pct', label: 'ROC (premium)' },
  { key: 'annualized_roc_pct', label: 'ROC (full wheel)' },
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
    statusCell.appendChild(el('span', { class: 'badge ' + cycle.status }, cycle.status));
    tr.appendChild(statusCell);

    tr.appendChild(el('td', { class: 'left' }, cycle.start_date));
    tr.appendChild(el('td', { class: 'left' }, cycle.end_date || '—'));
    tr.appendChild(el('td', { class: 'num' }, cycle.days_active));
    tr.appendChild(el('td', { class: 'num' }, cycle.legs_total));
    tr.appendChild(el('td', { class: 'num' }, cycle.rolls));
    tr.appendChild(el('td', { class: 'num' }, cycle.assignments));
    tr.appendChild(el('td', { class: 'num' }, money(cycle.option_realized_pl, { cents: true })));

    const pl = el('td', { class: 'num ' + (cycle.net_realized_pl >= 0 ? 'pos' : 'neg') },
      money(cycle.net_realized_pl, { cents: true }));
    tr.appendChild(pl);

    tr.appendChild(el('td', { class: 'num' }, money(cycle.initial_collateral)));
    tr.appendChild(el('td', { class: 'num' }, money(cycle.avg_collateral)));
    tr.appendChild(el('td', { class: 'num' }, pct(cycle.roi_pct, 2)));
    tr.appendChild(el('td', { class: 'num' }, pct(cycle.annualized_roc_premium_pct)));
    tr.appendChild(el('td', { class: 'num' }, pct(cycle.annualized_roc_pct)));
    tr.appendChild(el('td', { class: 'num' },
      cycle.avg_days_in_trade === null ? '—' : cycle.avg_days_in_trade.toFixed(1)));

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
      money(leg.collateral),
      money(leg.realized_pl, { cents: true }),
    ])
  );

  if (cycle.roll_events.length) {
    inner.appendChild(el('h3', {}, `Rolls (${cycle.roll_events.length})`));
    const rollHost = el('div');
    inner.appendChild(rollHost);
    buildTableInto(
      rollHost,
      ['Date', 'Right', 'Direction', 'Closed', 'Opened', 'Net credit'],
      cycle.roll_events.map((roll) => [
        roll.date,
        roll.right === 'P' ? 'PUT' : 'CALL',
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
    const host = el('div');
    inner.appendChild(host);
    buildTableInto(
      host,
      ['Date', 'Symbol', 'Direction', 'Shares', 'Strike', 'Implied cash', 'Note'],
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
  head.forEach((label, index) => headRow.appendChild(el('th', { class: index === 0 ? 'left' : '' }, label)));
  thead.appendChild(headRow);
  table.appendChild(thead);
  const tbody = el('tbody');
  for (const row of rows) {
    const tr = el('tr');
    row.forEach((cell, index) => tr.appendChild(el('td', { class: index === 0 ? 'left' : 'num' }, cell)));
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  host.appendChild(table);
}

/* -------------------------------------------------------------- stat tiles */

function renderTiles(portfolio, reconciliation) {
  const host = $('tiles');
  clear(host);

  const tiles = [
    {
      label: 'Net realized P/L',
      value: money(portfolio.net_realized_pl, { cents: true }),
      foot: `${money(portfolio.option_realized_pl)} options · ${money(portfolio.stock_realized_pl)} stock`,
      tone: portfolio.net_realized_pl >= 0 ? 'pos' : 'neg',
    },
    {
      label: 'Premium collected (net)',
      value: money(portfolio.option_realized_pl, { cents: true }),
      foot: `${money(portfolio.premium_received)} gross · ${money(portfolio.premium_paid)} paid to close · ${money(portfolio.open_premium)} still open`,
      tone: portfolio.option_realized_pl >= 0 ? 'pos' : 'neg',
    },
    {
      label: 'Annualized ROC — premium only',
      value: pct(portfolio.annualized_roc_premium_pct),
      foot: `${pct(portfolio.roi_on_avg_premium_pct)} over ${portfolio.days_span} days, excludes stock P/L`,
      tone: (portfolio.annualized_roc_premium_pct ?? 0) >= 0 ? 'pos' : 'neg',
    },
    {
      label: 'Annualized ROC — full wheel',
      value: pct(portfolio.annualized_roc_pct),
      foot: `${pct(portfolio.roi_on_avg_pct)} over ${portfolio.days_span} days, incl. stock P/L`,
      tone: (portfolio.annualized_roc_pct ?? 0) >= 0 ? 'pos' : 'neg',
    },
    {
      label: 'Capital deployed',
      value: money(portfolio.capital_deployed_now),
      foot: `${money(portfolio.avg_capital)} avg · ${money(portfolio.peak_capital)} peak`,
    },
    {
      label: 'Win rate',
      value: pct(portfolio.win_rate_pct, 0),
      foot: `${portfolio.wins}W / ${portfolio.losses}L of ${portfolio.total_legs} legs`,
    },
    {
      label: 'Avg days in trade',
      value: portfolio.avg_days_in_trade === null ? '—' : portfolio.avg_days_in_trade.toFixed(1),
      foot: `${portfolio.rolls} rolls · ${portfolio.assignments} assignments`,
    },
    {
      label: 'Cycles',
      value: String(portfolio.cycles),
      foot: `${portfolio.active_cycles} active · ${portfolio.tickers} tickers`,
    },
    {
      label: 'Cash reconciliation',
      value: reconciliation.balanced ? 'Balanced' : 'Mismatch',
      foot: `${reconciliation.rows_checked} rows · delta ${reconciliation.delta}`,
      tone: reconciliation.balanced ? 'pos' : 'neg',
    },
  ];

  for (const tile of tiles) {
    const node = el('div', { class: 'tile' });
    node.appendChild(el('div', { class: 'label' }, tile.label));
    node.appendChild(el('div', { class: 'value ' + (tile.tone || '') }, tile.value));
    node.appendChild(el('div', { class: 'foot' }, tile.foot));
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
      `${reconciliation.rows_checked} priced rows re-derived from price × quantity − fees; ` +
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

  if (!listing.datasets.length) {
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

    renderDatasets(result);
    fileInput.value = '';
    // A new account has different tickers and dates, so start from a clean slice.
    state.tickers.clear();
    state.statuses.clear();
    state.start = null;
    state.end = null;
    state.expanded.clear();
    $('preset').value = 'all';
    $('start').value = '';
    $('end').value = '';

    await load();
    sourceStatus(result.message || 'Loaded.', 'ok');
  } catch (error) {
    sourceStatus('Load failed: ' + error.message, 'err');
  } finally {
    button.disabled = false;
  }
}

/* ------------------------------------------------------------------ filters */

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
    state.tickers.clear();
    state.statuses.clear();
    state.start = null;
    state.end = null;
    state.expanded.clear();
    $('preset').value = 'all';
    $('start').value = '';
    $('end').value = '';
    load();
  });

  // Re-expresses one chart, so it redraws that chart rather than refetching.
  $('capital-mode').addEventListener('click', (event) => {
    const on = event.currentTarget.getAttribute('aria-pressed') === 'true';
    event.currentTarget.setAttribute('aria-pressed', on ? 'false' : 'true');
    state.capitalMode = on ? 'value' : 'share';
    if (state.data) drawCapital(state.data.capital_series);
  });

  document.querySelectorAll('.toggle[data-twin]').forEach((button) => {
    button.addEventListener('click', () => {
      const twin = $(button.dataset.twin);
      const showing = button.getAttribute('aria-pressed') === 'true';
      button.setAttribute('aria-pressed', showing ? 'false' : 'true');
      twin.hidden = showing;
    });
  });

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

async function load() {
  const params = new URLSearchParams();
  if (state.tickers.size) params.set('tickers', [...state.tickers].join(','));
  if (state.statuses.size) params.set('status', [...state.statuses].join(','));
  if (state.start) params.set('start', state.start);
  if (state.end) params.set('end', state.end);

  // Hold the previous render at reduced opacity -- no skeleton, no layout jump.
  document.querySelector('.wrap').classList.add('loading');
  try {
    const response = await fetch('/api/dashboard?' + params.toString());
    if (!response.ok) throw new Error('HTTP ' + response.status);
    state.data = await response.json();
    render();
  } catch (error) {
    const host = $('notices');
    clear(host);
    const notice = el('div', { class: 'notice' });
    notice.appendChild(el('strong', {}, 'Could not load data'));
    notice.appendChild(document.createTextNode(' ' + error.message));
    host.appendChild(notice);
  } finally {
    document.querySelector('.wrap').classList.remove('loading');
  }
}

function render() {
  const data = state.data;
  if (!data) return;
  const { meta, portfolio, cycles, tickers, capital_series, pnl_series, reconciliation } = data;

  // With several exports loaded the filenames are long and already listed in the
  // notice below, so the subtitle summarises rather than enumerating them.
  const label = meta.combined
    ? `${meta.sources.length} exports combined · ${meta.duplicates_removed} duplicate rows merged`
    : meta.source;
  $('subtitle').textContent =
    `${label} · ${meta.transactions_in_slice} of ${meta.transactions_total} transactions · ` +
    `${meta.data_first_date} → ${meta.data_last_date} · generated ${meta.generated_at.replace('T', ' ')}`;
  $('subtitle').title = meta.source;

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

  drawCapital(capital_series);
  drawPnl(pnl_series);

  drawSignedBars('chart-ticker-pl', tickers, {
    valueOf: (row) => row.net_realized_pl,
    format: (value) => compactMoney(value),
    tipRows: (row) => [
      { label: 'Net realized P/L', value: money(row.net_realized_pl, { cents: true }) },
      { label: 'Premium collected (net)', value: money(row.option_realized_pl, { cents: true }) },
      { label: 'Premium collected (gross)', value: money(row.premium_received) },
      { label: 'Paid to close', value: money(row.premium_paid) },
      { label: 'Stock P/L', value: money(row.stock_realized_pl) },
      { label: 'Cycles', value: `${row.cycles} (${row.active} active)` },
      { label: 'Rolls / assignments', value: `${row.rolls} / ${row.assignments}` },
    ],
    tableId: 'ticker-table',
    tableHead: ['Ticker', 'Cycles', 'Premium', 'Paid to close', 'Option P/L', 'Stock P/L', 'Net P/L', 'Rolls', 'Assign'],
    tableRow: (row) => [
      row.underlying,
      row.cycles,
      money(row.premium_received),
      money(row.premium_paid),
      money(row.option_realized_pl, { cents: true }),
      money(row.stock_realized_pl),
      money(row.net_realized_pl, { cents: true }),
      row.rolls,
      row.assignments,
    ],
  });

  drawSignedBars('chart-roc', tickers.filter((row) => row.annualized_roc_pct !== null), {
    valueOf: (row) => row.annualized_roc_pct,
    format: (value) => pct(value, 0),
    tipRows: (row) => [
      { label: 'Annualized ROC — full wheel', value: pct(row.annualized_roc_pct) },
      { label: 'Annualized ROC — premium only', value: pct(row.annualized_roc_premium_pct) },
      { label: 'Return on avg capital', value: pct(row.roi_on_avg_pct, 2) },
      { label: 'Avg capital', value: money(row.avg_capital) },
      { label: 'Peak capital', value: money(row.peak_capital) },
      { label: 'Net realized P/L', value: money(row.net_realized_pl, { cents: true }) },
      ...(row.capital_estimated
        ? [{ label: 'Note', value: 'includes strike-based proxy' }]
        : []),
    ],
    tableId: 'roc-table',
    tableHead: [
      'Ticker',
      'Avg capital',
      'Peak capital',
      'Capital now',
      'Net P/L',
      'Return on avg',
      'Annual ROC (full wheel)',
      'Annual ROC (premium only)',
      'Proxy',
    ],
    tableRow: (row) => [
      row.underlying,
      money(row.avg_capital),
      money(row.peak_capital),
      money(row.capital_now),
      money(row.net_realized_pl, { cents: true }),
      pct(row.roi_on_avg_pct, 2),
      pct(row.annualized_roc_pct),
      pct(row.annualized_roc_premium_pct),
      row.capital_estimated ? 'yes' : '—',
    ],
  });

  drawTimeline(cycles, meta.through);
  renderCycles(cycles);
}

wireFilters();
refreshDatasets();
load();
