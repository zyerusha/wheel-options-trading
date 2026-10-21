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

// Every field below this line that isn't marked "view-only, never saved" is
// part of the reader's saved settings -- see the "persisted settings"
// section (buildConfigSnapshot/applyConfigSnapshot) below, which reads and
// writes these same paths against data/config.json. The literal values here
// are just the fallback for a first run with nothing saved yet; they're
// overwritten by applyConfigSnapshot() before the first render, so a
// reload always resumes wherever the reader left off instead of flashing
// these defaults first.
const state = {
  data: null,
  tickers: new Set(),
  start: null,
  end: null,
  // view-only: which cards/rows are expanded, never saved (nothing to resume
  // -- collapsed is the natural state right after data loads).
  expanded: new Set(),
  // Per-chart wheel-status filter ('' = all, else 'ACTIVE'/'NO_ACTIVITY'/
  // 'CLOSED'), local to just that one chart -- Wheel timelines, Wheel
  // sequences, and the Ticker efficiency scatter. Every other chart/tile/
  // table always shows every status; see applyTimelineFilter() /
  // applySequencesFilter() / applyScatterFilter(). Deliberately never saved
  // to data/config.json (not part of buildConfigSnapshot()/
  // applyConfigSnapshot()) -- these three reset to "all" on every page
  // load, unlike the rest of the reader's remembered settings.
  timelineStatus: '',
  sequencesStatus: '',
  scatterStatus: '',
  cycleSort: { key: 'net_realized_pl', dir: -1 },
  // Open option positions table: within each symbol group (symbols stay
  // alphabetical), which column orders the rows and in which direction.
  openPosSort: { key: 'expiration', dir: 1 },
  // Covered-call candidates table: which column sorts it, and which direction.
  ccCandSort: { key: 'underlying', dir: 1 },
  // CSP-candidates table (inside the Cash for CSPs card): sort column + dir.
  cspCandSort: { key: 'stars', dir: -1 },
  // Wheel price targets table (Dashboard): which column sorts it, and direction.
  wheelTargetsSort: { key: 'gap_pct', dir: 1 },
  // How the capital chart expresses its bands: 'value' (dollars) or 'share' (%
  // of the day's total). A view of one chart, not a filter -- it changes no data.
  capitalMode: 'value',
  // Row order for the Wheel timelines chart: 'time' (by start date, newest wheel
  // on top -- the default, the account's recent history first) or 'ticker' (A-Z,
  // a ticker's cycles together, best for lookup). View-only, changes no data.
  timelineSort: 'time',
  // Bucket size for the Periodic P/L chart: 'month' or 'week'. Both are
  // already in the payload (data.period_pl.months / .weeks), so switching is
  // a redraw from already-fetched data, not a refetch.
  periodPlGranularity: 'month',
  // Expiration-calendar bucket size: 'day' (default), 'week', or 'month'. All
  // three are in the payload (data.expiration_calendar.days/.weeks/.months), so
  // switching is a redraw, not a refetch.
  expCalGrain: 'day',
  // View window for the Expiration calendar's time axis -- [startMs, endMs]
  // or null for the default per-grain window computed in
  // renderExpirationCalendarBody(). Set by the From/To date inputs or by
  // scroll-zoom (handleExpCalWheel); cleared only by Reset. Survives a grain
  // switch on purpose -- which dates to look at and how finely to bucket them
  // are independent choices. View-only, never saved.
  expCalZoom: null,
  // Date-range preset the filter row's #preset select is showing -- kept
  // alongside start/end (below) since "custom" carries no formula of its own
  // to recompute them from; see applyPreset()/the config-apply boot sequence.
  datePreset: 'all',
  // Which data/<account>/ folder is active, or COMBINED_ACCOUNT_ID for every
  // account aggregated without merging their cycles. Affects every chart and
  // table on the page, not just Net worth & benchmark.
  account: COMBINED_ACCOUNT_ID,
  // Top-level view: 'dashboard' (every analytics card) or 'tradelog' (one
  // wheel's transaction ledger). `tradeLogCycleId` is which wheel it shows;
  // `tradeLogTicker` (null = all) narrows the wheel list to one ticker.
  // Neither is saved -- both name a specific wheel/cycle in the currently
  // loaded data, which a future session's data may not even contain.
  activeTab: 'dashboard',
  tradeLogCycleId: null,
  tradeLogTicker: null,
  // Directional (LC/LP) Trade Log card: per-position trade-management
  // settings the reader configures -- target DTE for a roll/exit review,
  // an optional stop-loss (dollars or % of cost basis), and an optional
  // underlying-price exit trigger. Keyed by cycle_id, same id the rest of
  // the Trade Log already uses (and, for Combined, already account-
  // prefixed) -- see getManagementSettings()/setManagementSetting(). A
  // cycle with no entry here just uses MANAGEMENT_DEFAULTS.
  directionalManagement: {},
  // Tools tab: Capital projection card inputs.
  projection: {
    balanceSource: 'full_value',
    manualBalance: 100000,
    roiSource: 'wheel_roc',
    manualRoiPct: 8,
    taxRatePct: 34,
    years: 5,
    // Scroll-to-zoom view window for the chart, [lo, hi] or null for "fit the
    // data." View-only, never saved -- a reload should re-fit, not reopen
    // scrolled into whatever was last on screen.
    xZoom: null,
    yZoom: null,
  },
};

/* ----------------------------------------------------- persisted settings
 *
 * data/config.json remembers the reader's last settings across a browser
 * refresh -- everything `buildConfigSnapshot()` below reads off `state`
 * (plus the theme, which lives on the document root, not in `state`).
 * `loadConfig()` fetches it once at boot, `applyConfigSnapshot()` applies it
 * before the first render (see boot() at the very bottom of this file), and
 * `queueConfigSave()` -- a debounced POST back to /api/config, called from
 * every handler that changes one of these fields -- keeps it current. A
 * missing or unreadable file (a first run, or a hand-edit gone wrong) just
 * means "nothing saved yet": every field already has a plain literal
 * fallback sitting in `state` above, same as before this existed.
 *
 * Table column sorts (cycleSort/openPosSort/ccCandSort/cspCandSort/
 * wheelTargetsSort) and the Trade Log's own wheel/ticker selection are
 * deliberately left out -- the former for effort/value (many independent
 * sort controls for comparatively little payoff), the latter because it
 * names a specific wheel in the currently loaded data, which a future
 * session's data may not even contain.
 */

function buildConfigSnapshot() {
  return {
    theme: document.documentElement.dataset.theme || null,
    activeTab: state.activeTab,
    account: state.account,
    datePreset: state.datePreset,
    dateStart: state.start,
    dateEnd: state.end,
    tickers: [...state.tickers],
    capitalMode: state.capitalMode,
    timelineSort: state.timelineSort,
    periodPlGranularity: state.periodPlGranularity,
    expCalGrain: state.expCalGrain,
    directionalManagement: state.directionalManagement,
    projection: {
      balanceSource: state.projection.balanceSource,
      manualBalance: state.projection.manualBalance,
      roiSource: state.projection.roiSource,
      manualRoiPct: state.projection.manualRoiPct,
      taxRatePct: state.projection.taxRatePct,
      years: state.projection.years,
    },
  };
}

async function saveConfigNow() {
  try {
    await fetch('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(buildConfigSnapshot()),
    });
  } catch (error) {
    // Non-fatal: the next change (or the next debounced call) tries again --
    // losing one save to a flaky connection shouldn't surface as an error.
  }
}

// `debounce` is declared further down in this file as a plain `function`
// (hoisted), so it's already callable here despite the textual order.
const queueConfigSave = debounce(saveConfigNow, 500);

async function loadConfig() {
  try {
    const response = await fetch('/api/config');
    if (!response.ok) return {};
    const cfg = await response.json();
    return cfg && typeof cfg === 'object' ? cfg : {};
  } catch (error) {
    return {};
  }
}

/**
 * Applies a fetched config.json onto `state`/the theme. Called once at boot,
 * before the first render, so a reload resumes exactly where the reader left
 * off instead of flashing the plain defaults in `state` above first. Every
 * field is validated against the same set of values its own control
 * accepts -- this file is machine-written, not hand-edited, but a stale
 * field from an older version of the app (or a bad manual edit, since
 * nothing stops someone from opening it) must not carry over a nonsense
 * value; an invalid field is just skipped, leaving that one field's literal
 * default in place.
 */
function applyConfigSnapshot(cfg) {
  if (!cfg || typeof cfg !== 'object') return;

  if (cfg.theme === 'dark' || cfg.theme === 'light') document.documentElement.dataset.theme = cfg.theme;
  if (TAB_IDS.includes(cfg.activeTab)) state.activeTab = cfg.activeTab;
  // `account` can't be validated against the real account list yet (that
  // list itself is still an unfetched /api/accounts call away) -- it's
  // applied optimistically and renderAccountChips() falls back to Combined
  // the moment it turns out to be stale, the same way it already does for
  // any other now-unknown account id.
  if (typeof cfg.account === 'string' && cfg.account) state.account = cfg.account;
  if (typeof cfg.datePreset === 'string') state.datePreset = cfg.datePreset;
  if (typeof cfg.dateStart === 'string') state.start = cfg.dateStart;
  if (typeof cfg.dateEnd === 'string') state.end = cfg.dateEnd;
  if (Array.isArray(cfg.tickers)) state.tickers = new Set(cfg.tickers.filter((t) => typeof t === 'string'));
  // cfg.statuses (an older config.json may still carry one) is deliberately
  // never restored -- status filtering moved to the three per-chart
  // dropdowns, which always start at "all" on a fresh load.
  if (cfg.capitalMode === 'value' || cfg.capitalMode === 'share') state.capitalMode = cfg.capitalMode;
  if (cfg.timelineSort === 'time' || cfg.timelineSort === 'ticker') state.timelineSort = cfg.timelineSort;
  if (cfg.periodPlGranularity === 'month' || cfg.periodPlGranularity === 'week') {
    state.periodPlGranularity = cfg.periodPlGranularity;
  }
  if (EXP_CAL_GRAINS.includes(cfg.expCalGrain)) state.expCalGrain = cfg.expCalGrain;

  // Per-cycle directional trade-management settings. Each entry is
  // validated and clamped independently (same reasoning as every other
  // field here: this file is machine-written but not hand-edit-proof) --
  // a bad entry for one cycle_id is dropped rather than refusing the whole
  // map, same as a bad field elsewhere just keeps its literal default.
  if (cfg.directionalManagement && typeof cfg.directionalManagement === 'object') {
    for (const [cycleId, raw] of Object.entries(cfg.directionalManagement)) {
      const settings = sanitizeManagementSettings(raw);
      if (settings) state.directionalManagement[cycleId] = settings;
    }
  }

  const p = cfg.projection;
  if (p && typeof p === 'object') {
    if (['full_value', 'avg_capital', 'manual'].includes(p.balanceSource)) state.projection.balanceSource = p.balanceSource;
    if (Number.isFinite(p.manualBalance)) state.projection.manualBalance = Math.max(0, p.manualBalance);
    if (['wheel_roc', 'active_wheel_roc', 'manual'].includes(p.roiSource)) state.projection.roiSource = p.roiSource;
    if (Number.isFinite(p.manualRoiPct)) state.projection.manualRoiPct = p.manualRoiPct;
    if (Number.isFinite(p.taxRatePct)) state.projection.taxRatePct = Math.min(100, Math.max(0, p.taxRatePct));
    if (Number.isFinite(p.years)) state.projection.years = Math.min(40, Math.max(1, Math.round(p.years)));
  }
}

/**
 * Visual sync for the handful of controls whose displayed state lives only
 * in their own DOM attributes (aria-pressed/textContent), set nowhere else
 * during a normal render -- normally fine, since they start out matching
 * `state`'s own literal defaults, but a config-restored `state` needs them
 * explicitly caught up once, at boot, before the first render. Every other
 * restored field is already picked up naturally: the Capital projection
 * inputs by `renderCapitalProjection()`, the date range by `applyPreset()`,
 * tab visibility by `renderTabs()`, and so on.
 */
function syncViewControlsFromState() {
  const capBtn = $('capital-mode');
  if (capBtn) capBtn.setAttribute('aria-pressed', state.capitalMode === 'share' ? 'true' : 'false');

  const timelineBtn = $('timeline-sort');
  if (timelineBtn) {
    const pressed = state.timelineSort === 'time';
    timelineBtn.setAttribute('aria-pressed', pressed ? 'true' : 'false');
    timelineBtn.textContent = pressed ? 'Sort by ticker' : 'Sort by time';
  }

  const periodBtn = $('period-pl-granularity');
  if (periodBtn) {
    const pressed = state.periodPlGranularity === 'week';
    periodBtn.setAttribute('aria-pressed', pressed ? 'true' : 'false');
    periodBtn.textContent = pressed ? 'Monthly' : 'Weekly';
  }

  $('start').value = state.start || '';
  $('end').value = state.end || '';
  $('preset').value = state.datePreset || 'all';
}

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
 * title-attribute tip (used on tiles and table cells, opened with T in the
 * page tooltip, not the browser's -- see `adoptTitles`); the richer
 * hover/keyboard tooltip (`showTooltip`/`attachTip`, for chart marks) takes
 * the same kind of string as its `formula` argument and renders it in a
 * dedicated block below the row readout. Raw, directly-sourced values (a
 * strike, a contract count, a broker-reported price) get no formula: there is
 * nothing to explain.
 */
function formula(lines) {
  return lines.join('\n');
}

/** Hover tooltip carrying the calculation behind `node`'s value. */
function setFormula(node, text) {
  if (text) node.title = text;
}

/**
 * A ticker rendered as a real link to its TradingView chart, opened in a new
 * tab -- shared by every table's Symbol column (Open option positions, Wheel
 * price targets, CC/CSP candidates). Plain `<a>`, not a click handler, so the
 * usual new-tab gestures (middle-click, cmd/ctrl-click) work for free.
 */
function tickerLink(symbol) {
  return el(
    'a',
    {
      class: 'ticker-link',
      href: `https://www.tradingview.com/chart/?symbol=${encodeURIComponent(symbol)}`,
      target: '_blank',
      rel: 'noopener noreferrer',
      title: `Open ${symbol} on TradingView`,
    },
    symbol
  );
}

/**
 * The specific contract behind an Open option positions row (CSP/CC/LP/LC),
 * as TradingView's own compact options symbol: root + YYMMDD + C/P + strike,
 * e.g. "TLT250711C85.5". Loading that symbol takes TradingView straight to
 * this contract's own chart, expiry/strike/right already resolved -- the
 * options-chain equivalent of the plain ticker link above. `null` when the
 * row is missing any of the four pieces (a synthesized no-contract row).
 *
 * A whole-dollar strike needs its trailing ".0" spelled out -- confirmed
 * against real contracts: "LNC260918P42.5" resolved, but "IWM260918C297"
 * and "SPCX260918P139" (both integer strikes, otherwise identical shape)
 * came back "this symbol doesn't exist." Trimming the decimal for a clean
 * number apparently isn't how TradingView's own symbol index stores it.
 */
function tradingViewOptionSymbol(row) {
  if (!row.underlying || !row.expiration || !row.right || row.strike == null) return null;
  const [y, m, d] = row.expiration.split('-');
  const strike = Math.round(row.strike * 100) / 100;
  const strikeStr = Number.isInteger(strike) ? `${strike}.0` : String(strike);
  return `${row.underlying}${y.slice(2)}${m}${d}${row.right}${strikeStr}`;
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
    `${title} = CSP/CC P/L + Hedge P/L (protective puts, credit-spread legs)`,
    `= ${money(row.wheel_core_realized_pl, { cents: true })} + ${money(row.hedge_realized_pl, { cents: true })}`,
    `= ${money(row.option_realized_pl, { cents: true })}`,
    '  Gross, paid-to-close and open figures cover short legs only.',
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

/** The "nice" step (1/2/2.5/5/10 x a power of ten) that lands closest to
 * `span / count` ticks -- e.g. spans of $2.3M at count 5 picks $500k, not
 * some arbitrary $460k. Shared by `niceTicks` below and the Capital
 * projection chart's adaptive gridlines, which also want the step itself
 * (to derive a finer minor tier from it), not just the resulting tick list. */
function niceStep(span, count) {
  const raw = span / count;
  const magnitude = Math.pow(10, Math.floor(Math.log10(Math.abs(raw))));
  return [1, 2, 2.5, 5, 10].map((m) => m * magnitude).find((s) => s >= raw) || magnitude * 10;
}

function niceTicks(min, max, count = 5) {
  if (min === max) return [min];
  const step = niceStep(max - min, count);
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

/* One tooltip for the whole page, in two states:
 *
 * - Readout: a chart's hover readout, following the mouse and gone when it
 *   leaves the mark, so scanning a chart stays quick.
 * - Pinned: opened by pressing T over anything that has a tip (or over a
 *   chart readout, which freezes it), larger text, and it stays until T is
 *   pressed again.
 *   While pinned nothing else opens, moves or hides it.
 *
 * Plain text tips never pop up on hover at all: the browser's own title
 * tooltip cuts long text off with "...", so it is switched off everywhere
 * (see `adoptTitles`) and those tips only open with T. */
let tooltipPinned = false;
// Where the pointer is, so T knows what it is over and where to open.
let hoveredNode = null;
let lastPointer = { clientX: 0, clientY: 0 };

function renderTooltip(title, rows, formula) {
  clear(tooltip);
  tooltip.classList.remove('card');
  if (title) tooltip.appendChild(el('div', { class: 'tt-title' }, title));
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
    tooltip.appendChild(el('div', { class: rows.length || title ? 'tt-formula' : 'tt-text' }, formula));
  }
  tooltip.scrollTop = 0;
}

// `formula`, when given, is the actual equation behind the row(s) above --
// e.g. "Annualized Wheel ROC =\n  (...) \n= $300 / $10,000 × 365/30\n= 36.5%".
// Every calculated (non-raw) value shown anywhere in the dashboard carries one,
// per the calculation-transparency rule: a reader must be able to see the real
// formula and reproduce the number, not just a label describing it in words.
/** Show a chart readout (does nothing while a tooltip is pinned). */
function showTooltip(event, title, rows, formula) {
  if (tooltipPinned) return;
  renderTooltip(title, rows, formula);
  tooltip.classList.add('on');
  moveTooltip(event);
}

/** Position the tooltip next to `event`'s point, flipped to stay on screen. */
function placeTooltip(event) {
  const box = tooltip.getBoundingClientRect();
  let x = event.clientX + 14;
  let y = event.clientY + 14;
  if (x + box.width > window.innerWidth - 8) x = event.clientX - box.width - 14;
  if (y + box.height > window.innerHeight - 8) y = event.clientY - box.height - 14;
  tooltip.style.left = Math.max(8, x) + 'px';
  tooltip.style.top = Math.max(8, y) + 'px';
}

function moveTooltip(event) {
  if (!tooltipPinned) placeTooltip(event);
}

function hideTooltip() {
  if (!tooltipPinned) tooltip.classList.remove('on');
}

/** Pin what is under the pointer: the open chart readout, else its tip card
 * or text tip. */
function pinTooltip() {
  if (tooltipPinned) return;
  if (!tooltip.classList.contains('on')) {
    const tip = nearestTip(hoveredNode || document.activeElement);
    if (!tip) return;
    if (tip.card) {
      clear(tooltip);
      tooltip.appendChild(tipCardNode(tip.card));
      tooltip.classList.add('card');
    } else {
      renderTooltip('', [], tip.text);
    }
    tooltip.classList.add('on');
  }
  tooltipPinned = true;
  tooltip.classList.add('pinned');
  // Larger text changes the size, so re-fit it on screen.
  placeTooltip(lastPointer);
}

function dismissTooltip() {
  tooltipPinned = false;
  tooltip.classList.remove('on', 'pinned', 'card');
}

// Opening a details section makes the tooltip taller; keep it on screen
// (it scrolls inside itself once it reaches the window height).
tooltip.addEventListener(
  'toggle',
  () => {
    const box = tooltip.getBoundingClientRect();
    if (box.bottom > window.innerHeight - 8) {
      tooltip.style.top = Math.max(8, window.innerHeight - 8 - box.height) + 'px';
    }
  },
  true
);

const isTypingTarget = (node) =>
  !!node && (node.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(node.tagName));

// T toggles: opens the tip under the pointer, or closes the open one. Ignored
// while typing in a field and with Ctrl/Alt/Cmd, so it never steals a letter
// or a browser shortcut.
document.addEventListener('keydown', (event) => {
  const isT = event.key === 't' || event.key === 'T';
  if (!isT || event.repeat || event.ctrlKey || event.metaKey || event.altKey) return;
  if (isTypingTarget(event.target)) return;
  if (tooltipPinned) dismissTooltip();
  else pinTooltip();
});

document.addEventListener('pointerover', (event) => {
  hoveredNode = event.target;
});
document.addEventListener('pointermove', (event) => {
  lastPointer = { clientX: event.clientX, clientY: event.clientY };
});
document.addEventListener('pointerout', (event) => {
  if (!event.relatedTarget) hoveredNode = null; // left the window
});

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

/* Plain text tips (`title` attributes and SVG `<title>` children).
 *
 * Rather than rewrite the ~50 places that set `title`, one observer moves
 * every `title` into `data-tip` the moment it lands in the page -- including
 * on every re-render -- so the browser never has one to show. Observer
 * callbacks run before the next frame, well inside the browser's hover delay.
 * Writing an empty `title` clears the tip. */

function adoptTitleAttr(node) {
  if (!node.hasAttribute('title')) return;
  const text = node.getAttribute('title');
  node.removeAttribute('title');
  if (text) node.dataset.tip = text;
  else delete node.dataset.tip;
}

function adoptSvgTitle(titleNode) {
  const host = titleNode.parentNode;
  if (host && host.dataset && titleNode.textContent) host.dataset.tip = titleNode.textContent;
  titleNode.remove();
}

function adoptTitles(root) {
  if (root.nodeType !== Node.ELEMENT_NODE) return;
  if (root.hasAttribute('title')) adoptTitleAttr(root);
  root.querySelectorAll('[title]').forEach(adoptTitleAttr);
  // SVG only: the page's own <head><title> must stay.
  const svgTitles = root.namespaceURI === SVG_NS && root.localName === 'title' ? [root] : [];
  root.querySelectorAll('title').forEach((node) => {
    if (node.namespaceURI === SVG_NS) svgTitles.push(node);
  });
  svgTitles.forEach(adoptSvgTitle);
}

new MutationObserver((records) => {
  for (const record of records) {
    if (record.type === 'attributes') adoptTitleAttr(record.target);
    else record.addedNodes.forEach(adoptTitles);
  }
}).observe(document.documentElement, {
  subtree: true,
  childList: true,
  attributes: true,
  attributeFilter: ['title'],
});
adoptTitles(document.body);

/* Tip cards: the structured CSP TO ENTER / CC TO EXIT tooltips. The backend
 * builds each card already formatted (wheel/tooltipcard.py documents the
 * shape); `tipCardNode` is the one renderer for both, so they can't drift
 * apart, and it only lays out what it is given -- no numbers are computed
 * or reformatted here. */
const tipCards = new WeakMap();

function setTipCard(node, card) {
  if (card) tipCards.set(node, card);
}

/** The nearest tip at or above `start`: `{card}` or `{text}`, else null. */
function nearestTip(start) {
  for (let node = start; node && node !== document; node = node.parentNode) {
    if (node.nodeType !== Node.ELEMENT_NODE || node === tooltip) continue;
    if (tipCards.has(node)) return { card: tipCards.get(node) };
    if (node.dataset && node.dataset.tip) return { text: node.dataset.tip };
  }
  return null;
}

function tipCardItem(item, className) {
  const box = el('div', { class: className });
  box.appendChild(el('div', { class: 'tc-label' }, item.label));
  box.appendChild(el('strong', { class: 'tc-value' }, item.value));
  if (item.note) box.appendChild(el('div', { class: 'tc-muted' }, item.note));
  return box;
}

function tipCardTable(spec) {
  const wrap = el('div', { class: 'tc-table-wrap' });
  const table = el('table', { class: 'tc-table' });
  const head = el('tr');
  spec.columns.forEach((name, i) => head.appendChild(el('th', { class: spec.numeric[i] ? 'num' : '' }, name)));
  const thead = el('thead');
  thead.appendChild(head);
  table.appendChild(thead);
  const body = el('tbody');
  for (const row of spec.rows) {
    const tr = el('tr', { class: row.picked ? 'picked' : '' });
    row.cells.forEach((cell, i) => tr.appendChild(el('td', { class: spec.numeric[i] ? 'num' : '' }, cell)));
    body.appendChild(tr);
    // The reason gets its own full-width line so a long one never squeezes the figures.
    if (row.reason) {
      const why = el('tr', { class: 'tc-reason' + (row.picked ? ' picked' : '') });
      const cell = el('td', { colspan: spec.columns.length });
      // Optional bold lead-in (e.g. "Below resistance") so each line scans by its status.
      if (row.reason_label) {
        cell.appendChild(el('strong', {}, row.reason_label + ': '));
      }
      cell.appendChild(document.createTextNode(row.reason));
      why.appendChild(cell);
      body.appendChild(why);
    }
  }
  table.appendChild(body);
  wrap.appendChild(table);
  return wrap;
}

function tipCardSection(spec) {
  const details = el('details', { class: 'tc-details' });
  details.open = !!spec.open;
  details.appendChild(el('summary', {}, spec.title));
  const content = el('div', { class: 'tc-details-content' });
  if (spec.table) content.appendChild(tipCardTable(spec.table));
  if (spec.rows.length) {
    const list = el('dl', { class: 'tc-rows' });
    for (const [label, value] of spec.rows) {
      list.appendChild(el('dt', {}, label));
      list.appendChild(el('dd', {}, value));
    }
    content.appendChild(list);
  }
  for (const note of spec.notes) content.appendChild(el('p', { class: 'tc-muted' }, note));
  details.appendChild(content);
  return details;
}

function tipCardNode(card) {
  const root = el('div', { class: `tip-card tip-card--${card.status}` });

  const header = el('header', { class: 'tc-header' });
  const titles = el('div');
  titles.appendChild(el('div', { class: 'tc-eyebrow' }, card.strategy));
  if (card.ticker) titles.appendChild(el('div', { class: 'tc-ticker' }, card.ticker));
  titles.appendChild(el('div', { class: 'tc-muted' }, card.subtitle));
  if (card.quoted) titles.appendChild(el('div', { class: 'tc-muted' }, card.quoted));
  header.appendChild(titles);
  // The badge carries the status in words; its colour only reinforces it.
  header.appendChild(el('span', { class: 'tc-status' }, card.status_label));
  root.appendChild(header);

  if (card.headline) {
    const rec = el('section', { class: 'tc-recommendation' });
    rec.appendChild(tipCardItem(card.headline, 'tc-headline'));
    if (card.headline_side) rec.appendChild(tipCardItem(card.headline_side, 'tc-headline-side'));
    root.appendChild(rec);
  }
  if (card.summary) root.appendChild(el('p', { class: 'tc-summary' }, card.summary));

  if (card.metrics.length) {
    const grid = el('section', { class: 'tc-metrics' });
    for (const metric of card.metrics) grid.appendChild(tipCardItem(metric, 'tc-metric'));
    root.appendChild(grid);
  }

  if (card.why.length) {
    const why = el('section', { class: 'tc-section' });
    why.appendChild(el('h4', {}, 'Why this strike?'));
    const list = el('ul');
    for (const line of card.why) list.appendChild(el('li', {}, line));
    why.appendChild(list);
    root.appendChild(why);
  }

  // Historical context for the pick (CC TO EXIT's resistance): shown, never
  // used to choose -- the backend says so in its own notes.
  if (card.context) {
    const ctx = card.context;
    const block = el('section', { class: 'tc-section tc-context' });
    block.appendChild(el('h4', {}, ctx.title));
    const head = el('div', { class: 'tc-context-head' });
    head.appendChild(el('strong', { class: 'tc-context-value' }, ctx.value));
    if (ctx.badge) head.appendChild(el('span', { class: 'tc-context-badge' }, ctx.badge));
    block.appendChild(head);
    block.appendChild(el('p', {}, ctx.text));
    if (ctx.stats.length) {
      const stats = el('div', { class: 'tc-context-stats' });
      for (const stat of ctx.stats) stats.appendChild(tipCardItem(stat, 'tc-stat'));
      block.appendChild(stats);
    }
    if (ctx.details) block.appendChild(tipCardSection(ctx.details));
    root.appendChild(block);
  }

  if (card.risk) {
    const risk = el('section', { class: 'tc-section tc-risk' });
    risk.appendChild(el('h4', {}, card.risk.title));
    risk.appendChild(el('p', {}, card.risk.text));
    const stats = el('div', { class: 'tc-risk-stats' });
    for (const stat of card.risk.stats) stats.appendChild(tipCardItem(stat, 'tc-stat'));
    risk.appendChild(stats);
    root.appendChild(risk);
  }

  for (const section of card.sections) root.appendChild(tipCardSection(section));
  if (card.footer) root.appendChild(el('footer', { class: 'tc-footer' }, card.footer));
  return root;
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

function frame(svg, { width, height, margin, yMin, yMax, yFormat = compactMoney, grid = true }) {
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

  // `grid: false` skips the default nice-number gridlines/labels for a
  // caller that draws its own (e.g. the Capital projection chart's fixed
  // $100k/$10k grid) -- it still gets the sized svg, `group` and `y` scale.
  if (grid) {
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
    // A pinned tooltip holds until T closes it; moving the crosshair underneath it
    // would point at a different day than the readout describes.
    if (tooltipPinned) return;
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
    title: 'Account cash minus CSP collateral; only on Positions snapshot dates.',
  },
  {
    text: 'Unrealized',
    title: 'Unrealized gains plus equity this history can\'t track, blank except on a Positions snapshot date.',
  },
  { text: 'Total value', title: 'Total committed + Cash + Unrealized: the whole account.' },
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
    let estimated = false;
    for (const timeline of timelines) {
      const latest = lastSnapshotAtOrBefore(timeline, asOf);
      if (latest) {
        totalValue += latest.total_value;
        cashTotal += latest.cash_total || 0;
        anyKnown = true;
        // A configured opening balance (data/accounts.json's "opening_balances",
        // spliced into net_worth.timeline server-side -- see Dashboard._build_net_worth)
        // rather than a real, itemized Positions snapshot -- flagged through so
        // the chart can mark that one dot as a guess instead of a reading.
        if (latest.estimated) estimated = true;
      }
    }
    if (!anyKnown) continue;
    const amount = totalValue - point.total;
    if (amount <= 1e-9) continue;
    const cash = Math.min(Math.max(cashTotal - point.put, 0), amount);
    // `totalValue` (this snapshot's real total account value) rides along too
    // -- the day-to-day interpolation between two snapshots needs it to
    // derive the not-deployed amount from *that day's own* committed capital,
    // rather than interpolating cash/unrealized on their own and stacking
    // them on top of a committed-capital figure that can jump independently
    // in between (a CSP sold the day after a snapshot moves cash into
    // collateral without changing total value at all -- see drawCapital()'s
    // ndInterp).
    map.set(asOf, { amount, cash, unrealized: amount - cash, estimated, totalValue });
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

  // A configured opening balance (data/accounts.json's "opening_balances")
  // can predate the account's first real trade -- `points` (capital_series)
  // only ever covers actual trade activity, so there'd be no day for that
  // dot to land on otherwise. Prepend a $0-committed day for it: true,
  // nothing was in the wheel yet, so it reads as "all cash" once the Cash +
  // Unrealized overlay below picks it up from net_worth.timeline.
  // net_worth.timeline (like benchmark.series) is built once from the whole
  // account history and ignores the dashboard's date-range filter entirely
  // -- so the opening balance's own date can predate the *selected* range's
  // start even when it doesn't predate the full history. Prepending it
  // regardless would silently widen the x-axis back past a range the reader
  // explicitly chose to narrow to; `state.start` (set) wins over it, same as
  // it already does for the Capital projection chart's history.
  const earliestEstimated = (netWorth && netWorth.available ? netWorth.timeline || [] : [])
    .filter((snapshot) => snapshot.estimated)
    .map((snapshot) => snapshot.as_of)
    .sort()[0];
  if (
    earliestEstimated &&
    earliestEstimated < points[0].date &&
    (!state.start || earliestEstimated >= state.start)
  ) {
    points = [
      { date: earliestEstimated, put: 0, stock: 0, call: 0, long: 0, spread: 0, idle_stock: 0, call_stock: 0, total: 0 },
      ...points,
    ];
  }

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
          // Interpolate *total account value* -- the one figure only known at
          // sparse snapshot dates -- then subtract that day's own real
          // committed capital (capital_series, known every day) to get the
          // not-deployed amount. Interpolating cash/unrealized directly and
          // stacking them on top of committed capital was wrong: selling a
          // put the day after a snapshot moves cash into collateral without
          // changing total value at all, but the interpolated cash figure
          // wouldn't have caught up yet, so the same dollars got counted in
          // both bands at once -- a false spike right on the trade date.
          const totalValue = a.totalValue + (b.totalValue - a.totalValue) * f;
          const amount = Math.max(totalValue - p.total, 0);
          // The cash/unrealized split itself is interpolated as a ratio of
          // that (now-correct) amount, not as absolute dollars.
          const cashFraction = (x) => (x.amount > 1e-9 ? x.cash / x.amount : 1);
          const fraction = cashFraction(a) + (cashFraction(b) - cashFraction(a)) * f;
          const cash = Math.min(Math.max(fraction, 0), 1) * amount;
          return { cash, unrealized: amount - cash };
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

    const fillRun = (lower, upper, color, opacity = BAND_WASH) => {
      for (const run of bandRuns(lower, upper)) {
        const top = run.map((i) => `${x(points[i].date)},${y(upper[i])}`);
        const bottom = run.map((i) => `${x(points[i].date)},${y(lower[i])}`).reverse();
        group.appendChild(
          svgEl('path', {
            d: `M${top.join('L')}L${bottom.join('L')}Z`,
            fill: color,
            'fill-opacity': opacity,
            stroke: 'none',
          })
        );
      }
    };
    const notDeployedColor = cssVar(NOT_DEPLOYED_COLOR);
    const unrealizedColor = cssVar(UNREALIZED_COLOR);
    // Unrealized fills noticeably denser than Cash (not just a different grey
    // at the same low wash) so the two read apart at a glance, not just on
    // close inspection -- two adjacent bands this close in hue were
    // indistinguishable at a shared low opacity.
    fillRun(cashLower, cashUpper, notDeployedColor, BAND_WASH);
    fillRun(cashUpper, unrUpper, unrealizedColor, BAND_WASH * 1.9);

    // A firm cap along the Cash/Unrealized boundary itself, in Unrealized's
    // own full-strength colour -- the same "edge, not outline" cap the hued
    // capital bands above use to stay legible where two fills meet, which
    // this pair never had (a fill-only boundary is still a soft, easy-to-miss
    // transition even once the two washes are visibly different densities).
    for (const run of bandRuns(cashUpper, unrUpper)) {
      group.appendChild(
        svgEl('path', {
          d: 'M' + run.map((i) => `${x(points[i].date)},${y(cashUpper[i])}`).join('L'),
          fill: 'none',
          stroke: unrealizedColor,
          'stroke-width': 1.5,
          'stroke-linejoin': 'round',
        })
      );
    }

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

    // Dots: real snapshot readings, at each sub-band's true peak -- except a
    // dot flagged `estimated` (a configured opening balance standing in for
    // a missing earlier snapshot, see notDeployedByDate()), which draws
    // hollow with a dashed ring instead of solid-filled: the same "this
    // figure is a guess" vocabulary the estimate capital band above uses,
    // so a reader never mistakes a manual number for an itemized reading.
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
            fill: nd.estimated ? surface : stop.color,
            stroke: nd.estimated ? stop.color : surface,
            'stroke-width': nd.estimated ? 1.5 : 2,
            'stroke-dasharray': nd.estimated ? '2,1.5' : 'none',
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
        const tag = nd.estimated ? ' (estimated)' : '';
        rows.push(
          { label: 'Cash' + tag, value: money(nd.cash), color: cssVar(NOT_DEPLOYED_COLOR) },
          { label: 'Unrealized' + tag, value: money(nd.unrealized), color: cssVar(UNREALIZED_COLOR) },
          { label: 'Total value' + tag, value: money(point.total + nd.amount) }
        );
      }
      showTooltip(
        at,
        longDate(point.date),
        rows,
        formula([
          'Total committed = sum of every band above',
          `= ${CAPITAL_BANDS.map((c) => money(point[c.key] || 0)).join(' + ')}`,
          `= ${money(point.total)}`,
          ...(nd !== undefined
            ? [
                '',
                nd.estimated
                  ? 'Total value = configured opening balance (data/accounts.json),'
                  : 'Total value (Positions snapshot only) = Total committed + Cash + Unrealized',
                nd.estimated
                  ? '  booked as cash in place of a missing earlier snapshot.'
                  : `= ${money(point.total)} + ${money(nd.cash)} + ${money(nd.unrealized)} = ${money(point.total + nd.amount)}`,
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
    const hasEstimate = ndAnchors.some((a) => a.estimated);
    const note = hasEstimate
      ? `Dots = ${ndAnchors.length} snapshots (the earliest is a configured opening balance, shown hollow); band between is interpolated.`
      : `Dots = ${ndAnchors.length} snapshots; band between is interpolated.`;
    legend.appendChild(el('span', { class: 'legend-note' }, note));
  } else if (ndAnchors.length === 1 && ndAnchors[0].estimated) {
    legend.appendChild(
      el('span', { class: 'legend-note' }, 'The hollow dot is a configured opening balance, not a Positions snapshot.')
    );
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
            'Cash = Account cash - Open CSP collateral',
            '  (collateral is a hold on this same cash, so it is netted out)',
            `= ${money(cashTotal)} - ${money(phaseMeta.puts.amount)} = ${money(c.amount)}`,
          ])
        : c.key === 'unrealized'
          ? formula([
              'Unrealized = Total value (Positions snapshot) - Capital deployed - Cash',
              `= ${money(totalValue)} - ${money(deployed)} - ${money(cash)} = ${money(c.amount)}`,
              'Unrealized gains vs. cost basis, plus equity not tracked here.',
            ])
          : c.estimate
            ? formula([
                `${c.label} ≈ Strike × 100 × Contracts (faded: an estimate)`,
                '  Shares predate the export, so their real cost basis is unknown.',
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

/**
 * `pnl_series` only has closed-trade events, so it understates today: shares
 * still held and options still open haven't generated a closing transaction,
 * so an unrealized loss on them reads as break-even. This appends one extra
 * point, dated today, built from `data.portfolio`'s five already-computed
 * mark-to-market terms (PortfolioMetrics.mark_to_market_pl in metrics.py):
 * realized option P/L, realized stock P/L, dividends, unrealized stock P/L,
 * open-option P/L. Only drawPnl() sees this; Cash Flow/PPD keep reading the
 * untouched `pnl_series` (render() passes each its own copy), and the
 * point's `option_pl`/`stock_pl`/`total_pl` deltas are 0 -- it's a snapshot,
 * not a cash event those charts should bucket.
 *
 * `cum_stock_pl` here is a chart *position*, not an accounting figure --
 * it's everything besides realized option P/L (unrealized stock, dividends,
 * open-option P/L included), kept only so the Full wheel P/L line still
 * plots as Premium + this. The real breakdown (5 separate terms) is what
 * drawPnl()'s tooltip shows for this point, never collapsed into "Stock
 * P/L" as if that were its own calculation.
 *
 * Skipped when there's no prior point to extend, mark-to-market is missing,
 * or the view is cropped to a past end date (today's prices don't belong on
 * a deliberately historical slice).
 */
function appendMarkToMarketPoint(series, meta, portfolio) {
  if (!series.length || !portfolio) return series;
  if (portfolio.mark_to_market_pl == null) return series;
  if (!meta.through || !meta.data_last_date || meta.through < meta.data_last_date) return series;
  const last = series[series.length - 1];
  const todayIso = localIso(Date.now());
  if (todayIso <= last.date) return series; // last close already is today (or data is dated past it)
  return [
    ...series,
    {
      date: todayIso,
      option_pl: 0,
      stock_pl: 0,
      total_pl: 0,
      cum_option_pl: last.cum_option_pl,
      cum_stock_pl: portfolio.mark_to_market_pl - last.cum_option_pl,
      cum_total_pl: portfolio.mark_to_market_pl,
      synthetic: true,
      option_realized_pl: portfolio.option_realized_pl,
      stock_realized_pl: portfolio.stock_realized_pl,
      stock_unrealized_pl: portfolio.stock_unrealized_pl,
      option_open_pl: portfolio.option_open_pl,
      dividends_received: portfolio.dividends_received,
    },
  ];
}

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

  // The synthetic "today" point (see appendMarkToMarketPoint()) is never a
  // closed trade -- its whole segment is drawn dashed, same convention the
  // Capital projection chart uses for its own hypothetical-vs-real split, so
  // "this part is a live mark, not a historical fact" reads at a glance.
  const lastPoint = series[series.length - 1];
  const hasMtm = lastPoint.synthetic === true;
  const realSeries = hasMtm ? series.slice(0, -1) : series;

  lines.forEach((line, index) => {
    const realPath = realSeries.map((point) => `${x(point.date)},${y(point[line.key])}`).join('L');
    group.appendChild(
      svgEl('path', {
        d: 'M' + realPath,
        fill: 'none',
        stroke: colors[index],
        'stroke-width': 2,
        'stroke-linejoin': 'round',
        'stroke-linecap': 'round',
      })
    );
    if (hasMtm && realSeries.length) {
      const bridgeFrom = realSeries[realSeries.length - 1];
      group.appendChild(
        svgEl('path', {
          d: `M${x(bridgeFrom.date)},${y(bridgeFrom[line.key])}L${x(lastPoint.date)},${y(lastPoint[line.key])}`,
          fill: 'none',
          stroke: colors[index],
          'stroke-width': 2,
          'stroke-linejoin': 'round',
          'stroke-linecap': 'round',
          'stroke-dasharray': '5,4',
        })
      );
    }
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
      // The synthetic point (see appendMarkToMarketPoint()) isn't a closed
      // trade -- its own tooltip, not the chart's usual two-line split,
      // which would otherwise present "Stock P/L" here as if it were a real
      // figure when it's actually a residual blending in dividends and
      // open-option P/L too. This lists the five real terms instead.
      if (point.synthetic) {
        showTooltip(
          at,
          `Wheel P/L today: ${money(point.cum_total_pl, { cents: true })}`,
          [
            { label: 'Option P/L', value: money(point.option_realized_pl, { cents: true }) },
            { label: 'Realized stock P/L', value: money(point.stock_realized_pl, { cents: true }) },
            { label: 'Unrealized stock P/L', value: money(point.stock_unrealized_pl, { cents: true }) },
            { label: 'Open option P/L', value: money(point.option_open_pl, { cents: true }) },
            { label: 'Dividends', value: money(point.dividends_received, { cents: true }) },
          ],
          null
        );
        return;
      }
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
          'Cumulative Full wheel P/L = Premium to date + Stock P/L to date',
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
  if (hasMtm) {
    legend.appendChild(
      el('span', { class: 'legend-note' }, 'Dashed = today, mark-to-market (not yet realized)')
    );
  }

  buildTable(
    'pnl-table',
    ['Date', 'Premium that day', 'Stock P/L that day', 'Cum. premium', 'Cum. stock P/L', 'Cum. full wheel P/L'],
    series.map((point) => [
      point.synthetic ? 'Today (mark-to-market)' : point.date,
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
      'Dated to when cash settles, not to when the position closes.',
    ]),
  },
  {
    text: 'Wheel realized P/L',
    title: formula([
      'Wheel realized P/L = Option P/L + Stock P/L',
      'Dated to when a leg closes or a lot is sold, not when premium was collected.',
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
 * Realized wheel cash flow by calendar month: one diverging stacked bar per
 * month -- cash In (credits: premium sold, dividends) stacked above zero,
 * cash Out (debits + fees: BTC costs, protective premiums paid) stacked
 * below -- the same stacked-bar idiom `drawPeriodPl` uses, so composition
 * (how much came in vs. went out) and net direction read in one glance. Reuses
 * `frame()` for the y-scale/gridlines, same as every other dollar chart, but
 * the x-axis is categorical (one slot per month) rather than the continuous
 * date scale `timeAxis()` assumes.
 *
 * Realized wheel P/L is overlaid as its own line, not a third stacked
 * segment -- it is not additive with In/Out (it is a different, event-dated
 * view of overlapping cash, not a component of this month's cash total) and
 * answers a different question than the bars do. Cash flow books a credit
 * the moment premium is *sold* (STO); wheel P/L only once the leg actually
 * *closes*. So a month heavy on new short premium but light on closes shows
 * a tall In segment next to a flat wheel-P/L line -- that gap is the whole
 * point of showing both: it is what separates "cash is coming in" from "the
 * wheel is actually profitable," which is what decides whether this can be
 * relied on as real monthly income. See the dashboard's explanation for the
 * full mechanics (also in docs/DESIGN.md).
 *
 * Two reference lines, averaged over every month `rows` shows (the same
 * selected-range summary `renderCashFlowTiles`'s "Avg monthly income" tile
 * uses -- see `wheel.cashflow.range_summary`): a dashed red line at the
 * range's avg monthly income, and a dotted line (wheel's own color) at its
 * avg wheel realized P/L. Both are plain averages, not clamped to zero, so a
 * losing range draws its line below the zero axis exactly like a losing
 * month's bar would -- no separate handling for a negative average anywhere
 * here.
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
  // Gross In/Out, not just their net, so the bars honestly show how much
  // actually moved each direction -- a high-turnover month (big credits, big
  // debits, small net) needs an axis tall enough to fit the gross legs, not
  // just the net that's left over.
  const inFlow = rows.map((row) => row.gross_credits);
  const outFlow = rows.map((row) => -(row.gross_debits + row.fees));
  const values = [...inFlow, ...outFlow, ...wheelPl.map((w) => w.total_pl)];
  if (avgIncome !== null && avgIncome !== undefined) values.push(avgIncome);
  if (avgWheelPl !== null && avgWheelPl !== undefined) values.push(avgWheelPl);
  const yMin = Math.min(0, ...values) * 1.15;
  const yMax = Math.max(0, ...values, 1) * 1.15;

  const { group, plotWidth, plotHeight, y } = frame(svg, { width, height, margin, yMin, yMax });

  const slot = plotWidth / rows.length;
  const barWidth = Math.max(8, Math.min(48, slot * 0.6));
  const hitWidth = Math.max(barWidth, Math.min(slot * 0.9, slot - 2));
  const positive = cssVar('--pos'); // credit/inflow identity -- direction, not P/L sign
  const negative = cssVar('--neg'); // debit/outflow identity -- direction, not P/L sign
  const wheelColor = cssVar('--series-2'); // same color drawPnl() uses for "Full wheel P/L"
  const zeroY = y(0);
  const centers = rows.map((_, index) => margin.left + slot * (index + 0.5));

  group.appendChild(
    svgEl('line', { class: 'axis-line', x1: margin.left, x2: margin.left + plotWidth, y1: zeroY, y2: zeroY })
  );

  // Phase 1: In/Out bars for every month, bottom layer.
  rows.forEach((_, index) => {
    const cx = centers[index];
    const inValue = inFlow[index];
    const outValue = outFlow[index];

    if (inValue > 0) {
      const barHeight = Math.max(zeroY - y(inValue), 1.5);
      group.appendChild(
        svgEl('rect', {
          class: 'mark',
          x: cx - barWidth / 2,
          y: y(inValue),
          width: barWidth,
          height: barHeight,
          rx: 3,
          fill: positive,
        })
      );
    }
    if (outValue < 0) {
      const barHeight = Math.max(y(outValue) - zeroY, 1.5);
      group.appendChild(
        svgEl('rect', {
          class: 'mark',
          x: cx - barWidth / 2,
          y: zeroY,
          width: barWidth,
          height: barHeight,
          rx: 3,
          fill: negative,
        })
      );
    }
    if (inValue === 0 && outValue === 0) {
      // A real $0 month, not a gap -- a flat tick on the baseline instead of
      // an empty column that would otherwise read as missing data.
      group.appendChild(
        svgEl('rect', { x: cx - barWidth / 2, y: zeroY - 1, width: barWidth, height: 2, rx: 1, fill: cssVar('--axis') })
      );
    }
  });

  // Phase 2: wheel realized P/L as a connected line + per-month dot, on top
  // of the bars -- see the function doc for why it's a line, not a third
  // stacked segment.
  const wheelPath = centers.map((cx, index) => `${cx},${y(wheelPl[index].total_pl)}`).join('L');
  group.appendChild(
    svgEl('path', {
      d: 'M' + wheelPath,
      fill: 'none',
      stroke: wheelColor,
      'stroke-width': 2,
      'stroke-linejoin': 'round',
      'stroke-linecap': 'round',
    })
  );
  wheelPl.forEach((wp, index) => {
    group.appendChild(
      svgEl('circle', {
        cx: centers[index],
        cy: y(wp.total_pl),
        r: 3,
        fill: wheelColor,
        stroke: cssVar('--surface-1'),
        'stroke-width': 1.5,
      })
    );
  });

  // Phase 3: one transparent hit target per column, topmost so it receives
  // the pointer over both the bars and the line -- spans the whole column,
  // not just the bars, so hovering anywhere over a month (including a $0
  // one) shows the full cash-flow-and-wheel-P/L breakdown together.
  rows.forEach((row, index) => {
    const cx = centers[index];
    const wp = wheelPl[index];
    const hit = svgEl('rect', {
      class: 'hit',
      x: cx - hitWidth / 2,
      y: margin.top,
      width: hitWidth,
      height: plotHeight,
      tabindex: '0',
      'aria-label': `${monthLabel(row.period)}: net cash flow ${money(row.net_cash_flow, {
        cents: true,
      })}, wheel realized P/L ${money(wp.total_pl, { cents: true })}`,
    });
    group.appendChild(hit);
    attachTip(
      hit,
      monthLabel(row.period),
      [
        { label: 'Gross credits', value: money(row.gross_credits, { cents: true }) },
        { label: 'Gross debits', value: money(row.gross_debits, { cents: true }) },
        { label: 'Fees', value: money(row.fees, { cents: true }) },
        {
          label: 'Net cash flow',
          value: money(row.net_cash_flow, { cents: true }),
          valueClass: row.net_cash_flow < 0 ? 'neg' : 'pos',
        },
        {
          label: 'Wheel realized P/L',
          value: money(wp.total_pl, { cents: true }),
          color: wheelColor,
          valueClass: wp.total_pl < 0 ? 'neg' : 'pos',
        },
        { label: 'Avg collateral', value: money(row.avg_collateral) },
        { label: 'Monthly yield', value: pct(row.monthly_yield_pct, 2) },
      ],
      formula([
        'Net cash flow = Gross credits - Gross debits - Fees',
        `= ${money(row.gross_credits, { cents: true })} - ${money(row.gross_debits, { cents: true })} - ${money(row.fees, { cents: true })}`,
        `= ${money(row.net_cash_flow, { cents: true })}`,
        '',
        'Wheel realized P/L = Premium collected (net) + Stock P/L',
        '  Dated to leg close / lot sale, not to premium sale.',
        `= ${money(wp.option_pl, { cents: true })} + ${money(wp.stock_pl, { cents: true })}`,
        `= ${money(wp.total_pl, { cents: true })}`,
        '',
        'Monthly yield % = Net cash flow ÷ Avg allocated collateral × 100',
        `= ${money(row.net_cash_flow, { cents: true })} ÷ ${money(row.avg_collateral)} × 100`,
        `= ${row.monthly_yield_pct === null ? 'N/A; no collateral committed this month' : pct(row.monthly_yield_pct, 2)}`,
      ])
    );
  });

  // Reference lines only, no floating in-chart labels: now that the axis has
  // to stretch to fit gross In/Out (not just the smaller net/wheel-P/L
  // figures it used to), both averages sit close to zero, right where bars
  // are densest -- a floating label there collides with whatever bar is
  // underneath no matter which edge it's anchored to. The exact figure is
  // already in the legend below and in the line's own tooltip; the line
  // itself is still exact and still hoverable.
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
  }

  if (avgWheelPl !== null && avgWheelPl !== undefined) {
    // A dotted line, not dashed, so the two reference lines stay visually
    // distinct where they land close together -- and a plain linear y-scale
    // means a negative average needs no special handling: y(avgWheelPl)
    // already falls below the zero line exactly like a negative bar would.
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
        'Avg wheel realized P/L = Total wheel realized P/L ÷ Months shown',
        `  (${wheelPl.length} month(s))`,
        `= ${money(avgWheelPl, { cents: true })}`,
      ])
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
    `Monthly cash flow, split into money in and money out, versus realized wheel P/L (line), ` +
      `${rows.length} month(s) from ${monthLabel(rows[0].period)} to ` +
      `${monthLabel(rows[rows.length - 1].period)}. Hover or focus a month for its breakdown.` +
      (avgIncome !== null && avgIncome !== undefined
        ? ` Dashed red line marks the avg monthly income of ${money(avgIncome, { cents: true })} over this range.`
        : '') +
      (avgWheelPl !== null && avgWheelPl !== undefined
        ? ` Dotted orange line marks the avg wheel realized P/L of ${money(avgWheelPl, { cents: true })} over this range.`
        : '')
  );

  const inSwatchItem = el('span');
  const inSwatch = el('i');
  inSwatch.style.background = positive;
  inSwatchItem.appendChild(inSwatch);
  inSwatchItem.appendChild(document.createTextNode('In: premium + dividends collected'));
  legend.appendChild(inSwatchItem);

  const outSwatchItem = el('span');
  const outSwatch = el('i');
  outSwatch.style.background = negative;
  outSwatchItem.appendChild(outSwatch);
  outSwatchItem.appendChild(document.createTextNode('Out: BTC costs + fees paid'));
  legend.appendChild(outSwatchItem);

  const wheelSwatchItem = el('span');
  const wheelSwatch = el('i', { class: 'line' });
  wheelSwatch.style.background = wheelColor;
  wheelSwatchItem.appendChild(wheelSwatch);
  wheelSwatchItem.appendChild(document.createTextNode('Wheel realized P/L (what actually closed profitably)'));
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
      money(row.net_cash_flow, { cents: true, sign: true }),
      money(wheelPl[index].total_pl, { cents: true, sign: true }),
      money(row.avg_collateral),
      pct(row.monthly_yield_pct, 2),
    ])
  );
}

/* ------------------------------------------------- chart: periodic P/L */

const PERIOD_PL_TABLE_HEAD = [
  'Period',
  {
    text: 'Option P/L',
    title: 'Realized P/L (CSP + covered-call + hedge legs) from options that closed in this period.',
  },
  { text: 'Stock P/L', title: 'Realized P/L from shares sold or called away in this period.' },
  {
    text: 'Total Realized P/L',
    title: 'Option P/L + Stock P/L, realized only.',
  },
];

// One fixed identity color per metric, regardless of sign -- a bar's own
// position off the zero line already shows gain vs. loss, so color here
// answers "which source," not "up or down." Same colors drawPnl's cumulative
// option/stock lines use, so the two complementary P/L cards read as one
// system.
const PERIOD_PL_SERIES = [
  { key: 'option_pl', label: 'Option P/L', varName: '--series-1' },
  { key: 'stock_pl', label: 'Stock P/L', varName: '--series-3' },
];

const periodPlLabel = (row, granularity) => (granularity === 'week' ? weekLabel(row.period) : monthLabel(row.period));
const periodPlRangeLabel = (row, granularity) =>
  granularity === 'week' ? weekRangeLabel(row.week_start, row.week_end) : monthLabel(row.period);

/**
 * Stacks `PERIOD_PL_SERIES` values for one period into a diverging bar: each
 * series extends the positive cursor upward or the negative cursor downward
 * from zero, in series order, so same-signed values stack contiguously
 * (Option P/L touches zero, Stock P/L stacks beyond it) while opposite-signed
 * values extend to opposite sides of zero instead of overlapping and
 * visually canceling. Returns one `{ key, from, to, value }` segment per
 * non-zero series plus the resulting `top`/`bottom` extents of the whole bar.
 */
function stackPeriodPl(row) {
  let posCursor = 0;
  let negCursor = 0;
  const segments = [];
  for (const series of PERIOD_PL_SERIES) {
    const value = row[series.key] || 0;
    if (value === 0) continue;
    if (value > 0) {
      segments.push({ key: series.key, from: posCursor, to: posCursor + value, value });
      posCursor += value;
    } else {
      segments.push({ key: series.key, from: negCursor, to: negCursor + value, value });
      negCursor += value;
    }
  }
  return { segments, top: posCursor, bottom: negCursor };
}

/**
 * One diverging stacked bar per period -- Option P/L and Stock P/L stacked
 * from a shared zero baseline via `stackPeriodPl()`, so a glance answers
 * "profitable or not, and from which source" without implying a trend the
 * way a line would across discrete, unconnected periods. Each series keeps a
 * fixed identity color; sign is read from a segment's own side of the zero
 * line, never from color. Granularity ('week'/'month') is a view toggle, not
 * a filter -- both series are already in `periodPl` (data.period_pl), so
 * switching redraws from already-fetched data.
 */
function drawPeriodPl(periodPl) {
  const svg = $('chart-period-pl');
  const legend = $('legend-period-pl');
  const summary = $('period-pl-summary');
  clear(legend);
  clear(summary);
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

  // Summary covers exactly the periods on display -- the full history when
  // no date filter is set, the cropped range otherwise (`rows` is already
  // the display-cropped series the chart and table both draw from).
  const totalOption = rows.reduce((sum, row) => sum + (row.option_pl || 0), 0);
  const totalStock = rows.reduce((sum, row) => sum + (row.stock_pl || 0), 0);
  const totalRealized = rows.reduce((sum, row) => sum + (row.total_realized_pl || 0), 0);
  const summaryItem = (label, value, colorVar) => {
    const item = el('span');
    if (colorVar) {
      const swatch = el('i');
      swatch.style.background = colorVar;
      item.appendChild(swatch);
    }
    item.appendChild(document.createTextNode(label + ': '));
    item.appendChild(
      el('b', { class: 'legend-value ' + (value < 0 ? 'neg' : 'pos') }, money(value, { cents: true, sign: true }))
    );
    return item;
  };
  const colors = PERIOD_PL_SERIES.map((series) => cssVar(series.varName));
  summary.appendChild(summaryItem('Total realized', totalRealized));
  summary.appendChild(summaryItem('Option P/L', totalOption, colors[0]));
  summary.appendChild(summaryItem('Stock P/L', totalStock, colors[1]));

  const stacks = rows.map(stackPeriodPl);
  const margin = { top: 14, right: 20, bottom: 30, left: 62 };
  const width = chartWidth(svg);
  const height = 260;
  const yMax = Math.max(0, ...stacks.map((stack) => stack.top), 1) * 1.15;
  const yMin = Math.min(0, ...stacks.map((stack) => stack.bottom)) * 1.15;

  const { group, plotWidth, plotHeight, y } = frame(svg, { width, height, margin, yMin, yMax });
  const zeroY = y(0);
  group.appendChild(
    svgEl('line', { class: 'axis-line', x1: margin.left, x2: margin.left + plotWidth, y1: zeroY, y2: zeroY })
  );

  const slot = plotWidth / rows.length;
  const barWidth = Math.max(8, Math.min(48, slot * 0.6));
  const hitWidth = Math.max(barWidth, Math.min(slot * 0.9, slot - 2));
  const centers = rows.map((_, index) => margin.left + slot * (index + 0.5));

  rows.forEach((row, index) => {
    const cx = centers[index];
    const { segments } = stacks[index];

    if (!segments.length) {
      // A real $0 period, not a gap -- a flat tick on the baseline instead of
      // leaving an empty column that would otherwise read as missing data.
      group.appendChild(
        svgEl('rect', {
          x: cx - barWidth / 2,
          y: zeroY - 1,
          width: barWidth,
          height: 2,
          rx: 1,
          fill: cssVar('--axis'),
        })
      );
    }

    segments.forEach((segment) => {
      const seriesIndex = PERIOD_PL_SERIES.findIndex((series) => series.key === segment.key);
      const top = y(Math.max(segment.from, segment.to));
      const bottom = y(Math.min(segment.from, segment.to));
      group.appendChild(
        svgEl('rect', {
          class: 'mark',
          x: cx - barWidth / 2,
          y: top,
          width: barWidth,
          height: Math.max(bottom - top, 1.5),
          rx: 2,
          fill: colors[seriesIndex],
        })
      );
    });

    // A single transparent hit target spans the whole column (not just the
    // bar) so a $0 period -- no visible bar to land a pointer on -- is just
    // as hoverable/focusable as any other, and reports its real $0 breakdown
    // rather than looking unresponsive.
    const hit = svgEl('rect', {
      class: 'hit',
      x: cx - hitWidth / 2,
      y: margin.top,
      width: hitWidth,
      height: plotHeight,
      tabindex: '0',
      'aria-label': `${periodPlRangeLabel(row, granularity)}: total realized ${money(row.total_realized_pl, {
        cents: true,
      })}`,
    });
    group.appendChild(hit);
    attachTip(
      hit,
      periodPlRangeLabel(row, granularity),
      [
        ...PERIOD_PL_SERIES.map((series, i) => ({
          label: series.label,
          value: money(row[series.key] || 0, { cents: true }),
          color: colors[i],
          valueClass: (row[series.key] || 0) < 0 ? 'neg' : 'pos',
        })),
        {
          label: 'Total Realized P/L',
          value: money(row.total_realized_pl || 0, { cents: true }),
          valueClass: (row.total_realized_pl || 0) < 0 ? 'neg' : 'pos',
        },
      ],
      formula([
        'Total Realized P/L = Option P/L + Stock P/L',
        `= ${money(row.option_pl || 0, { cents: true })} + ${money(row.stock_pl || 0, { cents: true })}`,
        `= ${money(row.total_realized_pl || 0, { cents: true })}`,
      ])
    );
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
          x: centers[index],
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
      'Above zero = that period’s gain, below = its loss. Bars stack from zero; opposite-signed parts extend to opposite sides rather than canceling out.'
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
      money(row.option_pl, { cents: true, sign: true }),
      money(row.stock_pl, { cents: true, sign: true }),
      money(row.total_realized_pl, { cents: true, sign: true }),
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
        class: 'chart-endpoint-value',
        x: Math.min(centers[lastIndex] + 8, margin.left + plotWidth + margin.right - 4),
        y: y(currentPpd) + 3.5,
        'text-anchor': centers[lastIndex] + 8 > margin.left + plotWidth ? 'end' : 'start',
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

/**
 * A Dashboard tile's small print. An array of [name, value] pairs renders as
 * one "Name: value" line each; a plain string (a description with no figures
 * in it) renders as-is; null/undefined renders nothing. Values are shown
 * without a leading "+" -- only a negative number carries a sign.
 */
function tileFoot(foot) {
  if (foot === null || foot === undefined) return null;
  const node = el('div', { class: 'foot' });
  if (!Array.isArray(foot)) {
    node.textContent = foot;
    return node;
  }
  for (const [name, value] of foot) node.appendChild(el('div', {}, `${name}: ${value}`));
  return node;
}

function appendTileFoot(node, foot) {
  const footNode = tileFoot(foot);
  if (footNode) node.appendChild(footNode);
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

  // "Reliable as monthly income" is a consistency question, not just a size
  // one -- Avg monthly income already answers "how much, typically"; this
  // answers "how often was that actually true." Same win-rate convention the
  // Performance tile's leg-level win rate uses: an exact $0 month counts
  // toward neither side, and N/A (not 0%) when nothing has closed yet.
  const wheelWins = wheelPl.filter((w) => w.total_pl > 0).length;
  const wheelLosses = wheelPl.filter((w) => w.total_pl < 0).length;
  const wheelDecided = wheelWins + wheelLosses;
  const wheelWinRate = wheelDecided ? (100.0 * wheelWins) / wheelDecided : null;
  const cashWins = rows.filter((row) => row.net_cash_flow > 0).length;
  const cashLosses = rows.filter((row) => row.net_cash_flow < 0).length;

  const tiles = [
    {
      label: 'Avg monthly income',
      value: trailing.avg_monthly_income === null ? '—' : money(trailing.avg_monthly_income, { cents: true }),
      foot: [['Months in range', trailing.months_counted]],
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
      foot: [['Avg collateral', money(trailing.avg_collateral)]],
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
              '  (Avg monthly income × 12) ÷ Avg collateral (time-weighted) × 100',
              `= (${money(trailing.avg_monthly_income, { cents: true })} × 12) ÷ ${money(trailing.avg_collateral)} × 100`,
              `= ${pct(trailing.annualized_cash_on_cash_return_pct, 2)}`,
              'Income = all cash collected, incl. premium on open legs and dividends.',
            ]),
    },
    {
      // No `tone`, deliberately: this is a timing gap, not a verdict -- a
      // large gap isn't inherently good or bad (see the chart below for why),
      // so it shouldn't be colored red/green like a win/loss figure would be.
      label: 'Cash flow vs. wheel P/L gap',
      value: money(gap, { cents: true }),
      foot: [
        ['Cash flow', money(trailing.cash_flow, { cents: true })],
        ['Wheel P/L', money(rangeWheelPl, { cents: true })],
      ],
      formula: formula([
        `Gap = Cash flow - Wheel realized P/L (${trailing.months_counted} month(s))`,
        `= ${money(trailing.cash_flow, { cents: true })} - ${money(rangeWheelPl, { cents: true })}`,
        `= ${money(gap, { cents: true })}`,
        'Positive: cash ahead of realized P/L (open positions, dividends).',
        'Negative: realized P/L ahead of new premium.',
      ]),
    },
    {
      label: 'Months profitable',
      value: wheelDecided ? pct(wheelWinRate, 0) : '—',
      foot: wheelDecided
        ? [
            ['Profitable months', `${wheelWins} of ${wheelDecided}`],
            ['Cash-flow positive months', `${cashWins} of ${cashWins + cashLosses}`],
          ]
        : 'No month has closed with a nonzero realized P/L yet',
      tone: wheelDecided ? (wheelWinRate >= 50 ? 'pos' : 'neg') : undefined,
      formula: wheelDecided
        ? formula([
            'Months profitable = Months > $0 ÷ (Months > $0 + Months < $0)',
            '  Wheel realized P/L only, not cash timing; $0 months excluded.',
            `= ${wheelWins} ÷ (${wheelWins} + ${wheelLosses})`,
            `= ${pct(wheelWinRate, 0)}`,
          ])
        : formula(['No month in the selected range has a decided (nonzero) realized P/L yet.']),
    },
  ];

  for (const tile of tiles) {
    const node = el('div', { class: 'tile' });
    node.appendChild(el('div', { class: 'label' }, tile.label));
    node.appendChild(el('div', { class: 'value ' + (tile.tone || '') }, tile.value));
    appendTileFoot(node, tile.foot);
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
            'Annualized Wheel ROC = (Option P/L ÷ Avg capital) × (365 ÷ Days)',
            '',
            'N/A, no capital committed.',
          ])
        : formula([
            'x = Annualized Wheel ROC, y = Net realized P/L, area = Avg capital',
            '',
            `Annualized Wheel ROC = (Option P/L ÷ Avg capital) × (365 ÷ Days) = ${pct(row.annualized_wheel_roc_pct)}`,
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
              value: money(realized, { cents: true }),
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
          value: money(leg.realized_pl, { cents: true }),
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
    'Avg cap = time-weighted mean of the daily total',
    `  (${engagedDays} of ${totalDays} days; $0 days excluded)`,
    `= ${money(cycle.avg_collateral)}`,
  ]);

  const roi = formula([
    'ROI = Net realized P/L (option + stock) ÷ Initial collateral',
    `= ${money(cycle.net_realized_pl, { cents: true })} ÷ ${money(cycle.initial_collateral)}`,
    `= ${cycle.roi_pct === null ? 'N/A' : pct(cycle.roi_pct, 2)}`,
  ]);

  const wheelRoc =
    cycle.annualized_wheel_roc_pct === null
      ? formula([
          'Annualized Wheel ROC = (Wheel option P/L ÷ Avg collateral) × (365 ÷ Days active)',
          '',
          'N/A, no capital committed in this cycle.',
        ])
      : formula([
          'Annualized Wheel ROC = (Wheel option P/L ÷ Avg collateral) × (365 ÷ Days active)',
          '  Wheel option P/L = CSP + CC + hedge P/L; no stock P/L.',
          `= (${money(cycle.option_realized_pl, { cents: true })} ÷ ${money(cycle.avg_collateral)}) × (365 ÷ ${cycle.days_active})`,
          `= ${pct(cycle.roi_on_avg_wheel_pct, 2)} × ${(365 / cycle.days_active).toFixed(2)}`,
          `= ${pct(cycle.annualized_wheel_roc_pct)}`,
        ]);

  const netOptionYield =
    cycle.annualized_net_option_yield_pct === null
      ? formula([
          'Annualized Net Option Yield =',
          '  (Option P/L ÷ Initial collateral) × (365 ÷ Days active)',
          '',
          'N/A, no initial collateral committed in this cycle.',
        ])
      : formula([
          'Net Option Yield = Option P/L ÷ Initial collateral',
          '  Day-one capital, not time-weighted.',
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
          'Annualized Total Position ROI = (Option P/L + Stock P/L + Dividends)',
          '  ÷ Initial collateral × (365 ÷ Days active)',
          '',
          'N/A, no initial collateral committed in this cycle.',
        ])
      : formula([
          'Total Position ROI = (Option P/L + Stock realized P/L +',
          '  Stock unrealized P/L + Dividends) ÷ Initial collateral',
          '  Excludes open long-option (hedge) P/L.',
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
      ? formula(['Avg days in trade = Average days held per closed leg', '', 'N/A, no closed legs yet.'])
      : formula([
          `Avg days in trade = Average days held per closed leg (${held.length})`,
          `= ${cycle.avg_days_in_trade.toFixed(2)}`,
        ]);

  const premium = wheelOptionPlFormula(cycle, 'Premium (net)');

  const ppd = formula([
    'PPD = (Premium collected - Closeout cost) ÷ Days active',
    '  Option P/L only; no stock P/L.',
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
  ACTIVE: 'ACTIVE: something is still open, a short leg, a long hedge, or shares held.',
  NO_ACTIVITY: 'NO ACTIVITY: flat, but still this calendar year; another trade here would resume this wheel.',
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
        formula([
          isHold
            ? 'Buy & hold: shares only, no call ever written (a covered call would make it a wheel)'
            : 'Directional: long options only, no CSP, CC, shares, or assignment',
          '',
          'Counts toward every total; Wheel ROC / PPD / Net Option Yield show as a dash.',
        ])
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
          `${leg.paired_contracts} of ${leg.contracts} contract(s) are in a spread, collateralized`,
          `  there by strike width. The other ${contracts} naked contract(s):`,
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
        '  The backing shares are already counted as capital.',
      ]);
    }
    return formula([
      ...pairedNote,
      'Collateral = Strike × 100 × Naked contracts',
      '  (strike stands in for cost basis: shares predate the export)',
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
            'Raw assignment/purchase price (1099-B basis).',
            '"Unknown": shares predate this export, so the basis was never seen.',
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
            'Net adjusted cost basis = Tax basis - Cycle net option cash per share',
            '  (pro-rated to this lot). The wheel break-even, not the 1099-B basis.',
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
        'A roll closes one contract and opens another, same stock, same put or call, same day, with the ' +
          'new expiry on or after the old one. Reopening the exact contract you just closed is a round ' +
          "trip, not a roll. Contract counts don't have to match either: a real roll can close 4 and open 2."
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
            'Net credit = Cash from closed legs + Cash from opened legs',
            '  Positive = credit roll; negative = debit roll.',
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
            'ACQUIRE = -Strike × Shares (assigned / bought)',
            'DISPOSE = Strike × Shares (called away / sold)',
            'Uses the broker\'s equity fill when the export has one.',
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
      foot: [
        ['Realized premium', money(portfolio.option_realized_pl, { cents: true })],
        ['Calendar days', portfolio.days_span],
      ],
      tone: portfolio.profit_per_day >= 0 ? 'pos' : 'neg',
      primary: true,
      formula: formula([
        'Portfolio PPD = (Premium collected - Closeout cost) ÷ Days',
        '  All cycles; option P/L only, no stock P/L.',
        `= ${money(portfolio.option_realized_pl, { cents: true })} ÷ ${portfolio.days_span}`,
        `= ${money(portfolio.profit_per_day, { cents: true })}/day`,
      ]),
    },
    {
      label: 'Net realized P/L',
      value: money(portfolio.net_realized_pl, { cents: true }),
      foot: [
        ['Options', money(portfolio.option_realized_pl)],
        ['Stock', money(portfolio.stock_realized_pl)],
      ],
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
      // Still open: credit on open short options only (CSPs, covered calls),
      // sold but not yet realized. Open long options are a cost paid, not
      // premium collected, so they're left out (`open_short_premium`).
      foot: [
        ['Gross', money(portfolio.premium_received)],
        ['Paid to close', money(portfolio.premium_paid)],
        ['Still open', money(portfolio.open_short_premium)],
      ],
      tone: portfolio.option_realized_pl >= 0 ? 'pos' : 'neg',
      formula: wheelOptionPlFormula(portfolio, 'Wheel option P/L (this tile)'),
    },
    (() => {
      // Always exactly these parts, in this order, summing to Mark-to-market
      // P&L -- realized stock P/L and dividends only appear when non-zero, so
      // the common case stays a plain 3-term line without losing
      // reconciliation on an account where they matter. Same construction as
      // the Trade Log's own per-wheel Mark-to-market P&L tile, just summed
      // across every wheel in the account instead of one.
      const asMoney = (v) => money(v, { cents: true });
      const mtmTerms = [
        [portfolio.option_realized_pl, 'Realized options'],
        ...(portfolio.stock_realized_pl ? [[portfolio.stock_realized_pl, 'Realized stock']] : []),
        ...(portfolio.dividends_received ? [[portfolio.dividends_received, 'Dividends']] : []),
        [portfolio.stock_unrealized_pl, 'Shares held'],
        [portfolio.option_open_pl, `Open options${portfolio.option_open_pl_marked ? '' : ' (est.)'}`],
      ];
      return {
        label: 'Mark-to-market P&L',
        value: asMoney(portfolio.mark_to_market_pl),
        foot: mtmTerms.map(([v, label]) => [label, money(v, { cents: true })]),
        tone: portfolio.mark_to_market_pl >= 0 ? 'pos' : 'neg',
        formula: formula([
          `Mark-to-market P&L = ${mtmTerms.map(([, label]) => label).join(' + ')}`,
          '  Realized P/L plus open positions at current market value.',
          `= ${mtmTerms.map(([v]) => asMoney(v)).join(' + ')}`,
          `= ${asMoney(portfolio.mark_to_market_pl)}`,
        ]),
      };
    })(),
    {
      label: 'Annualized Wheel ROC',
      value: pct(portfolio.annualized_wheel_roc_pct),
      foot: [
        ['Return for the period', pct(portfolio.roi_on_avg_wheel_pct)],
        ['Days', portfolio.days_span],
      ],
      tone: (portfolio.annualized_wheel_roc_pct ?? 0) >= 0 ? 'pos' : 'neg',
      formula:
        portfolio.annualized_wheel_roc_pct === null
          ? formula([
              'Annualized Wheel ROC = (Option P/L ÷ Avg capital) × (365 ÷ Days)',
              '',
              'N/A, no capital has been committed in this window yet.',
            ])
          : formula([
              'Annualized Wheel ROC = (Option P/L ÷ Avg capital) × (365 ÷ Days)',
              '  Option P/L only; excludes stock P/L.',
              `= (${money(portfolio.option_realized_pl, { cents: true })} ÷ ${money(portfolio.avg_capital)}) × (365 ÷ ${days})`,
              `= ${pct(portfolio.roi_on_avg_wheel_pct, 2)} × ${scale.toFixed(2)}`,
              `= ${pct(portfolio.annualized_wheel_roc_pct)}`,
            ]),
    },
    {
      label: 'Annualized Active Wheel ROC',
      value: pct(portfolio.annualized_active_wheel_roc_pct),
      foot: [
        ['Return for the period', pct(portfolio.roi_on_avg_active_capital_pct)],
        ['Days', portfolio.days_span],
      ],
      tone: (portfolio.annualized_active_wheel_roc_pct ?? 0) >= 0 ? 'pos' : 'neg',
      formula:
        portfolio.annualized_active_wheel_roc_pct === null
          ? formula([
              'Annualized Active Wheel ROC = (Wheel option P/L ÷ Avg active capital) × (365 ÷ Days)',
              '',
              'N/A, no capital backing an open CSP or CC in this window.',
            ])
          : formula([
              'Annualized Active Wheel ROC = (Wheel option P/L ÷ Avg active capital) × (365 ÷ Days)',
              '  Active capital excludes shares with no covered call written.',
              `= (${money(portfolio.option_realized_pl, { cents: true })} ÷ ${money(portfolio.avg_active_capital)}) × (365 ÷ ${days})`,
              `= ${pct(portfolio.annualized_active_wheel_roc_pct)}`,
            ]),
    },
    {
      label: 'Annualized Net Option Yield',
      value: pct(portfolio.annualized_net_option_yield_pct),
      foot: [
        ['Return for the period', pct(portfolio.net_option_yield_pct, 2)],
        ['Days', portfolio.days_span],
      ],
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
              '  Denominator = sum of each cycle\'s day-one capital.',
              `= (${money(portfolio.option_realized_pl, { cents: true })} ÷ ${money(portfolio.total_initial_collateral)}) × (365 ÷ ${days})`,
              `= ${pct(portfolio.net_option_yield_pct, 2)} × ${scale.toFixed(2)}`,
              `= ${pct(portfolio.annualized_net_option_yield_pct)}`,
            ]),
    },
    {
      label: 'Annualized Total Position ROI',
      value: pct(portfolio.annualized_total_position_roi_pct),
      foot: [
        ['Dividends', money(portfolio.dividends_received)],
        ['Stock unrealized', money(portfolio.stock_unrealized_pl)],
      ],
      tone: (portfolio.annualized_total_position_roi_pct ?? 0) >= 0 ? 'pos' : 'neg',
      formula:
        portfolio.annualized_total_position_roi_pct === null
          ? formula([
              'Annualized Total Position ROI = (Option P/L + Stock P/L + Dividends)',
              '  ÷ Total initial collateral × (365 ÷ Days)',
              '',
              'N/A, no initial collateral committed in this window.',
            ])
          : formula([
              'Annualized Total Position ROI =',
              '  (Option P/L + Stock realized P/L + Stock unrealized P/L + Dividends)',
              '  ÷ Total initial collateral × (365 ÷ Days)',
              '  Excludes open long-option (hedge) P/L.',
              `= (${money(portfolio.option_realized_pl, { cents: true })} + ${money(portfolio.stock_realized_pl, { cents: true })} + ${money(portfolio.stock_unrealized_pl, { cents: true })} + ${money(portfolio.dividends_received, { cents: true })}) ÷ ${money(portfolio.total_initial_collateral)} × (365 ÷ ${days})`,
              `= ${pct(portfolio.total_position_roi_pct, 2)} × ${scale.toFixed(2)}`,
              `= ${pct(portfolio.annualized_total_position_roi_pct)}`,
            ]),
    },
    {
      label: 'Capital deployed',
      value: money(portfolio.capital_deployed_now),
      foot: [
        ['Average', money(portfolio.avg_capital)],
        ['Peak', money(portfolio.peak_capital)],
      ],
      formula: formula([
        'Today =',
        `  ${CAPITAL_FORMULA_SUM}`,
        `= ${money(portfolio.capital_deployed_now)}`,
        '',
        `Avg = time-weighted daily mean, $0 days excluded = ${money(portfolio.avg_capital)}`,
        `Peak = highest single day = ${money(portfolio.peak_capital)}`,
        '',
        `Avg includes ${money(portfolio.avg_capital - portfolio.avg_active_capital)} of idle shares (no covered call written).`,
      ]),
    },
    {
      // Secondary/diagnostic only -- Annualized Wheel ROC above is the
      // primary performance figure. wins + losses excludes open legs and
      // exact break-evens, so it can (and usually does) undercount total_legs;
      // spelling out "N/M closed" keeps that from reading as a mismatch.
      label: 'Win rate',
      value: pct(portfolio.win_rate_pct, 0),
      foot: [
        ['Wins', portfolio.wins],
        ['Losses', portfolio.losses],
        ['Decided', `${portfolio.wins + portfolio.losses} of ${portfolio.total_legs} legs`],
      ],
      formula:
        wl > 0
          ? formula([
              'Win rate = Winning legs ÷ (Winning legs + Losing legs)',
              '  Open and $0 legs excluded.',
              '',
              `= ${portfolio.wins} ÷ (${portfolio.wins} + ${portfolio.losses})`,
              `= ${pct(portfolio.win_rate_pct, 1)}`,
            ])
          : formula([
              'Win rate = Winning legs ÷ (Winning legs + Losing legs)',
              '',
              'N/A, no closed leg with a nonzero P/L yet.',
            ]),
    },
    {
      // Deliberately separate from Win rate above: a leg rolled into a new,
      // still-open contract counts here whether or not it's also a win or a
      // loss there. Whether the roll itself paid off is a further question
      // -- resolved_roll_wins/losses -- answered only once the whole chain
      // of rolls it belongs to stops rolling and actually closes.
      label: 'Roll rate',
      value: pct(portfolio.roll_rate_pct, 0),
      foot: [['Rolled', `${portfolio.rolled_legs} of ${portfolio.rollable_legs} closed puts/calls`]],
      formula: formula([
        'Roll rate = Puts/calls closed by a same-day roll ÷ Closed puts/calls',
        '  Long hedge legs excluded.',
        `= ${portfolio.rolled_legs} ÷ ${portfolio.rollable_legs}`,
        `= ${pct(portfolio.roll_rate_pct, 1)}`,
        '',
        `Resolved roll chains: ${portfolio.resolved_roll_wins} win / ${portfolio.resolved_roll_losses} loss`,
        `Open roll chains: ${money(portfolio.open_roll_credit)} net cash so far (not marked to market)`,
      ]),
    },
    {
      label: 'Avg days in trade',
      value: portfolio.avg_days_in_trade === null ? '—' : portfolio.avg_days_in_trade.toFixed(1),
      foot: [
        ['Rolls', portfolio.rolls],
        ['Assignments', portfolio.assignments],
      ],
      formula: formula([
        'Avg days in trade = Each cycle\'s avg days held, weighted by its',
        '  decided legs (closed with a nonzero P/L)',
        `= ${portfolio.avg_days_in_trade === null ? 'N/A, no decided legs yet' : portfolio.avg_days_in_trade.toFixed(1) + ' days'}`,
      ]),
    },
    {
      label: 'Cycles',
      value: String(portfolio.cycles),
      foot: [
        ['Active', portfolio.active_cycles],
        ['Tickers', portfolio.tickers],
      ],
      formula: formula([
        `Cycles = wheel sequences in the current filtered view = ${portfolio.cycles}`,
        `Active = of those, status = ACTIVE = ${portfolio.active_cycles}`,
        `Tickers = distinct underlyings among those cycles = ${portfolio.tickers}`,
      ]),
    },
    {
      label: 'Cash reconciliation',
      value: reconciliation.balanced ? 'Balanced' : 'Mismatch',
      foot: [
        ['Rows checked', reconciliation.rows_checked],
        ['Difference', reconciliation.delta],
      ],
      tone: reconciliation.balanced ? 'pos' : 'neg',
      formula: formula([
        'Δ = File cash total - Model cash total - Unmatched cash',
        '  Unmatched: closes whose opening leg is outside this window.',
        '  Balanced when Δ is under half a cent.',
        `Δ = ${money(reconciliation.file_cash_total, { cents: true })} - ${money(reconciliation.model_cash_total, { cents: true })} - ${money(reconciliation.unmatched_cash, { cents: true })}`,
        `= ${reconciliation.delta}`,
      ]),
    },
  ];

  for (const tile of tiles) {
    const node = el('div', { class: 'tile' + (tile.primary ? ' primary' : '') });
    node.appendChild(el('div', { class: 'label' }, tile.label));
    node.appendChild(el('div', { class: 'value ' + (tile.tone || '') }, tile.value));
    appendTileFoot(node, tile.foot);
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
// Wording for the "N days ___" line, keyed by hedge phase (wheel/api.py's
// _open_hedge_entry). Only DIRECTIONAL reads differently -- it isn't acting
// as insurance anymore, so "of protection left" would be a false claim.
const HEDGE_PHASE_DAYS_LABEL = {
  runway: 'of protection left',
  wind_down: 'of protection left',
  expiring: 'of protection left',
  directional: 'to expiry',
};

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
    el('span', { class: 'hedge-title' }, `${acct}${h.underlying} · ${h.label} · exp ${longDate(h.expiry)}`)
  );
  row.appendChild(main);

  // Facts line: what it cost, then either where the whole WHEEL stands (the
  // honest "are we winning" number for a genuine protective hedge -- it
  // deliberately blends in the cycle's premium history and share P&L) or,
  // once a hedge is DIRECTIONAL, this TRADE's own P&L instead -- blending in
  // unrelated wheel history would hide a losing directional bet behind an
  // unrelated share/premium gain, which is exactly what a DIRECTIONAL row is
  // warning about.
  const econ = el('div', { class: 'hedge-econ' });
  econ.appendChild(el('span', {}, `Hedge cost ${money(h.cost)}`));
  if (h.phase === 'directional' && typeof h.trade_pl_floor === 'number') {
    econ.appendChild(
      el(
        'span',
        {
          class: (h.trade_pl_floor >= 0 ? 'pos' : 'neg') + ' hedge-pl-floor',
          title:
            "Today's value minus what it cost, using intrinsic value only since there is no live " +
            'options quote. Any remaining time value would only reduce a loss shown here, or add ' +
            'to a gain: never the other way.',
        },
        `this trade's P&L ${money(h.trade_pl_floor, { sign: true })}`
      )
    );
  } else if (typeof h.wheel_pl_now === 'number') {
    econ.appendChild(
      el(
        'span',
        { class: h.wheel_pl_now >= 0 ? 'pos' : 'neg' },
        `wheel P&L now ${money(h.wheel_pl_now, { sign: true })}`
      )
    );
  }
  // "days of protection left" claims it's still doing insurance's job -- true
  // for RUNWAY/WIND_DOWN/EXPIRING, but exactly what a DIRECTIONAL row is
  // saying is no longer the case here. A lookup, not a ternary, so a future
  // phase needing its own wording extends this table instead of another
  // bolted-on condition.
  const daysLabel = HEDGE_PHASE_DAYS_LABEL[h.phase] || 'of protection left';
  econ.appendChild(el('span', {}, `${h.days_to_expiry} days ${daysLabel}`));
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

/* ------------------------------------------------- table: wheel price targets
 *
 * One row per running wheel with a phase-specific target -- `data.wheel_targets`,
 * built by `_build_wheel_targets_banner` in wheel/api.py. Same look and sort
 * behavior as the Planner tab's tables (Open option positions, CC/CSP
 * candidates): sortable `<th>`s, `.op-row`/`.num`/`.left` cell classes, the
 * same `wheelLink` jump-to-Trade-Log.
 *
 * Three separate target-related columns: CC TO EXIT (cc_to_exit, a
 * direct backend field, see ccToExitCell) and CSP TO ENTER (the ticker's
 * canonical backend result, see cspToEnterCell) are strike suggestions --
 * violet `.cc-target`, neutral, not a "met/not met" signal. Profit Target is the
 * one real pass/fail threshold, so it's colored green/red by whether last
 * price has reached it (same comparison Gap to Target already makes), not
 * violet.
 */
/**
 * CC TO EXIT, the one renderer every table and card uses. All four fields
 * come verbatim from the wheel's single backend calculation (wheel/ccexit.py,
 * copied onto each row as cc_to_exit / cc_to_exit_label / cc_to_exit_card):
 * a strike when one qualified, otherwise ALL SHARES COVERED / NO QUALIFYING
 * CC, or a dash when no shares are held. The card is the T tooltip.
 */
function ccToExitText(row) {
  if (row.cc_to_exit != null) return money(row.cc_to_exit, { cents: true });
  return row.cc_to_exit_label || '—';
}

function ccToExitCell(row) {
  const hasValue = row.cc_to_exit != null || row.cc_to_exit_label;
  const cell = el('td', { class: hasValue ? 'num cc-target' : 'num' }, ccToExitText(row));
  setTipCard(cell, row.cc_to_exit_card);
  return cell;
}

/**
 * CSP TO ENTER, the one lookup and renderer every table and card uses. The
 * value, label, foot facts and tooltip all come verbatim from the ticker's
 * single backend result (wheel/cspentry.py, served once per ticker as
 * data.csp_to_enter[TICKER]): a strike when a put qualified, otherwise NO
 * QUALIFYING CSP, or a dash when the ticker has no result. Nothing here
 * computes, rounds or substitutes a strike.
 */
function cspToEnterFor(ticker) {
  const entries = (state.data && state.data.csp_to_enter) || {};
  return (ticker && entries[ticker]) || null;
}

// Sort key for a CSP TO ENTER column: the strike, or null (sorts last).
function cspToEnterStrike(ticker) {
  const entry = cspToEnterFor(ticker);
  return entry ? entry.recommended_strike : null;
}

function cspToEnterText(entry) {
  if (!entry) return '—';
  if (entry.recommended_strike != null) return money(entry.recommended_strike, { cents: true });
  return entry.label || '—';
}

function cspToEnterCell(ticker) {
  const entry = cspToEnterFor(ticker);
  const cell = el('td', { class: entry ? 'num cc-target' : 'num' }, cspToEnterText(entry));
  if (entry) setTipCard(cell, entry.card);
  return cell;
}

const WHEEL_TARGETS_COLUMNS = [
  { key: 'underlying', label: 'Symbol', left: true },
  { key: 'wheel_phase', label: 'Type', left: true },
  { key: 'cc_to_exit', label: 'CC TO EXIT' },
  { key: 'csp_to_enter', label: 'CSP TO ENTER' },
  { key: 'last_close', label: 'Last Price' },
  { key: 'profit_target', label: 'Profit Target' },
  { key: 'gap_pct', label: 'Gap to Target' },
  { key: 'shares_total', label: 'Shares Held' },
  { key: 'cycle_id', label: 'Wheel', left: true },
];

function wheelTargetsRow(row) {
  const tr = el('tr', { class: 'op-row' });

  const symCell = el('td', { class: 'left ticker-cell' });
  if (row.name) symCell.title = row.name;
  symCell.appendChild(tickerLink(row.underlying));
  tr.appendChild(symCell);

  const typeLabel = row.wheel_phase === 'csp' ? 'CSP' : 'CC';
  const typeCell = el('td', { class: 'left' });
  typeCell.appendChild(
    el(
      'span',
      {
        class: 'badge op-type op-type-' + typeLabel,
        title: row.wheel_phase === 'csp' ? 'Preferred CSP entry' : 'Profit target (wheel economics)',
      },
      typeLabel
    )
  );
  tr.appendChild(typeCell);

  // Backend's already-computed numbers, tooltips included -- no formula
  // reconstruction here, see ccToExitCell / cspToEnterCell /
  // profit_target_explanation in wheel/api.py.
  tr.appendChild(ccToExitCell(row));
  tr.appendChild(cspToEnterCell(row.underlying));

  tr.appendChild(el('td', { class: 'num' }, money(row.last_close, { cents: true })));

  // Only the CC-phase gap is a met/not-met signal (last price closing in on
  // a profitable exit); the CSP-phase gap is just how far below the price
  // the CSP TO ENTER strike sits, not something to color pass/fail -- so
  // this tone is CC-only (the gap itself shows on both).
  const metTone = row.wheel_phase === 'cc' && typeof row.gap_pct === 'number' ? toneOf(-row.gap_pct) : '';
  const profitTargetCell = el('td', { class: 'num ' + metTone }, money(row.profit_target, { cents: true }));
  if (row.profit_target_explanation) profitTargetCell.title = row.profit_target_explanation;
  tr.appendChild(profitTargetCell);

  tr.appendChild(el('td', { class: 'num ' + metTone }, signedPct(row.gap_pct)));

  // Every share really held, including ones bought before the oldest loaded
  // history file (`shares_untracked`, wheel/api.py) -- those have no known
  // cost, which is why Profit Target can be blank on a CC row.
  const sharesCell = el(
    'td',
    { class: 'num' },
    row.shares_total ? Math.round(row.shares_total).toLocaleString('en-US') : '—'
  );
  if (row.shares_untracked) {
    sharesCell.title = [
      `${Math.round(row.shares_held || 0).toLocaleString('en-US')} with a known cost + `
        + `${Math.round(row.shares_untracked).toLocaleString('en-US')} bought before your loaded history.`,
      'Targets count only the shares with a known cost.',
    ].join('\n');
  }
  tr.appendChild(sharesCell);

  const wheelCell = el('td', { class: 'left op-wheel' });
  wheelCell.appendChild(wheelLink(row.cycle_id, row.underlying));
  tr.appendChild(wheelCell);

  return tr;
}

function renderWheelTargets() {
  const table = $('wheel-targets-table');
  if (!table) return;
  const card = $('wheel-targets-card');
  const rows = ((state.data && state.data.wheel_targets) || []).map((row) => ({
    ...row,
    shares_total: (row.shares_held || 0) + (row.shares_untracked || 0),
    csp_to_enter: cspToEnterStrike(row.underlying),
  }));
  if (card) card.hidden = rows.length === 0;
  clear(table);
  if (!rows.length) return;

  const { key, dir } = state.wheelTargetsSort;

  const thead = el('thead');
  const headRow = el('tr');
  for (const column of WHEEL_TARGETS_COLUMNS) {
    const th = el('th', { class: `sortable${column.left ? ' left' : ''}` }, column.label);
    if (key === column.key) th.textContent = column.label + (dir === 1 ? ' ▲' : ' ▼');
    th.addEventListener('click', () => {
      if (state.wheelTargetsSort.key === column.key) state.wheelTargetsSort.dir *= -1;
      else state.wheelTargetsSort = { key: column.key, dir: column.key === 'underlying' ? 1 : -1 };
      renderWheelTargets();
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
    const base = typeof av === 'string' ? av.localeCompare(bv) : av - bv;
    return base * dir || a.underlying.localeCompare(b.underlying);
  });

  const tbody = el('tbody');
  for (const row of sorted) tbody.appendChild(wheelTargetsRow(row));
  table.appendChild(tbody);
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

  // "Capital at risk" is a cost-basis/collateral reconstruction from the
  // transaction history, never the broker's own mark-to-market Total value
  // -- the two answer different questions and are not expected to match.
  // `untracked_equity_value` (Dashboard._build_net_worth) is the one
  // quantifiable piece of that gap: Positions-snapshot shares this history
  // has no real lot for at all (bought before every export loaded begins),
  // which count fully in Total value but essentially not here. The rest of
  // the gap -- idle cash, unrealized gains marked to market only in Total
  // value, short options' mark-to-market vs. their collateral -- isn't
  // separable into its own number, so the foot line names it without one.
  const untracked = totals.untracked_equity_value || 0;
  const deployedFoot = [
    ['Total value', money(totals.total_value)],
    ...(untracked > 1 ? [['Untracked shares', money(untracked)]] : []),
  ];

  // Total value and Cash are read verbatim off the broker's Positions
  // snapshot -- nothing computed to show a formula for, but four similarly-
  // scoped dollar figures (this pair, plus Capital at risk here and the
  // portfolio's own Capital deployed tile above, a different, narrower
  // reconstruction) need a foot line each so a reader isn't left guessing
  // which is which.
  const tiles = [
    {
      label: 'Total value',
      value: money(totals.total_value, { cents: true }),
      foot: 'Everything the account holds right now: cash plus every investment.',
      formula: formula([
        'Cash + all positions at market value.',
        'From your broker; not calculated.',
      ]),
    },
    {
      label: 'Cash',
      value: money(totals.cash_total, { cents: true }),
      foot: 'Money sitting uninvested. Already counted inside Total value.',
      formula: formula([
        'Cash not in stock or options.',
        'From your broker; not calculated.',
      ]),
    },
    {
      label: 'Capital at risk',
      value: money(totals.wheel_capital_deployed),
      foot: deployedFoot,
      formula: formula([
        `Capital deployed = ${CAPITAL_FORMULA_SUM}`,
        `= ${money(totals.wheel_capital_deployed)}`,
        'Excludes idle cash and unrealized gains.',
      ]),
    },
  ];

  // "Available Cash" for new CSPs (liquid cash minus collateral already securing
  // open puts) lives in its own card up near Covered-call candidates -- see
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
        foot: [['Value added', money(wheelReturn.value_added, { cents: true })]],
        tone: beat ? 'pos' : 'neg',
        formula: formula([
          'XIRR: annualized return that accounts for when each dollar went in or out.',
          `This wheel: ${pct(wheelReturn.xirr_pct, 1)}`,
          `Same cash flows in SPY: ${pct(wb.xirr_pct, 1)}`,
          `Difference: ${money(wheelReturn.value_added, { cents: true })}`,
          'Same dates on both sides. Dividends not counted.',
        ]),
      });
      // One extra compact tile per additional benchmark index (QQQ, ...).
      for (const entry of (wheelReturn.benchmarks || []).slice(1)) {
        if (entry.xirr_pct === null || entry.xirr_pct === undefined) continue;
        const ahead = (wheelReturn.xirr_pct ?? 0) >= entry.xirr_pct;
        tiles.push({
          label: `Wheel vs. ${entry.name} (XIRR)`,
          value: `${pct(wheelReturn.xirr_pct, 0)} ${ahead ? '›' : '‹'} ${pct(entry.xirr_pct, 0)}`,
          foot: [['Value added', money(entry.value_added, { cents: true })]],
          tone: ahead ? 'pos' : 'neg',
          formula: formula([
            'XIRR: annualized return that accounts for when each dollar went in or out.',
            `This wheel: ${pct(wheelReturn.xirr_pct, 1)}`,
            `Same cash flows in ${entry.name}: ${pct(entry.xirr_pct, 1)}`,
            `Difference: ${money(entry.value_added, { cents: true })}`,
            'Same dates on both sides. Dividends not counted.',
          ]),
        });
      }
    } else {
      tiles.push({
        label: 'Wheel-only return (XIRR)',
        value: pct(wheelReturn.xirr_pct, 1),
        foot: [
          ['Period', span],
          ['Still committed', money(wheelReturn.terminal_value)],
        ],
        tone: (wheelReturn.xirr_pct ?? 0) >= 0 ? 'pos' : 'neg',
        formula: formula([
          'XIRR: annualized return that accounts for when each dollar went in or out',
          '  (every option and share trade, plus what is still tied up today).',
          `${events.length} moves, ${span}: ${pct(wheelReturn.xirr_pct, 1)}`,
          'Not risk-adjusted. Dividends not counted.',
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
        'XIRR: annualized return that accounts for when each dollar went in or out.',
        `${events.length} moves since the opening balance, ${span},`,
        `  ending at ${money(benchmark.actual.terminal_value)} on ${benchmark.as_of}.`,
        'Whole account, from the accounts.json opening balance (can predate this wheel).',
      ]),
    });
  }

  for (const tile of tiles) {
    const node = el('div', { class: 'tile' });
    node.appendChild(el('div', { class: 'label' }, tile.label));
    node.appendChild(el('div', { class: 'value ' + (tile.tone || '') }, tile.value));
    appendTileFoot(node, tile.foot);
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

  // Index lines: SPY keeps the legacy `benchmark_value` key; any others carry a
  // `benchmark_value_<name>` key alongside it (see api.py BENCHMARK_TICKERS).
  const indexEntries =
    benchmark.benchmarks && benchmark.benchmarks.length
      ? benchmark.benchmarks
      : [benchmark.benchmark || { name: 'SPY' }];
  const indexLines = indexEntries.map((entry, i) => ({
    key: i === 0 ? 'benchmark_value' : `benchmark_value_${entry.name.toLowerCase()}`,
    label: `If held in ${entry.name} instead`,
    varName: `--series-${i + 2}`,
  }));

  const series = (benchmark.series || []).filter((point) => point.actual_value !== null);
  const tableHead = ['Date', 'Actual value', ...indexLines.map((l) => l.label)];
  if (!series.length) {
    clear(svg);
    buildTable('networth-table', tableHead, []);
    return;
  }

  const lines = [
    { key: 'actual_value', label: 'Actual account value', varName: '--series-1' },
    ...indexLines,
  ];
  const colors = lines.map((line) => cssVar(line.varName));

  const values = series.flatMap((point) => lines.map((line) => point[line.key]).filter((v) => v !== null));
  const margin = { top: 14, right: 58, bottom: 30, left: 62 };
  const width = chartWidth(svg);
  const height = 260;
  // Net worth never sits near $0, so anchoring the axis there (like the P/L
  // charts do) would squash the actual account-value swings into a thin band
  // at the top. Pad proportionally to the data's own range instead, so the
  // axis fits what's actually displayed.
  const dataMin = Math.min(...values);
  const dataMax = Math.max(...values);
  const pad = (dataMax - dataMin) * 0.08 || Math.abs(dataMax) * 0.02 || 1;
  const yMin = dataMin - pad;
  const yMax = dataMax + pad;

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
          'Actual: account value from your broker.',
          '"If held in SPY": the same deposits and withdrawals, on the same',
          '  dates, invested in SPY instead.',
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
    tableHead,
    series.map((point) => [
      point.as_of,
      money(point.actual_value, { cents: true }),
      ...indexLines.map((l) =>
        point[l.key] === null || point[l.key] === undefined ? '—' : money(point[l.key], { cents: true })
      ),
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
// id -> display label for every known account, refreshed on each accounts
// listing. Used by `currentAccountIsRetirement()` (Capital projection) to
// read the *current* selection's label without threading it through render().
let accountLabelsById = {};

function renderAccountChips(accounts) {
  const bar = $('accounts-bar');
  const host = $('account-chips');
  clear(host);
  accountLabelsById = Object.fromEntries(accounts.map((a) => [a.id, a.label]));

  // The currently selected account may no longer exist (a folder removed,
  // an account ignored via a config edit, ...) -- fall back to Combined
  // rather than stay silently pointed at something gone, which would keep
  // sending a doomed ?account=<stale-id> on every future load(). Checked
  // against the known ids, not just "is the switcher shown": a single
  // remaining account restored from data/config.json must not be reset out
  // from under a page that never had more than one to switch between in the
  // first place.
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
      queueConfigSave();
      renderAccountChips(accounts);
      load();
    });
    host.appendChild(chip);
  }
}

// The restored date-range preset (state.datePreset, from data/config.json --
// see applyConfigSnapshot(), applied well before this ever runs) can't be
// turned into concrete start/end dates until state.data.meta exists, since
// applyPreset() needs the account's actual data range -- so load() applies
// it once, right after its first successful response. Account/tickers/
// etc. need no such deferral: they're already sitting in `state` by
// the time refreshAccounts()/load() first run (see boot() at the bottom of
// this file), and renderAccountChips() falls back to Combined on its own the
// moment a restored account id turns out to be unknown.
let configRangeApplied = false;

// Optimistic until the first /api/accounts response actually says otherwise
// -- a fresh checkout or an empty Docker data/ volume has zero accounts, and
// load() must not keep hammering /api/dashboard (which has nothing to build
// yet) every 15s on the background poll. Flips back to true the moment an
// upload succeeds (loadSelectedSource() re-runs refreshAccounts() itself).
let hasAnyAccounts = true;

function renderNoDataBanner(empty) {
  const banner = $('no-data-banner');
  if (banner) banner.hidden = !empty;
  if (empty) {
    $('subtitle').textContent = 'No data yet: see below to add your first export.';
    $('subtitle').title = '';
  }
}

async function refreshAccounts() {
  try {
    const response = await fetch('/api/accounts');
    if (!response.ok) return;
    const listing = await response.json();
    hasAnyAccounts = (listing.accounts || []).length > 0;
    renderNoDataBanner(!hasAnyAccounts);
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
    resetSelectionState({ keepDateRange: false });

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
 * Back to the unfiltered view: no ticker selection, nothing expanded. Shared
 * by the account switcher, a new upload, and the Reset button (the Filters
 * card on the Dashboard tab). A ticker picked under one account may not
 * exist under another, so it always clears. The date range -- its own
 * control, shared across every tab, separate from this card -- is kept by
 * default; only a fresh upload passes `keepDateRange: false`, since the old
 * range may not even apply to the newly-loaded data. Reset leaves the date
 * range exactly as the reader set it: it is a Ticker reset, not an "undo
 * everything" button. (Status is no longer filtered here at all -- see the
 * three per-chart dropdowns, state.timelineStatus/sequencesStatus/
 * scatterStatus -- so there is nothing of theirs for Reset to clear.)
 */
function resetSelectionState({ keepDateRange = true } = {}) {
  state.tickers.clear();
  state.expanded.clear();
  if (!keepDateRange) {
    state.start = null;
    state.end = null;
    $('preset').value = 'all';
    $('start').value = '';
    $('end').value = '';
  }
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

/* ------------------------------------------------- per-chart status filters
 *
 * Wheel timelines, Wheel sequences, and the Ticker efficiency scatter each
 * carry their own status dropdown (state.timelineStatus/sequencesStatus/
 * scatterStatus), independent of one another and of everything else on the
 * page -- every other tile/chart/table always shows every status. Called
 * both on a fresh render() and from each dropdown's own change handler, so
 * a chart's filter survives a date-range/ticker/account change instead of
 * silently reverting to "all".
 */

/** Wheel sequences draws straight from `cycles` and shows each cycle's own
 * (date-window-scoped) `status` badge directly -- filtering on that same
 * field is exactly what's on screen. A client-side filter over data
 * already on hand, no fetch needed. */
function cyclesForStatus(status) {
  if (!state.data) return [];
  return status ? state.data.cycles.filter((cycle) => cycle.status === status) : state.data.cycles;
}

/** Wheel timelines is different: it labels (and strikes through/dots) each
 * row with the wheel's real, full-history status from the Trade Log via
 * matchTradeLogWheel -- not the raw, date-window-scoped `cycle.status` --
 * so a wheel still open in full history never reads "closed" just because
 * the current date filter happens to cut off its tail (see drawTimeline's
 * own "ordered" step, which does this exact same lookup). Filtering on the
 * raw field instead let a row into the "ACTIVE" bucket by the window's
 * accounting while the chart went on to label it CLOSED (or vice versa) --
 * this filters on the same resolved status the chart actually displays,
 * so the two can never disagree again.
 */
function timelineCyclesForStatus(status) {
  if (!state.data) return [];
  const cycles = state.data.cycles;
  if (!status) return cycles;
  return cycles.filter((cycle) => (matchTradeLogWheel(cycle) || cycle).status === status);
}

function applyTimelineFilter() {
  if (!state.data) return;
  drawTimeline(timelineCyclesForStatus(state.timelineStatus), state.data.meta.through);
}

function applySequencesFilter() {
  if (!state.data) return;
  renderCycles(cyclesForStatus(state.sequencesStatus));
}

/** The Ticker efficiency scatter draws from `tickers`, a per-underlying
 * rollup already summed across however many cycles that ticker has -- not
 * filterable client-side, since narrowing to one status means re-summing,
 * not just dropping rows. "All" (the default) reuses the already-fetched
 * `state.data.tickers` for free; any specific status re-fetches the same
 * dashboard endpoint with that one status added, and reads `tickers` back
 * out of the response -- same account/ticker/date filters as everything
 * else, this chart's own status on top, state.data itself untouched. */
async function applyScatterFilter() {
  if (!state.data) return;
  if (!state.scatterStatus) {
    drawTickerScatter(state.data.tickers);
    return;
  }
  const generation = loadGeneration;
  const params = dashboardQuery();
  params.set('status', state.scatterStatus);
  try {
    const response = await fetch('/api/dashboard?' + params.toString());
    if (!response.ok) throw new Error('HTTP ' + response.status);
    const scoped = await response.json();
    // A newer load() (account/date/ticker change) superseded this one while
    // the fetch was in flight -- its own render() will call this again with
    // the current filters, so applying this stale response would just draw
    // the wrong thing for a moment before being overwritten.
    if (generation !== loadGeneration) return;
    drawTickerScatter(scoped.tickers);
  } catch (error) {
    if (generation !== loadGeneration) return;
    drawTickerScatter(state.data.tickers); // fail open: unfiltered beats blank
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
  $('global-search').addEventListener('input', applySearchFilter);

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
    state.datePreset = event.target.value;
    queueConfigSave();
    if (event.target.value !== 'custom') load();
  });
  $('start').addEventListener('change', (event) => {
    state.start = event.target.value || null;
    state.datePreset = 'custom';
    $('preset').value = 'custom';
    queueConfigSave();
    load();
  });
  $('end').addEventListener('change', (event) => {
    state.end = event.target.value || null;
    state.datePreset = 'custom';
    $('preset').value = 'custom';
    queueConfigSave();
    load();
  });
  // Ticker/Status live in their own Dashboard card, separate from the
  // shared Date range control above the tabs -- Reset clears just those two
  // (and the Trade Log's expanded-wheel state), leaving the date range as
  // the reader set it.
  $('reset').addEventListener('click', () => {
    resetSelectionState();
    queueConfigSave();
    load();
  });

  // The three per-chart status dropdowns -- Wheel timelines, Wheel
  // sequences, Ticker efficiency -- deliberately skip queueConfigSave():
  // they always start back at "all" on a fresh page load, unlike every
  // other filter on this page. See state.timelineStatus/sequencesStatus/
  // scatterStatus and applyTimelineFilter()/applySequencesFilter()/
  // applyScatterFilter().
  $('timeline-status-filter').addEventListener('change', (event) => {
    state.timelineStatus = event.target.value;
    applyTimelineFilter();
  });
  $('sequences-status-filter').addEventListener('change', (event) => {
    state.sequencesStatus = event.target.value;
    applySequencesFilter();
  });
  $('scatter-status-filter').addEventListener('change', (event) => {
    state.scatterStatus = event.target.value;
    applyScatterFilter();
  });

  // Re-expresses one chart, so it redraws that chart rather than refetching.
  $('capital-mode').addEventListener('click', (event) => {
    const on = event.currentTarget.getAttribute('aria-pressed') === 'true';
    event.currentTarget.setAttribute('aria-pressed', on ? 'false' : 'true');
    state.capitalMode = on ? 'value' : 'share';
    queueConfigSave();
    if (state.data) drawCapital(state.data.capital_series, state.data.net_worth);
  });

  $('timeline-sort').addEventListener('click', (event) => {
    const toTime = event.currentTarget.getAttribute('aria-pressed') !== 'true';
    event.currentTarget.setAttribute('aria-pressed', toTime ? 'true' : 'false');
    event.currentTarget.textContent = toTime ? 'Sort by ticker' : 'Sort by time';
    state.timelineSort = toTime ? 'time' : 'ticker';
    queueConfigSave();
    applyTimelineFilter();
  });

  $('period-pl-granularity').addEventListener('click', (event) => {
    const toWeekly = event.currentTarget.getAttribute('aria-pressed') !== 'true';
    event.currentTarget.setAttribute('aria-pressed', toWeekly ? 'true' : 'false');
    event.currentTarget.textContent = toWeekly ? 'Monthly' : 'Weekly';
    state.periodPlGranularity = toWeekly ? 'week' : 'month';
    queueConfigSave();
    if (state.data) drawPeriodPl(state.data.period_pl || {});
  });

  const expCalBtn = $('exp-cal-grain');
  if (expCalBtn) {
    expCalBtn.addEventListener('click', () => {
      const i = EXP_CAL_GRAINS.indexOf(state.expCalGrain);
      state.expCalGrain = EXP_CAL_GRAINS[(i + 1) % EXP_CAL_GRAINS.length];
      // Deliberately leaves state.expCalZoom alone -- the reader's chosen
      // From/To range is a statement about which dates they want to see,
      // independent of how finely those dates are bucketed. Only Reset (or
      // picking new From/To dates) should change it.
      queueConfigSave();
      renderExpirationCalendar();
    });
  }

  const expCalZoomResetBtn = $('exp-cal-zoom-reset');
  if (expCalZoomResetBtn) {
    expCalZoomResetBtn.addEventListener('click', () => {
      state.expCalZoom = null;
      renderExpirationCalendar();
    });
  }

  const expCalStartInput = $('exp-cal-start');
  const expCalEndInput = $('exp-cal-end');
  if (expCalStartInput && expCalEndInput) {
    const applyExpCalRange = () => {
      const startVal = expCalStartInput.value;
      const endVal = expCalEndInput.value;
      if (!startVal || !endVal) return;
      const lo = parseDay(startVal).getTime();
      // The End date is inclusive -- the window runs through the end of that
      // calendar day, not its midnight start.
      const hi = Math.max(parseDay(endVal).getTime() + DAY_MS, lo + DAY_MS);
      state.expCalZoom = [lo, hi];
      renderExpirationCalendar();
    };
    expCalStartInput.addEventListener('change', applyExpCalRange);
    expCalEndInput.addEventListener('change', applyExpCalRange);
  }

  const expCalChart = $('chart-exp-calendar');
  if (expCalChart) expCalChart.addEventListener('wheel', handleExpCalWheel, { passive: false });

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
    queueConfigSave();
    render(); // re-read CSS custom properties for the new mode
  });

  const setPresentation = (on) => {
    document.body.classList.toggle('presentation', on);
    const btn = $('present-toggle');
    if (btn) btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    const hash = on ? '#present' : '';
    if (location.hash !== hash) history.replaceState(null, '', location.pathname + location.search + hash);
    if (state.data) render();
  };
  $('present-toggle').addEventListener('click', () =>
    setPresentation(!document.body.classList.contains('presentation'))
  );
  window.addEventListener('hashchange', () => setPresentation(location.hash === '#present'));
  if (location.hash === '#present') setPresentation(true);

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

/** The current filter state as a URLSearchParams -- shared by the dashboard
 * fetch and the CSV export links so a download matches what's on screen.
 * Deliberately carries no `status`: the backend still accepts that param
 * (see Filters.from_query in wheel/api.py), but nothing here sends it
 * anymore -- every tile/chart/table gets every status by default. The three
 * per-chart status dropdowns (Wheel timelines, Wheel sequences, Ticker
 * efficiency) add their own `status` on top of this base query when they
 * need one; see applyScatterFilter(). */
function dashboardQuery() {
  const params = new URLSearchParams();
  if (state.account && state.account !== COMBINED_ACCOUNT_ID) params.set('account', state.account);
  if (state.tickers.size) params.set('tickers', [...state.tickers].join(','));
  if (state.start) params.set('start', state.start);
  if (state.end) params.set('end', state.end);
  return params;
}

/** Point the header ⬇ CSV links at the export routes with the live filters. */
function updateExportLinks() {
  const base = dashboardQuery().toString();
  const set = (id, name, extra) => {
    const a = $(id);
    if (!a) return;
    const q = extra ? (base ? base + '&' + extra : extra) : base;
    a.href = `/api/export/${name}.csv${q ? '?' + q : ''}`;
  };
  set('export-cycles', 'cycles');
  set('export-tickers', 'tickers');
  const wheel = state.tradeLogCycleId ? `wheel=${encodeURIComponent(state.tradeLogCycleId)}` : '';
  set('export-tradelog', 'trade-log', wheel);
  set('export-closed-lots', 'closed-lots');
}

async function load({ background = false } = {}) {
  // Nothing to fetch yet -- the #no-data-banner is already telling the user
  // what to do. Without this, the 15s background poll would keep hitting
  // /api/dashboard (which has no account to build) and stack up "Could not
  // load data" notices forever. loadSelectedSource() flips hasAnyAccounts
  // back on via its own refreshAccounts() call right before it calls load()
  // again, so a successful upload recovers on its own.
  if (!hasAnyAccounts) return;
  const generation = ++loadGeneration;
  // The restored date-range preset (state.datePreset) gets one shot at being
  // recomputed against the account's real data range, win or lose -- marked
  // spent right away (not only on success) so a later, unrelated load()
  // (e.g. the user picking a filter after the first attempt merely failed on
  // the network) is never mistaken for "the first response" and made to
  // silently clobber whatever the user just chose since.
  const isFirstAttempt = !configRangeApplied;
  configRangeApplied = true;

  const params = dashboardQuery();

  // A user-initiated load (account/filter switch, tab change) dims the whole
  // page -- clear feedback that something changed. The periodic background
  // poll (see LIVE_PRICE_POLL_MS below) only lights a small dot in the
  // header instead: dimming the entire dashboard every 15 seconds for a
  // refresh nobody asked for reads as the app freezing, not updating.
  const liveDot = $('live-indicator');
  if (background) {
    if (liveDot) liveDot.hidden = false;
  } else {
    document.querySelector('.wrap').classList.add('loading');
  }
  let succeeded = false;
  try {
    const response = await fetch('/api/dashboard?' + params.toString());
    if (!response.ok) throw new Error('HTTP ' + response.status);
    const data = await response.json();
    if (generation !== loadGeneration) return; // superseded by a newer load()
    state.data = data;
    render();
    succeeded = true;
    const errHost = $('load-error');
    errHost.hidden = true;
    clear(errHost);
  } catch (error) {
    if (generation !== loadGeneration) return; // superseded by a newer load()
    // Deliberately NOT #notices: that div lives inside #tab-dashboard and is
    // invisible on every other tab, which would leave whatever account/filter
    // was loaded before this failed fetch on screen with no visible sign
    // anything is wrong (see the comment on #load-error in index.html).
    const host = $('load-error');
    clear(host);
    const notice = el('div', { class: 'notice err' });
    notice.appendChild(el('strong', {}, 'Could not load data'));
    notice.appendChild(document.createTextNode(' ' + error.message));
    host.appendChild(notice);
    host.hidden = false;
  } finally {
    if (generation === loadGeneration) {
      document.querySelector('.wrap').classList.remove('loading');
      if (liveDot) liveDot.hidden = true;
    }
  }

  // A named preset ("ytd", "1y", "year:2025", a bare day count) needs
  // meta.data_last_date to turn into concrete start/end dates, which only
  // exists after this first response -- so it gets recomputed once, here.
  // "all" and "custom" need no recompute: their start/end, already restored
  // into `state` at boot (see applyConfigSnapshot()), are exactly what the
  // fetch above just used, so reloading for them would just refetch the same
  // thing. Runs as a second, independent load() rather than inline, so the
  // loading-state add/remove above stays correctly paired for both fetches.
  const preset = state.datePreset;
  if (isFirstAttempt && succeeded && preset && preset !== 'all' && preset !== 'custom') {
    applyPreset(preset);
    $('preset').value = preset;
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

const TAB_IDS = ['dashboard', 'planner', 'realized', 'tradelog', 'tools'];

function renderTabs() {
  TAB_IDS.forEach((name) => {
    const panel = $(`tab-${name}`);
    if (panel) panel.hidden = name !== state.activeTab;
  });
  document
    .querySelectorAll('.tab')
    .forEach((btn) => btn.setAttribute('aria-selected', btn.dataset.tab === state.activeTab ? 'true' : 'false'));
}

function switchTab(name) {
  state.activeTab = name;
  queueConfigSave();
  renderTabs();
  if (name === 'tradelog') renderTradeLog();
  // Re-render immediately from whatever's already in state.data so the tab
  // never paints blank or mis-sized while the load() below is still in
  // flight: the SVG expiration calendar sizes itself to the container width,
  // which reads as 0 (-> a too-narrow fallback) while the tab is still
  // `hidden`, so the first paint during load() comes out wrong.
  try {
    if (name === 'planner' && state.data) renderPlanner();
    if (name === 'realized' && state.data) renderRealizedGains();
    if (name === 'tools' && state.data) renderCapitalProjection();
  } catch (error) {
    console.error('tab render failed:', error);
  }
  applySearchFilter();
  // Also refetch on every tab change, not just on an account/filter switch.
  // An account switch's own load() runs asynchronously, so a tab entered
  // while it's still in flight would otherwise render off whatever *older*
  // state.data happens to be sitting around above -- a different account's
  // data than the one now selected. load() re-renders every tab once the
  // fetch actually lands, so this guarantees the tab settles on data that
  // matches the current filters even if the render just above didn't.
  load();
  // A new top-level view -- don't leave the reader parked wherever the old
  // (often much taller) page was scrolled.
  const tabs = document.querySelector('nav.tabs');
  if (tabs) tabs.scrollIntoView({ block: 'start' });
}

/**
 * Planner tab: the forward-looking "what needs a look now" workspace. The three
 * decision cards (open positions, covered-call candidates, cash for CSPs) used
 * to live on the Dashboard; they render the same, just parented here. The
 * assignment-risk / workflow / expiration-calendar panels above and below them
 * are Planner-only.
 */
function renderPlanner() {
  renderEarningsInView();
  renderAssignmentRisk();
  renderWorkflow();
  renderOpenPositions();
  renderCcCandidates();
  renderCspCash();
  renderExpirationCalendar();
}

/**
 * One-line "who reports soon" strip at the top of the Planner. Tickers whose
 * next earnings date lands on or before an open leg's expiry are marked ⚡ --
 * that is the gap-risk the position can't sidestep.
 */
function renderEarningsInView() {
  const host = $('earnings-in-view');
  if (!host) return;
  const data = (state.data && state.data.earnings_in_view) || null;
  const rows = (data && data.tickers) || [];
  const soon = rows.filter((r) => r.days_to_earnings <= 7);
  if (!soon.length && !rows.some((r) => r.before_expiry)) {
    host.hidden = true;
    return;
  }
  host.hidden = false;
  host.textContent = '';

  const flagged = rows.filter((r) => r.before_expiry);
  const list = (flagged.length ? flagged : soon).slice(0, 12);
  const label = flagged.length ? 'Earnings before an open leg expires' : 'Earnings within 7 days';
  host.appendChild(el('span', { class: 'planner-earnings-label' }, `${label}: `));
  list.forEach((r, i) => {
    if (i) host.appendChild(document.createTextNode(' · '));
    const chip = el('span', {
      class: `planner-earnings-chip${r.before_expiry ? ' warn' : ''}`,
      title:
        `${r.ticker} reports ${dayLabel(r.earnings_date)} (${r.days_to_earnings}d)` +
        (r.soonest_leg_expiry ? `; soonest open leg expires ${dayLabel(r.soonest_leg_expiry)}` : ''),
    });
    chip.appendChild(document.createTextNode(`${r.before_expiry ? '⚡ ' : ''}${r.ticker} `));
    chip.appendChild(el('span', { class: 'muted' }, dayLabel(r.earnings_date)));
    host.appendChild(chip);
  });
}

// --- Planner panels (filled in by later phases; safe no-ops until then) ---

function renderAssignmentRisk() {
  const card = $('assignment-risk');
  if (!card) return;
  const data = (state.data && state.data.assignment_risk) || null;
  if (!data || !data.count) {
    card.hidden = true;
    clear(card); // the account/filters just switched to something with no ITM
    // risk -- clear the previous account's own figures/table out of the DOM
    // too, not just the `hidden` flag, so nothing stale is left to read if
    // `hidden` is ever defeated (see #assignment-risk's own `display:` rule
    // in styles.css, which needs its own `[hidden]` override for exactly
    // this reason).
    return;
  }
  card.hidden = false;
  renderAssignmentRiskBody(card, data);
}

function renderWorkflow() {
  const card = $('workflow-card');
  if (!card) return;
  const data = (state.data && state.data.workflow) || null;
  const buckets = data && data.buckets;
  const any = buckets && Object.values(buckets).some((b) => b && b.legs && b.legs.length);
  if (!any) {
    card.hidden = true;
    return;
  }
  card.hidden = false;
  renderWorkflowBody($('workflow-buckets'), data);
}

const EXP_CAL_GRAINS = ['day', 'week', 'month'];
const EXP_CAL_KEY = { day: 'days', week: 'weeks', month: 'months' };
const EXP_CAL_NEXT_LABEL = { day: 'Daily', week: 'Weekly', month: 'Monthly' };

// Geometry of the most recent renderExpirationCalendarBody() draw, read back
// by handleExpCalWheel -- the wheel listener is attached once (wireFilters())
// and outlives every individual render. Mirrors capitalProjectionGeom.
let expCalGeom = null;
const EXP_CAL_ZOOM_IN = 0.85;
const EXP_CAL_ZOOM_OUT = 1 / 0.85;
// Smallest window a scroll-zoom may narrow the date axis to, per grain --
// finer than this and the bars/labels have no room to mean anything.
const EXP_CAL_MIN_SPAN_MS = { day: 3 * DAY_MS, week: 14 * DAY_MS, month: 60 * DAY_MS };

/**
 * Scroll-to-zoom for the Expiration calendar's date axis: hovering the x-axis
 * tick labels (the bottom gutter) and scrolling narrows or restores the time
 * window, centred on the cursor's date -- same interaction as
 * handleCapitalProjectionWheel's x-axis case. Scrolling over the plot itself
 * (the bars) does nothing here, so the page still scrolls normally there.
 */
function handleExpCalWheel(event) {
  const geom = expCalGeom;
  if (!geom) return;
  const svg = $('chart-exp-calendar');
  const rect = svg.getBoundingClientRect();
  if (!rect.width || !rect.height) return;

  const svgX = ((event.clientX - rect.left) / rect.width) * geom.width;
  const svgY = ((event.clientY - rect.top) / rect.height) * geom.height;
  const { margin, plotWidth, plotHeight } = geom;
  const overXAxis = svgY > margin.top + plotHeight && svgX >= margin.left && svgX <= margin.left + plotWidth;
  if (!overXAxis) return;

  event.preventDefault();
  const zoomIn = event.deltaY < 0;
  const factor = zoomIn ? EXP_CAL_ZOOM_IN : EXP_CAL_ZOOM_OUT;

  const [lo, hi] = geom.xDomain;
  const span = hi - lo || 1;
  const t = lo + ((svgX - margin.left) / plotWidth) * span;
  let newLo = t - (t - lo) * factor;
  let newHi = t + (hi - t) * factor;
  const [natLo, natHi] = geom.naturalXDomain;
  if (!zoomIn && newHi - newLo >= natHi - natLo) {
    state.expCalZoom = null;
  } else {
    const minSpan = EXP_CAL_MIN_SPAN_MS[geom.grain] || EXP_CAL_MIN_SPAN_MS.day;
    if (newHi - newLo < minSpan) return;
    state.expCalZoom = [newLo, newHi];
  }
  renderExpirationCalendar();
}

function renderExpirationCalendar() {
  const card = $('expiration-calendar-card');
  if (!card) return;
  const data = (state.data && state.data.expiration_calendar) || null;
  const anyLegs = data && (data.days || []).length;
  if (!anyLegs) {
    card.hidden = true;
    return;
  }
  card.hidden = false;
  const btn = $('exp-cal-grain');
  if (btn) btn.textContent = EXP_CAL_NEXT_LABEL[state.expCalGrain] || 'Daily';
  renderExpirationCalendarBody(data);
}

function renderRealizedGains() {
  const card = $('tab-realized');
  if (!card) return;
  const data = (state.data && state.data.realized_gains) || null;
  const empty = $('realized-gains-empty');
  const body = $('realized-gains-body');
  const hasData = data && data.lots && data.lots.length;
  if (empty) empty.hidden = !!hasData;
  if (body) body.hidden = !hasData;
  const note = $('realized-gains-note');
  if (note) note.textContent = hasData ? data.notes || '' : '';
  if (hasData) renderRealizedGainsBody(data);
}

/* ------------------------------------------------------- Capital projection
 *
 * Tools tab. Left of the red Today marker, the chart plots the account's real
 * value (from Portfolio Positions snapshots, the same source the Net Worth &
 * Benchmark chart uses) -- not modeled, an actual report of what happened.
 * Right of Today it switches to a hypothetical "what if" model: today's
 * balance compounds monthly at a fixed effective rate; a dashed gross
 * (never-taxed) reference and the real account-value line's own continuation
 * (which pays capital gains tax every March on its Jan-Dec earnings from the
 * prior calendar year) both start from that same point.
 */

/**
 * r_m = (1 + annual ROI)^(1/12) - 1, applied monthly from `startDate` (real
 * "today") forward, to both a gross (never-taxed) and a net (taxed) running
 * balance. At the close of every real December the net line's own earnings
 * since the prior December are booked; that tax comes due 3 months later, in
 * March, and is paid out of the net balance then -- so a calendar year's
 * Jan/Feb still compound on the not-yet-taxed balance, exactly as the card's
 * hint list describes. Because the clock starts at today rather than a
 * January, the first calendar year is partial: its "earnings" only cover
 * today through December, not a full year. A tax rate is never negative
 * here: a down year owes nothing, but carries no credit forward either.
 */
function computeCapitalProjection({ balance, annualRoiPct, taxRatePct, years, startDate }) {
  const months = Math.max(1, Math.round(years * 12));
  const rm = Math.pow(1 + annualRoiPct / 100, 1 / 12) - 1;
  const taxRate = taxRatePct / 100;
  const pad = (n) => String(n).padStart(2, '0');
  const startYear = startDate.getFullYear();
  const startMonth = startDate.getMonth() + 1; // 1-12, the calendar month "today" falls in

  let gross = balance;
  let net = balance;
  let prevDecBalance = balance;
  let pendingTax = 0;
  let pendingDueMonth = null;
  const annualMap = new Map();
  const ensureYear = (y) => {
    if (!annualMap.has(y)) annualMap.set(y, { year: y, endGross: null, endNet: null, taxPaid: 0 });
    return annualMap.get(y);
  };

  // Month 0 = today itself, at its real date (not rounded to the 1st) so it
  // lines up exactly with the historical series and the vertical Today marker.
  const rows = [{ month: 0, iso: localIso(startDate.getTime()), gross, net, taxPaid: 0 }];

  for (let m = 1; m <= months; m += 1) {
    gross *= 1 + rm;
    net *= 1 + rm;
    const monthsSinceJan = startMonth - 1 + m;
    const calendarMonth = (monthsSinceJan % 12) + 1;
    const calendarYear = startYear + Math.floor(monthsSinceJan / 12);

    let taxPaid = 0;
    if (calendarMonth === 3 && pendingDueMonth === m) {
      taxPaid = pendingTax;
      net -= taxPaid;
      pendingTax = 0;
      pendingDueMonth = null;
      ensureYear(calendarYear).taxPaid = taxPaid;
    }
    if (calendarMonth === 12) {
      const earnings = net - prevDecBalance;
      pendingTax = Math.max(0, earnings) * taxRate;
      pendingDueMonth = m + 3;
      prevDecBalance = net;
      const y = ensureYear(calendarYear);
      y.endGross = gross;
      y.endNet = net;
    }

    rows.push({ month: m, iso: `${calendarYear}-${pad(calendarMonth)}-01`, gross, net, taxPaid });
  }

  const annual = [...annualMap.values()].sort((a, b) => a.year - b.year);
  return { rows, annual };
}

// Yearly x-axis for the projection chart -- plain calendar years (2026, 2027,
// ...) at each January, rather than reusing `timeAxis`'s raw day-of-month
// labels, which carry no year and get crowded/ambiguous over a span that now
// runs from years of real history through years of future projection.
const PROJECTION_YEAR_STEPS = [1, 2, 3, 5, 10, 15, 20, 25, 50];
function projectionAxis(group, chartRows, x, yBase, plotWidth, margin) {
  const first = chartRows[0];
  const last = chartRows[chartRows.length - 1];
  group.appendChild(
    svgEl('line', { class: 'axis-line', x1: x(first.iso), x2: x(last.iso), y1: yBase, y2: yBase })
  );

  const firstYear = parseDay(first.iso).getFullYear();
  const lastYear = parseDay(last.iso).getFullYear();
  const maxLabels = Math.max(2, Math.floor(plotWidth / 60));
  const totalYears = Math.max(1, lastYear - firstYear + 1);
  const step =
    PROJECTION_YEAR_STEPS.find((s) => Math.ceil(totalYears / s) <= maxLabels) ||
    PROJECTION_YEAR_STEPS[PROJECTION_YEAR_STEPS.length - 1];

  for (let yr = firstYear; yr <= lastYear; yr += 1) {
    if ((yr - firstYear) % step !== 0) continue;
    const xp = x(`${yr}-01-01`);
    if (xp < margin.left - 1 || xp > margin.left + plotWidth + 1) continue;
    group.appendChild(svgEl('text', { class: 'tick-label', x: xp, y: yBase + 16, 'text-anchor': 'middle' }, String(yr)));
  }
}

// Current pixel<->data mapping for the projection chart, refreshed on every
// draw and read back by `handleCapitalProjectionWheel` -- the wheel listener
// is attached once (in `wireCapitalProjection`) to the SVG element, which the
// per-render `clear()` never replaces, so it needs this out-of-band handle on
// whatever geometry the most recent render actually used.
let capitalProjectionGeom = null;

const PROJ_ZOOM_IN = 0.85;
const PROJ_ZOOM_OUT = 1 / 0.85;
const PROJ_MIN_X_SPAN_MS = 20 * DAY_MS;

// Below this pixel spacing a gridline tier reads as a smear, not a scale --
// e.g. a fine-grained minor tier across a zoomed-out span. Shared floor for
// both the dollar and time grids below; on top of picking an already-sane
// adaptive step, it's a last-resort guard for extreme spans/tiny charts.
const PROJ_MIN_GRID_PX = 4;

/**
 * Adaptive dollar gridlines for the projection chart, scaled to how far the
 * reader has zoomed the y-axis (see `handleCapitalProjectionWheel`): a
 * heavier, labeled "major" line at whatever round step (`niceStep`, the same
 * 1/2/2.5/5/10-ladder every other chart's y-axis uses) lands close to 5
 * across the visible span, plus a lighter, unlabeled "minor" line at a fifth
 * of that. Zoomed out over a $2M+ span that's $500k major / $100k minor;
 * zoomed into a $50k window it's $10k major / $2k minor -- always roughly
 * the same number of lines on screen, never a fixed-step pile-up.
 */
function drawCapitalProjectionYGrid(group, { margin, plotWidth, plotHeight, yMin, yMax, y }) {
  const majorStep = niceStep(yMax - yMin, 5);
  const minorStep = majorStep / 5;

  const drawTier = (step, cls, withLabel) => {
    const count = (yMax - yMin) / step;
    if (count <= 0 || plotHeight / count < PROJ_MIN_GRID_PX) return;
    for (let v = Math.ceil(yMin / step) * step; v <= yMax + 1e-6; v += step) {
      const yPos = y(v);
      group.appendChild(
        svgEl('line', { class: cls, x1: margin.left, x2: margin.left + plotWidth, y1: yPos, y2: yPos })
      );
      if (withLabel) {
        group.appendChild(
          svgEl('text', { class: 'tick-label', x: margin.left - 8, y: yPos + 3.5, 'text-anchor': 'end' }, compactMoney(v))
        );
      }
    }
  };
  drawTier(minorStep, 'grid-line-minor', false);
  drawTier(majorStep, 'grid-line', true);
}

// Ladder of "nice" calendar-aligned month steps -- every entry is a divisor
// or multiple of 12, so `absoluteMonth % step === 0` always lands on the same
// calendar month every year (quarters, half-years, whole years, ...) rather
// than an arbitrary offset from wherever the visible window happens to start.
const PROJ_MONTH_STEPS = [1, 2, 3, 6, 12, 24, 36, 60, 120, 240, 360, 600, 1200];

/**
 * Adaptive calendar gridlines for the projection chart's time axis: a
 * heavier "major" line at whichever step off `PROJ_MONTH_STEPS` keeps the
 * count near the target, and a lighter "minor" line one rung finer -- e.g.
 * zoomed out over decades that's decade / year lines, zoomed into a couple of
 * years it's year / quarter, and tight on a few months it's month-only (no
 * rung below "month" to subdivide into). Text labels stay `projectionAxis`'s
 * job; these are just the lines, density-guarded like the dollar grid above.
 */
function drawCapitalProjectionXGrid(group, { margin, plotWidth, plotHeight, x }, firstIso, lastIso) {
  const first = parseDay(firstIso);
  const last = parseDay(lastIso);
  const firstAbs = first.getFullYear() * 12 + first.getMonth();
  const lastAbs = last.getFullYear() * 12 + last.getMonth();
  const totalMonths = lastAbs - firstAbs + 1;

  const maxLabels = Math.max(2, Math.floor(plotWidth / 70));
  const majorStep =
    PROJ_MONTH_STEPS.find((s) => Math.ceil(totalMonths / s) <= maxLabels) || PROJ_MONTH_STEPS[PROJ_MONTH_STEPS.length - 1];
  const minorStep = PROJ_MONTH_STEPS[Math.max(0, PROJ_MONTH_STEPS.indexOf(majorStep) - 1)];

  const drawAt = (absMonth, cls) => {
    const iso = `${Math.floor(absMonth / 12)}-${String((absMonth % 12) + 1).padStart(2, '0')}-01`;
    const xp = x(iso);
    if (xp < margin.left - 1 || xp > margin.left + plotWidth + 1) return;
    group.appendChild(svgEl('line', { class: cls, x1: xp, x2: xp, y1: margin.top, y2: margin.top + plotHeight }));
  };

  const drawTier = (step, cls) => {
    const count = Math.ceil(totalMonths / step);
    if (plotWidth / count < PROJ_MIN_GRID_PX) return;
    for (let i = Math.ceil(firstAbs / step) * step; i <= lastAbs; i += step) drawAt(i, cls);
  };
  if (minorStep < majorStep) drawTier(minorStep, 'grid-line-minor');
  drawTier(majorStep, 'grid-line');
}

/**
 * `historicalRows` ({iso, value}, real dates strictly before today) are the
 * account's actual value from Positions snapshots -- see `renderCapitalProjection`.
 * They're merged with the projected `rows` (from `computeCapitalProjection`)
 * into one continuous account-value line: real readings, then today's own
 * balance, then the hypothetical after-tax projection -- one color, because
 * it is conceptually one line, just partly measured and partly modeled. The
 * gross (no-tax) reference only exists going forward, since "no tax" is a
 * hypothetical that doesn't apply to money that already changed hands.
 *
 * `state.projection.xZoom`/`yZoom` (each `[lo, hi]` or null) narrow the chart
 * to a scrolled-in view of an axis; see `handleCapitalProjectionWheel`. Marks
 * that would land outside the visible plot rect (a zoomed-in view no longer
 * containing every point) are clipped to it, since raw SVG coordinates only
 * clip at the element's own edge, not at an inner axis gutter.
 */
function drawCapitalProjection(rows, historicalRows, retirement) {
  const svg = $('chart-capital-projection');
  const legend = $('legend-capital-projection');
  clear(legend);
  if (!svg || !rows.length) return;

  const netColor = cssVar('--series-1');
  const grossColor = cssVar('--text-secondary');
  const taxColor = cssVar('--neg');
  const todayColor = cssVar('--critical');

  const chartRows = [
    ...historicalRows.map((h) => ({ iso: h.iso, net: h.value, gross: null, taxPaid: 0, historical: true, today: false })),
    ...rows.map((r) => ({ iso: r.iso, net: r.net, gross: r.gross, taxPaid: r.taxPaid, historical: false, today: r.month === 0 })),
  ];

  const margin = { top: 24, right: 12, bottom: 30, left: 62 };
  const width = chartWidth(svg);
  const height = 300;

  const proj = state.projection;
  const values = chartRows.flatMap((r) => [r.net, r.gross]).filter((v) => v !== null);
  const naturalYDomain = [0, Math.max(...values) * 1.1 || 1];
  const [yMin, yMax] = proj.yZoom || naturalYDomain;

  const { group, plotWidth, plotHeight, y } = frame(svg, { width, height, margin, yMin, yMax, grid: false });

  const times = chartRows.map((r) => parseDay(r.iso).getTime());
  const naturalXDomain = [times[0], times[times.length - 1]];
  const [tMin, tMax] = proj.xZoom || naturalXDomain;
  const x = (iso) =>
    margin.left + (tMax === tMin ? plotWidth / 2 : ((parseDay(iso).getTime() - tMin) / (tMax - tMin)) * plotWidth);

  capitalProjectionGeom = { margin, plotWidth, plotHeight, width, height, xDomain: [tMin, tMax], yDomain: [yMin, yMax], naturalXDomain, naturalYDomain };

  // Fixed-step grid, drawn before the data marks so it sits behind them:
  // dollars every $100k (heavier, labeled) and every $10k (lighter); years
  // every January (heavier) and every calendar month (lighter). The $10k and
  // month tiers self-skip once zoomed out far enough to pack them unreadably
  // tight -- see `drawCapitalProjectionYGrid`/`XGrid`.
  drawCapitalProjectionYGrid(group, { margin, plotWidth, plotHeight, yMin, yMax, y });
  drawCapitalProjectionXGrid(group, { margin, plotWidth, plotHeight, x }, localIso(tMin), localIso(tMax));

  // Marks are drawn into a clipped sub-group -- when zoomed in, a line
  // segment running to an off-view point would otherwise be computed well
  // outside the plot rect and could cross through the axis-label gutters.
  const clipId = 'capital-projection-clip';
  const defs = svgEl('defs');
  const clipPath = svgEl('clipPath', { id: clipId });
  clipPath.appendChild(svgEl('rect', { x: margin.left, y: margin.top, width: plotWidth, height: plotHeight }));
  defs.appendChild(clipPath);
  svg.appendChild(defs);
  const marks = svgEl('g', { 'clip-path': `url(#${clipId})` });
  group.appendChild(marks);

  // The account-value line: real readings up to today, then the projected
  // net (after-tax) balance.
  marks.appendChild(
    svgEl('path', {
      d: 'M' + chartRows.map((r) => `${x(r.iso)},${y(r.net)}`).join('L'),
      fill: 'none',
      stroke: netColor,
      'stroke-width': 2,
      'stroke-linejoin': 'round',
      'stroke-linecap': 'round',
    })
  );

  // The no-tax hypothetical, today onward only -- meaningless (and identical
  // to the net line) for an account where no tax applies in the first place.
  if (!retirement) {
    marks.appendChild(
      svgEl('path', {
        d: 'M' + rows.map((r) => `${x(r.iso)},${y(r.gross)}`).join('L'),
        fill: 'none',
        stroke: grossColor,
        'stroke-width': 2,
        'stroke-linejoin': 'round',
        'stroke-linecap': 'round',
        'stroke-dasharray': '5,4',
      })
    );
  }

  // A dot on every real reading (historical snapshot or today's own balance);
  // pure future months get no dot of their own.
  chartRows
    .filter((r) => r.historical || r.today)
    .forEach((r) => {
      marks.appendChild(svgEl('circle', { cx: x(r.iso), cy: y(r.net), r: 3.5, fill: netColor }));
    });

  // Today: the line separating real history from the hypothetical.
  const todayRow = chartRows.find((r) => r.today);
  if (todayRow) {
    const tx = x(todayRow.iso);
    marks.appendChild(
      svgEl('line', {
        x1: tx,
        x2: tx,
        y1: margin.top,
        y2: margin.top + plotHeight,
        stroke: todayColor,
        'stroke-width': 1.5,
        'stroke-dasharray': '3,3',
      })
    );
    marks.appendChild(
      svgEl('text', { x: tx + 5, y: margin.top - 8, fill: todayColor, 'font-weight': 600, 'font-size': 11 }, 'Today')
    );
  }

  // March tax-payment markers, on the net line, at the balance right after
  // that March's payment came out.
  const taxRows = chartRows.filter((r) => r.taxPaid > 0);
  taxRows.forEach((r) => {
    marks.appendChild(
      svgEl('circle', {
        cx: x(r.iso),
        cy: y(r.net),
        r: 5,
        fill: taxColor,
        stroke: cssVar('--surface-1'),
        'stroke-width': 1.5,
      })
    );
  });

  projectionAxis(group, chartRows, x, margin.top + plotHeight, plotWidth, margin);

  const netDot = group.appendChild(
    svgEl('circle', { r: 5, fill: netColor, stroke: cssVar('--surface-1'), 'stroke-width': 2, opacity: 0 })
  );
  const grossDot = group.appendChild(
    svgEl('circle', { r: 5, fill: grossColor, stroke: cssVar('--surface-1'), 'stroke-width': 2, opacity: 0 })
  );

  crosshairLayer(svg, group, { margin, plotWidth, plotHeight, width }, chartRows, (r) => x(r.iso), {
    label: 'Actual account value up to today, projected gross vs. net (after-tax) balance after.',
    onIndex: (index, at) => {
      const row = chartRows[index];
      netDot.setAttribute('cx', x(row.iso));
      netDot.setAttribute('cy', y(row.net));
      netDot.setAttribute('opacity', 1);
      const showGross = !retirement && row.gross !== null;
      grossDot.setAttribute('opacity', showGross ? 1 : 0);
      if (showGross) {
        grossDot.setAttribute('cx', x(row.iso));
        grossDot.setAttribute('cy', y(row.gross));
      }
      const tooltipRows = [
        {
          label: row.historical ? 'Actual account value' : retirement ? 'Account balance (no tax)' : 'Net balance (after tax)',
          value: money(row.net),
          color: netColor,
        },
      ];
      if (showGross) tooltipRows.push({ label: 'Gross balance (no tax)', value: money(row.gross), color: grossColor });
      if (row.taxPaid > 0) tooltipRows.push({ label: 'Tax paid this March', value: money(row.taxPaid, { cents: true }), color: taxColor });
      showTooltip(
        at,
        row.today ? 'Today' : longDate(row.iso),
        tooltipRows,
        row.taxPaid > 0
          ? formula([
              'Tax paid = Capital gains rate × Prior calendar year\'s net earnings',
              `= ${money(row.taxPaid, { cents: true })}`,
            ])
          : null
      );
    },
    onLeave: () => {
      netDot.setAttribute('opacity', 0);
      grossDot.setAttribute('opacity', 0);
    },
  });

  const legendItem = (color, text, { dashed = false, dot = false } = {}) => {
    const item = el('span');
    const swatch = el('i', dot ? {} : { class: 'line' });
    swatch.style.background = color;
    if (dashed) swatch.style.opacity = '0.6';
    if (dot) swatch.style.borderRadius = '50%';
    item.appendChild(swatch);
    item.appendChild(document.createTextNode(text));
    legend.appendChild(item);
  };
  legendItem(netColor, 'Account value (actual to date, projected after)');
  if (!retirement) legendItem(grossColor, 'Gross balance (no tax, hypothetical)', { dashed: true });
  legendItem(todayColor, 'Today');
  if (taxRows.length) legendItem(taxColor, 'March tax payment', { dot: true });

  const zoomResetBtn = $('proj-zoom-reset');
  if (zoomResetBtn) zoomResetBtn.hidden = !proj.xZoom && !proj.yZoom;

  buildTable(
    'capital-projection-table',
    ['Month', 'Gross balance', 'Net balance', 'Tax paid'],
    rows.map((r) => [
      r.month === 0 ? 'Today' : longDate(r.iso),
      money(r.gross),
      money(r.net),
      r.taxPaid > 0 ? money(r.taxPaid, { cents: true }) : '—',
    ])
  );
}

function renderCapitalProjectionSummary(annual) {
  buildTable(
    'capital-projection-summary',
    ['Year', 'Ending gross balance', 'Ending net balance', 'Tax paid (that March)'],
    annual.map((y) => [
      String(y.year),
      y.endGross === null ? '—' : money(y.endGross),
      y.endNet === null ? '—' : money(y.endNet),
      y.taxPaid > 0 ? money(y.taxPaid, { cents: true }) : '—',
    ])
  );
}

/**
 * Best-effort only: there is no structured "this account is tax-advantaged"
 * field anywhere in the data model (see wheel/accounts.py), just the display
 * name the reader themselves gave the account/folder. "Combined" mixes
 * accounts that may not all be retirement, so it never counts as one even if
 * every account happens to match today -- only a single selected account can.
 */
function currentAccountIsRetirement() {
  if (state.account === COMBINED_ACCOUNT_ID) return false;
  const label = accountLabelsById[state.account] || '';
  return /\b(ira|401k|403b|457b|roth|retirement)\b/i.test(label);
}

function renderCapitalProjection() {
  const card = $('capital-projection-card');
  if (!card) return;
  const proj = state.projection;
  const portfolio = (state.data && state.data.portfolio) || {};
  const netWorth = state.data && state.data.net_worth;
  const totals = netWorth && netWorth.available ? netWorth.combined || netWorth : null;
  const totalValue = totals && totals.total_value !== null && totals.total_value !== undefined ? totals.total_value : null;
  const avgCapital = portfolio.avg_capital !== null && portfolio.avg_capital !== undefined ? portfolio.avg_capital : null;

  const balanceSourceSelect = $('proj-balance-source');
  const balanceInput = $('proj-balance');
  const balanceNote = $('proj-balance-note');
  const roiSelect = $('proj-roi-source');
  const manualInput = $('proj-roi-manual');
  const roiNote = $('proj-roi-note');
  const taxInput = $('proj-tax-rate');
  const taxNote = $('proj-tax-note');
  const yearsInput = $('proj-years');

  if (balanceSourceSelect) balanceSourceSelect.value = proj.balanceSource;
  if (yearsInput && document.activeElement !== yearsInput) yearsInput.value = String(proj.years);

  // Beginning balance: manual entry, or auto-tracking either the account's
  // full current value or its time-weighted average capital (same figure the
  // Annualized Wheel ROC tile divides by) -- same auto/manual pattern as the
  // ROI source below.
  const autoBalance =
    proj.balanceSource === 'full_value' ? totalValue : proj.balanceSource === 'avg_capital' ? avgCapital : null;
  const usingAutoBalance = proj.balanceSource !== 'manual';
  const autoBalanceAvailable = autoBalance !== null && autoBalance !== undefined;
  const effectiveBalance = usingAutoBalance && autoBalanceAvailable ? autoBalance : proj.manualBalance;

  if (balanceInput) {
    balanceInput.disabled = usingAutoBalance && autoBalanceAvailable;
    if (document.activeElement !== balanceInput) {
      balanceInput.value = usingAutoBalance && autoBalanceAvailable ? String(Math.round(effectiveBalance)) : String(proj.manualBalance);
    }
  }
  if (balanceNote) {
    balanceNote.textContent =
      usingAutoBalance && !autoBalanceAvailable
        ? `No ${proj.balanceSource === 'full_value' ? 'account value' : 'time-weighted avg capital'} yet for this filter, using the manual entry instead.`
        : '';
  }

  const autoPct =
    proj.roiSource === 'wheel_roc'
      ? portfolio.annualized_wheel_roc_pct
      : proj.roiSource === 'active_wheel_roc'
      ? portfolio.annualized_active_wheel_roc_pct
      : null;
  const usingAuto = proj.roiSource !== 'manual';
  const autoAvailable = autoPct !== null && autoPct !== undefined;
  const effectiveRoiPct = usingAuto && autoAvailable ? autoPct : proj.manualRoiPct;

  // Auto mode with a live figure: the box itself shows that figure (read-only,
  // it tracks the tile) rather than a separate line of text repeating it.
  if (manualInput) {
    manualInput.disabled = usingAuto && autoAvailable;
    if (document.activeElement !== manualInput) {
      manualInput.value = usingAuto && autoAvailable ? (Math.round(effectiveRoiPct * 100) / 100).toString() : String(proj.manualRoiPct);
    }
  }
  if (roiNote) {
    roiNote.textContent =
      usingAuto && !autoAvailable
        ? `No Annualized ${proj.roiSource === 'wheel_roc' ? 'Wheel' : 'Active Wheel'} ROC yet for this filter, using the manual entry instead.`
        : '';
  }

  // A retirement account (best guess from its name -- see
  // `currentAccountIsRetirement`) owes no annual capital gains tax, so the
  // configured rate is overridden to 0 regardless of what the field shows.
  const retirement = currentAccountIsRetirement();
  const effectiveTaxRatePct = retirement ? 0 : proj.taxRatePct;
  if (taxInput) {
    taxInput.disabled = retirement;
    if (!retirement && document.activeElement !== taxInput) taxInput.value = String(proj.taxRatePct);
  }
  if (taxNote) {
    taxNote.textContent = retirement
      ? `Not applied: "${accountLabelsById[state.account] || ''}" reads as a retirement account, so gains inside it are not taxed annually.`
      : '';
  }

  const startDate = new Date();
  const todayIso = localIso(startDate.getTime());
  const benchmark = state.data && state.data.benchmark;
  // Same source as the Net Worth & Benchmark chart's "Actual account value"
  // line: real Positions-snapshot readings, not modeled. That chart itself
  // ignores every filter (the backend builds it once from the account's
  // whole snapshot history, before any date range is ever applied) -- this
  // one instead honors the dashboard's selected date range's start, since a
  // reader narrowing to "last year" or a specific year expects the
  // projection's own history to begin there too, not further back. Ticker/
  // status stay ignored: neither means anything against a whole-account
  // value line.
  const historicalRows =
    benchmark && benchmark.available
      ? (benchmark.series || [])
          .filter(
            (p) =>
              p.actual_value !== null &&
              p.actual_value !== undefined &&
              p.as_of < todayIso &&
              (!state.start || p.as_of >= state.start)
          )
          .map((p) => ({ iso: p.as_of, value: p.actual_value }))
      : [];

  const { rows, annual } = computeCapitalProjection({
    balance: effectiveBalance,
    annualRoiPct: effectiveRoiPct,
    taxRatePct: effectiveTaxRatePct,
    years: proj.years,
    startDate,
  });

  drawCapitalProjection(rows, historicalRows, retirement);
  renderCapitalProjectionSummary(annual);
}

/**
 * Any of the six projection inputs (balance source/value, ROI source/value,
 * tax rate, duration) can reshape the chart's whole time/dollar range, so a
 * zoom window framed for the old numbers may no longer make sense against the
 * new ones -- back to "fit the data" on every parameter change. Zooming
 * itself (the wheel handler) is the only thing that ever sets a zoom, so it
 * never fights this.
 */
function resetProjectionZoom() {
  state.projection.xZoom = null;
  state.projection.yZoom = null;
}

function wireCapitalProjection() {
  const proj = state.projection;

  const balanceSourceSelect = $('proj-balance-source');
  if (balanceSourceSelect) {
    balanceSourceSelect.addEventListener('change', () => {
      proj.balanceSource = balanceSourceSelect.value;
      queueConfigSave();
      resetProjectionZoom();
      renderCapitalProjection();
    });
  }

  const balanceInput = $('proj-balance');
  if (balanceInput) {
    balanceInput.addEventListener('change', () => {
      const n = parseFloat(balanceInput.value);
      if (!Number.isFinite(n) || n < 0) return;
      proj.manualBalance = n;
      queueConfigSave();
      resetProjectionZoom();
      renderCapitalProjection();
    });
  }

  const roiSelect = $('proj-roi-source');
  if (roiSelect) {
    roiSelect.addEventListener('change', () => {
      proj.roiSource = roiSelect.value;
      queueConfigSave();
      resetProjectionZoom();
      renderCapitalProjection();
    });
  }

  const manualInput = $('proj-roi-manual');
  if (manualInput) {
    manualInput.addEventListener('change', () => {
      const n = parseFloat(manualInput.value);
      if (!Number.isFinite(n)) return;
      proj.manualRoiPct = n;
      queueConfigSave();
      resetProjectionZoom();
      renderCapitalProjection();
    });
  }

  const taxInput = $('proj-tax-rate');
  if (taxInput) {
    taxInput.addEventListener('change', () => {
      const n = parseFloat(taxInput.value);
      if (!Number.isFinite(n)) return;
      const clamped = Math.min(100, Math.max(0, n));
      proj.taxRatePct = clamped;
      queueConfigSave();
      resetProjectionZoom();
      renderCapitalProjection();
    });
  }

  const yearsInput = $('proj-years');
  if (yearsInput) {
    yearsInput.addEventListener('change', () => {
      const n = parseFloat(yearsInput.value);
      if (!Number.isFinite(n)) return;
      const clamped = Math.min(40, Math.max(1, Math.round(n)));
      proj.years = clamped;
      queueConfigSave();
      resetProjectionZoom();
      renderCapitalProjection();
    });
  }

  const zoomResetBtn = $('proj-zoom-reset');
  if (zoomResetBtn) {
    zoomResetBtn.addEventListener('click', () => {
      resetProjectionZoom();
      renderCapitalProjection();
    });
  }

  const projChart = $('chart-capital-projection');
  if (projChart) projChart.addEventListener('wheel', handleCapitalProjectionWheel, { passive: false });
}

/**
 * Scroll-to-zoom for the projection chart's axes: hovering the y-axis tick
 * labels (the left gutter) and scrolling trims or restores how much of the
 * dollar axis's top is shown -- the bottom is always $0, so a balance chart
 * never has reason to zoom to a floating window away from it. Hovering the
 * x-axis tick labels (the bottom gutter) zooms the time axis around the
 * cursor's date instead, in both directions. Scrolling over the plot itself
 * does nothing here (and isn't intercepted, so the page still scrolls
 * normally). Reads `capitalProjectionGeom`, the geometry the most recent
 * `drawCapitalProjection` call used, since this listener is attached once
 * and outlives every individual render.
 */
function handleCapitalProjectionWheel(event) {
  const geom = capitalProjectionGeom;
  if (!geom) return;
  const svg = $('chart-capital-projection');
  const rect = svg.getBoundingClientRect();
  if (!rect.width || !rect.height) return;

  const svgX = ((event.clientX - rect.left) / rect.width) * geom.width;
  const svgY = ((event.clientY - rect.top) / rect.height) * geom.height;
  const { margin, plotWidth, plotHeight } = geom;

  const overYAxis = svgX < margin.left && svgY >= margin.top && svgY <= margin.top + plotHeight;
  const overXAxis = svgY > margin.top + plotHeight && svgX >= margin.left && svgX <= margin.left + plotWidth;
  if (!overYAxis && !overXAxis) return;

  event.preventDefault();
  const zoomIn = event.deltaY < 0;
  const factor = zoomIn ? PROJ_ZOOM_IN : PROJ_ZOOM_OUT;
  const proj = state.projection;

  if (overYAxis) {
    // The dollar axis always starts at $0 -- zooming only ever trims or
    // restores the top, regardless of where on the axis the cursor sits.
    const hi = geom.yDomain[1];
    const natHi = geom.naturalYDomain[1];
    const newHi = hi * factor;
    if (!zoomIn && newHi >= natHi) {
      proj.yZoom = null;
    } else {
      if (newHi < Math.max(50, natHi * 0.02)) return;
      proj.yZoom = [0, newHi];
    }
  } else {
    const [lo, hi] = geom.xDomain;
    const span = hi - lo || 1;
    const t = lo + ((svgX - margin.left) / plotWidth) * span;
    let newLo = t - (t - lo) * factor;
    let newHi = t + (hi - t) * factor;
    const [natLo, natHi] = geom.naturalXDomain;
    if (!zoomIn && newHi - newLo >= natHi - natLo) {
      proj.xZoom = null;
    } else {
      if (newHi - newLo < PROJ_MIN_X_SPAN_MS) return;
      proj.xZoom = [newLo, newHi];
    }
  }
  renderCapitalProjection();
}

/**
 * Assignment risk -- the mirror of the Cash-for-CSP card. Three stacked figures
 * (obligation / available / shortfall) framed as a solvency check, then a row
 * per in-the-money short leg.
 */
function renderAssignmentRiskBody(card, data) {
  clear(card);
  card.classList.toggle('critical', (data.potential_shortfall || 0) > 0);

  const puts = data.itm_puts || [];
  const calls = data.itm_calls || [];
  const headBits = [];
  if (puts.length) headBits.push(`${puts.length} CSP${puts.length > 1 ? 's' : ''} in the money`);
  if (calls.length) headBits.push(`${calls.length} covered call${calls.length > 1 ? 's' : ''} in the money`);
  card.appendChild(el('div', { class: 'ar-head' }, headBits.join(' · ') || 'Short legs in the money'));

  const figures = el('div', { class: 'ar-figures' });
  const fig = (k, v) => {
    const box = el('div', { class: 'ar-fig' });
    box.appendChild(el('span', { class: 'k' }, k));
    box.appendChild(el('span', { class: 'v' }, v));
    return box;
  };
  figures.appendChild(fig('Assignment obligation', money(data.assignment_obligation)));
  figures.appendChild(
    fig('Cash available', data.cash_available === null ? 'snapshot needed' : money(data.cash_available))
  );
  figures.appendChild(
    fig(
      'Potential shortfall',
      data.potential_shortfall === null ? '—' : money(data.potential_shortfall)
    )
  );
  if (data.soonest_itm_expiry) figures.appendChild(fig('Soonest ITM expiry', longDate(data.soonest_itm_expiry)));
  figures.title = formula([
    'Assignment obligation = total strike value of the ITM puts',
    'Cash available = full cash balance (already includes put collateral)',
    'Potential shortfall = overdraw if all ITM puts assign at once (normally $0)',
  ]);
  card.appendChild(figures);

  if (!puts.length && !calls.length) return;
  const table = el('table');
  const thead = el('thead');
  thead.appendChild(
    rowOf('th', ['Leg', 'Strike', 'Qty', 'Expiry', 'ITM %', 'If assigned', 'Min Roll Premium'])
  );
  table.appendChild(thead);
  const tbody = el('tbody');
  puts.forEach((r) =>
    tbody.appendChild(
      rowOf('td', [
        `${r.underlying} CSP`,
        money(r.strike, { cents: true }),
        r.contracts,
        longDate(r.expiration),
        r.moneyness_pct === null ? '—' : `${Math.abs(r.moneyness_pct).toFixed(1)}%`,
        `${money(r.obligation)} cash`,
        minRollPremiumCell(r),
      ])
    )
  );
  calls.forEach((r) =>
    tbody.appendChild(
      rowOf('td', [
        `${r.underlying} CC${r.shares_tracked ? '' : ' ~'}`,
        money(r.strike, { cents: true }),
        r.contracts,
        longDate(r.expiration),
        r.moneyness_pct === null ? '—' : `${Math.abs(r.moneyness_pct).toFixed(1)}%`,
        `${r.shares_at_risk_of_call} sh called`,
        minRollPremiumCell(r),
      ])
    )
  );
  table.appendChild(tbody);
  card.appendChild(table);
}

/** Each value is either a plain string/number, or `{ text, title }` for a
 * cell that also carries a hover formula. */
function rowOf(cell, values) {
  const tr = el('tr');
  values.forEach((v) => {
    if (v && typeof v === 'object') {
      tr.appendChild(el(cell, v.title ? { title: v.title } : {}, v.text == null ? '' : String(v.text)));
    } else {
      tr.appendChild(el(cell, {}, v == null ? '' : String(v)));
    }
  });
  return tr;
}

/** The Min Roll Premium formula tooltip, shared by the Recommended
 * buy-to-close and Assignment risk tables -- see _min_roll_premium /
 * _roll_floor_date in wheel/api.py. `null` when the row carries nothing to
 * explain. The target date's weekday is read off the date itself rather than
 * assumed "Friday": a monthly-only ticker's own next expiration (used once
 * it's later than next week's Friday) is priced against real listed dates,
 * which are Fridays in practice but not guaranteed to be. */
function rollPremiumFormula(r) {
  if (r.min_roll_premium == null || r.strike == null || r.roll_dte == null) return null;
  const dow = parseDay(r.roll_target_date).toLocaleDateString('en-US', { weekday: 'short' });
  return formula([
    `Min credit/share to roll to ${r.roll_dte} DTE (${dow}, ${longDate(r.roll_target_date)}):`,
    'Min Roll Premium = Strike × (DTE ÷ 365) × 30% target annual yield',
    `= ${money(r.strike, { cents: true })} × (${r.roll_dte} ÷ 365) × 30%`,
    `= ${money(r.min_roll_premium, { cents: true })}`,
  ]);
}

/** Rendered as a plain `{text, title}` cell for the Assignment risk table's
 * `rowOf` -- see `rollPremiumFormula` for the shared math. */
function minRollPremiumCell(r) {
  const title = rollPremiumFormula(r);
  if (!title) return '—';
  return { text: money(r.min_roll_premium, { cents: true }), title };
}

/** Workflow buckets -- four columns of flagged legs, each with its reason. */
function renderWorkflowBody(host, data) {
  clear(host);
  const order = ['attention', 'take_profit_candidate', 'evaluate', 'working'];
  for (const name of order) {
    const bucket = data.buckets[name];
    if (!bucket) continue;
    const col = el('div', { class: `workflow-bucket ${name}` });
    const h = el('h4');
    h.appendChild(document.createTextNode(bucket.label));
    h.appendChild(
      el('span', { class: 'wf-sub' }, `${bucket.count} · ${compactMoney(bucket.capital)}`)
    );
    col.appendChild(h);
    const ul = el('ul');
    bucket.legs.forEach((leg) => {
      const li = el('li');
      const head = el('span', { class: 'wf-leg' });
      head.appendChild(
        leg.cycle_id ? wheelLink(leg.cycle_id, leg.underlying) : document.createTextNode(leg.underlying || '—')
      );
      head.appendChild(
        document.createTextNode(
          ` ${leg.type} ${leg.strike != null ? money(leg.strike, { cents: true }) : ''}` +
            (leg.expiration ? ` · ${longDate(leg.expiration)}` : '')
        )
      );
      li.appendChild(head);
      li.appendChild(el('span', { class: 'wf-reason' }, leg.reason));
      ul.appendChild(li);
    });
    if (!bucket.legs.length) ul.appendChild(el('li', { class: 'wf-reason' }, 'nothing here'));
    col.appendChild(ul);
    host.appendChild(col);
  }
}

const MONTH_ABBR = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];

/**
 * Expiration calendar as a time-axis chart: today at the left edge, every open
 * leg's expiry placed at its real date so the horizontal distance IS "how far
 * ahead". The whole span fits one view (no scroll); bars are thin. The x-axis
 * carries a full calendar for the selected scale — a tick for every day / week /
 * month across the span, drawn whether or not a bar sits there.
 */
function renderExpirationCalendarBody(data) {
  const svg = $('chart-exp-calendar');
  const legend = $('legend-exp-calendar');
  if (!svg) return;
  clear(legend);
  const grain = state.expCalGrain || 'day';
  const today = data.as_of || localIso(Date.now());
  let buckets = data[EXP_CAL_KEY[grain]] || [];
  // How far back each view reaches: the daily view is a tight two weeks; the
  // weekly view is capped at six months (no need for more there); the monthly
  // view shows the whole span. Drop older buckets so stale bars don't pile up
  // against the left edge.
  const BACK_LIMIT_DAYS = { day: 14, week: 182 };
  const backDays = BACK_LIMIT_DAYS[grain];
  const backCutoffMs = backDays ? parseDay(today).getTime() - backDays * DAY_MS : null;
  if (backCutoffMs !== null) {
    const cutoffIso = localIso(backCutoffMs);
    buckets = buckets.filter((b) => b.start >= cutoffIso);
  }
  if (!buckets.length) {
    clear(svg);
    return;
  }

  // A bucket occupies a *slot* -- the whole week or month it stands for (a day
  // is its own slot). Bars, and the "today" marker, sit at the slot's centre so
  // "today" reads as "we're partway through this month / week", not pinned to a
  // boundary line.
  const slotStartMs = (iso) => {
    const d = parseDay(iso);
    if (grain === 'week') return d.getTime() - ((d.getDay() + 6) % 7) * DAY_MS; // back to Monday
    if (grain === 'month') return new Date(d.getFullYear(), d.getMonth(), 1).getTime();
    return d.getTime();
  };
  const slotEndMs = (iso) => {
    const d = parseDay(iso);
    if (grain === 'week') return slotStartMs(iso) + 7 * DAY_MS;
    if (grain === 'month') return new Date(d.getFullYear(), d.getMonth() + 1, 1).getTime();
    return d.getTime() + DAY_MS;
  };

  const firstBucket = buckets[0].start;
  const lastBucket = buckets[buckets.length - 1].start;
  const leftIso = firstBucket < today ? firstBucket : today; // a stale leg can predate today
  let t0Raw = Math.min(slotStartMs(leftIso), slotStartMs(today));
  if (backCutoffMs !== null) t0Raw = Math.max(t0Raw, backCutoffMs);
  // Daily view: exactly two weeks of run-up before today. Weekly: open on the
  // 1st of the earliest month in view, but never earlier than the six-month
  // cap. Monthly: open on the 1st of the earliest month in view, uncapped.
  let natT0;
  if (grain === 'day') {
    natT0 = parseDay(today).getTime() - 14 * DAY_MS;
  } else {
    const t0d = new Date(t0Raw);
    natT0 = new Date(t0d.getFullYear(), t0d.getMonth(), 1).getTime();
  }
  const lastEnd = slotEndMs(lastBucket);
  // Always look a minimum distance forward from today -- 14 days (daily), 6
  // weeks (weekly), 2 months (monthly) -- even when the last real expiry is
  // nearer than that. A further-out expiry still extends the axis past it.
  const todayMs = parseDay(today).getTime();
  const tDay = new Date(todayMs);
  const minForwardMs =
    grain === 'day'
      ? todayMs + 14 * DAY_MS
      : grain === 'week'
        ? todayMs + 42 * DAY_MS
        : new Date(tDay.getFullYear(), tDay.getMonth() + 3, 1).getTime();
  let natT1 = Math.max(lastEnd, minForwardMs);
  if (grain === 'day') natT1 += Math.max((natT1 - natT0) * 0.03, 1.5 * DAY_MS);

  // state.expCalZoom (the From/To inputs, or scroll-to-zoom on the date axis
  // -- see handleExpCalWheel) narrows the view to a [lo, hi] window; null
  // means "fit the data," the natural range just computed above. Carries
  // over unchanged when the grain toggle changes the bucket size -- Reset is
  // the only thing that clears it back to null.
  const [t0, tEnd] = state.expCalZoom || [natT0, natT1];

  // A custom From/To (or scroll-zoom) range can now be narrower than the
  // full dataset, unlike the natural range above, which by construction
  // always spans every bucket. Everything that follows -- the y-axis scale,
  // the legend's loss/long flags, and the bars themselves -- reads off this
  // windowed set so a bucket outside the chosen range is dropped, not
  // squeezed into view at the edge.
  const windowedBuckets = buckets.filter((b) => {
    const s = slotStartMs(b.start);
    const e = slotEndMs(b.start);
    return e > t0 && s < tEnd;
  });

  // One STACKED bar per expiry bucket -- a graphical read of the Open option
  // positions table. The bar spans the whole day / week / month; its height is
  // the total capital expiring then, split into a segment per position (height
  // proportional to that position's capital). Colour = leg family; a red edge =
  // strike on the wrong side of break-even; a ▸ notch = in the money now.
  // Same colour vocabulary as the Open option positions table: shorts blue/green,
  // long legs amber ("premium paid, not received").
  const FAMILY_COLOR = { csp: '--pos', cc: '--good', long: '--warning' };
  const allPositions = windowedBuckets.flatMap((b) => b.positions || []);
  const anyLoss = allPositions.some((p) => p.at_a_loss);
  const anyLong = allPositions.some((p) => p.family === 'long');

  const margin = { top: 34, right: 14, bottom: 34, left: 54 };
  const width = chartWidth(svg);
  const height = 250;
  const bucketTotal = (b) => (b.positions || []).reduce((s, p) => s + (p.capital || 0), 0);
  const yMax = Math.max(1, ...windowedBuckets.map(bucketTotal)) * 1.12;
  const { group, plotWidth, plotHeight, y } = frame(svg, {
    width, height, margin, yMin: 0, yMax,
  });
  const zeroY = y(0);
  const xMs = (ms) => margin.left + ((ms - t0) / (tEnd - t0)) * plotWidth;

  // Geometry + natural (un-zoomed) domain for handleExpCalWheel, which reads
  // this out-of-band since the wheel listener is attached once (wireFilters())
  // and outlives any individual render.
  expCalGeom = { margin, plotWidth, plotHeight, width, height, grain, xDomain: [t0, tEnd], naturalXDomain: [natT0, natT1] };

  // Keep the From / To inputs showing the range actually on screen, however
  // it got there -- typed in directly, scroll-zoomed, or (on Reset / a grain
  // switch) the full natural range. tEnd is an exclusive boundary (the start
  // of the day *after* the last one shown), so back it off by a tick before
  // flooring to a calendar date for the End field. Skip whichever field the
  // reader is actively editing -- this card re-renders every 15s on the
  // background live-price poll (see LIVE_PRICE_POLL_MS), which would
  // otherwise overwrite a date mid-keystroke and make the field untypable.
  const startInput = $('exp-cal-start');
  const endInput = $('exp-cal-end');
  if (startInput && document.activeElement !== startInput) startInput.value = localIso(t0);
  if (endInput && document.activeElement !== endInput) endInput.value = localIso(tEnd - 1);

  // ---- calendar axis: a boundary line per slot, label centred in the slot ---
  const boundaries = []; // ms at each slot boundary from t0..tEnd
  if (grain === 'month') {
    const d = new Date(t0);
    for (let ms = new Date(d.getFullYear(), d.getMonth(), 1).getTime(); ms <= tEnd + 1; ) {
      boundaries.push(ms);
      const b = new Date(ms);
      ms = new Date(b.getFullYear(), b.getMonth() + 1, 1).getTime();
    }
  } else {
    const step = grain === 'week' ? 7 * DAY_MS : DAY_MS;
    for (let ms = t0; ms <= tEnd + 1; ms += step) boundaries.push(ms);
  }
  const minLabelPx = grain === 'day' ? 46 : 52;
  const slotCount = Math.max(boundaries.length - 1, 1);
  const labelEvery = Math.max(1, Math.ceil((slotCount * minLabelPx) / Math.max(plotWidth, 1)));

  boundaries.forEach((ms, i) => {
    const labelled = i % labelEvery === 0 && boundaries[i + 1] !== undefined;
    const bx = xMs(ms);
    // Only draw a gridline where there's a label (or for month grain, at every
    // month) -- a line every single day just clutters the axis.
    if (
      (labelled || grain === 'month') &&
      bx >= margin.left - 0.5 &&
      bx <= margin.left + plotWidth + 0.5
    ) {
      group.appendChild(
        svgEl('line', {
          class: 'grid-line',
          x1: bx, x2: bx, y1: margin.top, y2: margin.top + plotHeight,
          opacity: 0.35,
        })
      );
    }
    if (!labelled) return;
    const next = boundaries[i + 1];
    // Label the slot that starts here, placed at its centre.
    const cx = xMs((ms + next) / 2);
    if (cx < margin.left || cx > margin.left + plotWidth) return;
    const label =
      grain === 'month'
        ? `${MONTH_ABBR[new Date(ms).getMonth()]} ${String(new Date(ms).getFullYear()).slice(2)}`
        : dayLabel(localIso(ms));
    // Below the rotated ticker labels, at the very bottom of the margin.
    group.appendChild(
      svgEl('text', { class: 'tick-label', x: cx, y: margin.top + plotHeight + margin.bottom - 4, 'text-anchor': 'middle' }, label)
    );
  });
  group.appendChild(
    svgEl('line', {
      class: 'axis-line',
      x1: margin.left, x2: margin.left + plotWidth,
      y1: margin.top + plotHeight, y2: margin.top + plotHeight,
    })
  );

  // ---- one stacked bar per bucket, a segment per position ---------------
  const bucketLabel = (b) => {
    if (grain === 'month') return monthLabel(b.month);
    const d = parseDay(b.start);
    return grain === 'week'
      ? `wk ${MONTH_ABBR[d.getMonth()]} ${d.getDate()}`
      : `${MONTH_ABBR[d.getMonth()]} ${d.getDate()}`;
  };
  const SEG_GAP = 2.5;       // visible separation between stacked segments
  const MIN_SEG_H = 13;      // every segment is at least this tall (label fits)
  const SLOT_PAD = grain === 'day' ? 1 : 4;  // hairline gap between neighbouring slots
  const MIN_BAR_W = 14;      // a thin daily slot still shows a real bar

  const drawable = windowedBuckets.filter((b) => (b.positions || []).length);

  // Each bar FILLS the slot it stands for -- the whole September column in
  // monthly, the whole week in weekly, that one day in daily -- so bar width is
  // literally the time scale. A daily slot too thin to see is bumped to a
  // minimum and (only then) nudged clear of its neighbours.
  const bars = drawable.map((b) => {
    const x0 = xMs(slotStartMs(b.start));
    const x1 = xMs(slotEndMs(b.start));
    let w = x1 - x0 - SLOT_PAD;
    let x = x0 + SLOT_PAD / 2;
    if (w < MIN_BAR_W) {
      w = MIN_BAR_W;
      x = (x0 + x1) / 2 - w / 2;
    }
    return { b, x, w };
  });
  // Only the bumped-to-minimum (thin daily) bars can overlap; spread them.
  for (let i = 1; i < bars.length; i++) {
    if (bars[i].x < bars[i - 1].x + bars[i - 1].w + 2) {
      bars[i].x = bars[i - 1].x + bars[i - 1].w + 2;
    }
  }
  const runEnd = bars.length ? bars[bars.length - 1].x + bars[bars.length - 1].w : 0;
  const over = runEnd - (margin.left + plotWidth);
  if (over > 0) bars.forEach((bb) => (bb.x = Math.max(margin.left, bb.x - over)));

  // Segment heights: proportional to capital, floored so each carries a label;
  // the whole set is scaled down if the tallest stack would overflow.
  const layouts = bars.map(({ b }) => {
    const total = bucketTotal(b) || 1;
    const base = Math.max((total / yMax) * plotHeight, MIN_SEG_H);
    const segs = b.positions.map((p) => Math.max(((p.capital || 0) / total) * base, MIN_SEG_H));
    return { total, segs, stackH: segs.reduce((s, h) => s + h, 0) + SEG_GAP * (segs.length - 1) };
  });
  const availH = plotHeight - 6;
  const maxStack = Math.max(1, ...layouts.map((l) => l.stackH));
  const scale = maxStack > availH ? availH / maxStack : 1;

  bars.forEach(({ b, x: bx, w: bw }, gi) => {
    const layout = layouts[gi];
    const midX = bx + bw / 2;
    // Ticker labels only fit once the bar is wide enough; below that the bar is
    // a shape-at-a-glance ribbon and the hover carries the detail.
    const showSegLabels = bw >= 26;
    let yCursor = zeroY; // bottom, working up

    // Draw in reverse so the sorted-first (loss / biggest) position ends on top.
    b.positions
      .map((p, i) => ({ p, segH: layout.segs[i] * scale }))
      .reverse()
      .forEach(({ p, segH }) => {
        const segY = yCursor - segH;
        // A realized (already assigned / called-away) leg draws in a lighter
        // shade of its family colour -- same hue, faded -- so "what just
        // happened" reads as clearly past next to the solid upcoming bars.
        const seg = svgEl('rect', {
          class: 'mark',
          x: bx, y: segY, width: bw, height: Math.max(segH - SEG_GAP, 1.5), rx: 1,
          fill: cssVar(FAMILY_COLOR[p.family] || '--text-muted'),
          'fill-opacity': p.realized ? 0.38 : 1,
          stroke: p.at_a_loss ? cssVar('--neg') : 'none',
          'stroke-width': p.at_a_loss ? 2 : 0,
        });
        group.appendChild(seg);
        if (p.in_the_money) {
          const wedge = Math.min(segH - SEG_GAP, 7);
          group.appendChild(
            svgEl('path', {
              d: `M${bx} ${segY} L${bx + 6} ${segY + wedge / 2} L${bx} ${segY + wedge} Z`,
              fill: cssVar('--neg'),
            })
          );
        }
        const segMidY = segY + (segH - SEG_GAP) / 2;
        // Weekly: ticker sits ON TOP of its own segment. Monthly (wide bars):
        // centred inside it. Daily (thin bars): off to the right, in body text.
        const labelMode = grain === 'week' ? 'top' : showSegLabels ? 'center' : 'side';
        if (labelMode === 'side') {
          group.appendChild(
            svgEl(
              'text',
              { class: 'exp-seg-label-side', x: bx + bw + 3, y: segMidY + 3, 'text-anchor': 'start' },
              (p.underlying || '') + (p.earnings_soon ? ' ⚡' : '')
            )
          );
        } else {
          const labelY =
            labelMode === 'top' && segH - SEG_GAP >= 18 ? segY + 10 : segMidY + 3;
          group.appendChild(
            svgEl(
              'text',
              { class: 'exp-seg-label', x: midX, y: labelY, 'text-anchor': 'middle' },
              p.underlying || ''
            )
          );
        }
        // Inside a bar the ⚡ sits at the top-right of the very segment whose
        // stock reports -- one per affected position, never a single flag
        // floating over the whole bar. (Daily side labels carry it inline.)
        if (p.earnings_soon && labelMode !== 'side') {
          group.appendChild(
            svgEl(
              'text',
              { x: bx + bw - 2, y: segY + 9, 'text-anchor': 'end', 'font-size': 10 },
              '⚡'
            )
          );
        }

        const beLine =
          p.type === 'CC'
            ? `wheel break-even ${p.wheel_breakeven != null ? money(p.wheel_breakeven, { cents: true }) : '—'}`
            : `break-even ${p.breakeven != null ? money(p.breakeven, { cents: true }) : '—'}`;
        const OUTCOME_VERB = { ASSIGNED: 'assigned', EXPIRED: 'expired', CLOSED: 'closed' };
        attachTip(
          seg,
          `${p.underlying} ${p.type} ${p.strike != null ? money(p.strike, { cents: true }) : ''}` +
            (p.realized
              ? ` · ${OUTCOME_VERB[p.outcome] || 'closed'} ${p.close_date ? dayLabel(p.close_date) : ''}`
              : p.days_to_expiry != null
                ? ` · ${p.days_to_expiry}d to expiry`
                : ''),
          [
            { label: p.realized ? 'Notional' : 'Capital', value: money(p.capital) },
            ...(p.realized
              ? [
                  {
                    label: 'Realized P/L',
                    value: p.realized_pl != null ? money(p.realized_pl) : '—',
                    valueClass: p.realized_pl == null ? '' : p.realized_pl >= 0 ? 'pos' : 'neg',
                  },
                ]
              : [
                  {
                    label: 'Moneyness',
                    value:
                      p.moneyness_pct == null
                        ? '—'
                        : `${p.in_the_money ? 'ITM' : 'OTM'} ${Math.abs(p.moneyness_pct).toFixed(1)}%`,
                    valueClass: p.in_the_money ? 'neg' : 'pos',
                  },
                ]),
            { label: beLine, value: p.last_close != null ? `last ${money(p.last_close, { cents: true })}` : '' },
            ...(p.at_a_loss
              ? [{ label: '⚠ ' + (p.loss_note || 'strike is past break-even'), value: '', valueClass: 'neg' }]
              : []),
            ...(p.earnings_soon
              ? [
                  {
                    label:
                      '⚡ ' +
                      (p.earnings_date
                        ? `${p.underlying} reports ${dayLabel(p.earnings_date)}` +
                          (p.days_to_earnings != null ? ` (${p.days_to_earnings}d)` : '')
                        : `${p.underlying} reports within 14 days`) +
                      ': hold off on writing here',
                    value: '',
                    valueClass: 'neg',
                  },
                ]
              : []),
          ]
        );
        yCursor = segY;
      });

    const topY = yCursor - 7;
    group.appendChild(
      svgEl('text', { class: 'exp-bar-date', x: midX, y: topY, 'text-anchor': 'middle' }, bucketLabel(b))
    );
    group.appendChild(
      svgEl('text', { class: 'tick-label', x: midX, y: topY - 11, 'text-anchor': 'middle' }, compactMoney(layout.total))
    );
  });

  // ---- "today" marker: drawn last so the dashed line sits on top of every
  // bar, at its true proportional spot on the time axis --------------------
  const todayX = xMs(parseDay(today).getTime());
  group.appendChild(
    svgEl('line', {
      x1: todayX, x2: todayX, y1: margin.top, y2: margin.top + plotHeight,
      stroke: cssVar('--neg'), 'stroke-width': 1.5, 'stroke-dasharray': '3 3',
    })
  );
  group.appendChild(
    svgEl(
      'text',
      {
        class: 'tick-label exp-today-label',
        x: todayX,
        y: margin.top + plotHeight + 12,
        'text-anchor': 'middle',
      },
      'today'
    )
  );

  // ---- legend --------------------------------------------------------------
  const swatch = (kind, varName, text) => {
    const item = el('span');
    const box = el('i');
    if (kind === 'outline') {
      box.style.background = 'transparent';
      box.style.border = `2px solid ${cssVar(varName)}`;
    } else {
      box.style.background = cssVar(varName);
      if (kind === 'faded') box.style.opacity = '0.38';
    }
    item.appendChild(box);
    item.appendChild(document.createTextNode(text));
    return item;
  };
  const anyRealized = allPositions.some((p) => p.realized);
  legend.appendChild(swatch('fill', FAMILY_COLOR.csp, 'CSP'));
  legend.appendChild(swatch('fill', FAMILY_COLOR.cc, 'CC'));
  if (anyLong) legend.appendChild(swatch('fill', FAMILY_COLOR.long, 'long leg (LP/LC)'));
  if (anyRealized) legend.appendChild(swatch('faded', FAMILY_COLOR.cc, 'faded = closed this month (assigned / called away)'));
  if (anyLoss) legend.appendChild(swatch('outline', '--neg', 'red edge = strike past break-even'));
  legend.appendChild(swatch('fill', '--neg', 'red wedge = in the money now'));
  legend.appendChild(
    el(
      'span',
      { class: 'legend-note' },
      'One stacked bar per expiry: each segment is a position, its height proportional to its capital, its ticker inside it. ⚡ on a segment = that stock reports earnings within 14 days (hover for the date). Faded bars left of today are this month’s assignments / call-aways (daily and weekly views). Hover any segment for the detail.'
    )
  );

  svg.setAttribute(
    'aria-label',
    `Expiration calendar, ${windowedBuckets.length} ${grain} bucket(s) from ${longDate(localIso(t0))} to ${longDate(localIso(tEnd - 1))}.`
  );
}

function renderRealizedGainsBody(data) {
  const table = $('realized-gains-table');
  const lots = $('closed-lots-table');
  if (table) renderRealizedGainsTable(table, data);
  if (lots) renderClosedLotsTable(lots, data);
}

// Realized Gains -- per-ticker cross-check of Fidelity's realized figure against
// the wheel engine's option P/L over the export's window (see wheel/taxes.py).
function renderRealizedGainsTable(table, data) {
  clear(table);
  const rows = data.rows || [];
  const thead = el('thead');
  thead.appendChild(
    rowOf('th', ['Ticker', 'Engine P/L (window)', 'Fidelity realized', 'Difference', ''])
  );
  table.appendChild(thead);
  const tbody = el('tbody');
  rows.forEach((r) => {
    const tr = el('tr');
    tr.appendChild(el('td', { class: 'left' }, r.underlying));
    tr.appendChild(el('td', { class: 'num ' + toneOf(r.wheel_engine_pl) }, money(r.wheel_engine_pl, { cents: true })));
    tr.appendChild(el('td', { class: 'num ' + toneOf(r.fidelity_realized) }, money(r.fidelity_realized, { cents: true })));
    tr.appendChild(el('td', { class: 'num ' + toneOf(r.difference) }, money(r.difference, { cents: true })));
    const st = el('td', { class: 'num rg-status ' + (r.status === 'close' ? 'matched' : 'investigate') }, r.status);
    tr.appendChild(st);
    tbody.appendChild(tr);
  });
  const t = data.totals || {};
  const totalRow = el('tr', { class: 'totals' });
  totalRow.appendChild(el('td', { class: 'left' }, 'Total'));
  totalRow.appendChild(el('td', { class: 'num' }, money(t.wheel_engine_pl, { cents: true })));
  totalRow.appendChild(el('td', { class: 'num' }, money(t.options_total, { cents: true })));
  totalRow.appendChild(el('td', { class: 'num' }, ''));
  totalRow.appendChild(
    el('td', { class: 'num' }, `${t.close || 0} close · ${t.review || 0} review`)
  );
  tbody.appendChild(totalRow);
  table.appendChild(tbody);
}

function renderClosedLotsTable(table, data) {
  clear(table);
  const lots = data.lots || [];
  if (!lots.length) return;
  const thead = el('thead');
  thead.appendChild(rowOf('th', ['Symbol', 'Description', 'Acquired', 'Sold', 'Qty', 'Cost', 'Proceeds', 'ST', 'LT']));
  table.appendChild(thead);
  const tbody = el('tbody');
  lots.forEach((l) => {
    tbody.appendChild(
      rowOf('td', [
        l.symbol,
        l.description,
        l.date_acquired,
        l.date_sold,
        l.quantity,
        money(l.cost_basis, { cents: true }),
        money(l.proceeds, { cents: true }),
        l.st_gain == null ? '—' : money(l.st_gain, { cents: true }),
        l.lt_gain == null ? '—' : money(l.lt_gain, { cents: true }),
      ])
    );
  });
  table.appendChild(tbody);
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

function tradeLogCell(label, value, { help, card, foot, tone, extra } = {}) {
  const cell = el('div', { class: 'tl-cell' });
  cell.appendChild(el('div', { class: 'tl-label' }, label));
  // An array value stacks each part on its own line (e.g. a date range), so a
  // long value never wraps mid-token across two lines. `tone` ('pos'|'neg')
  // colours the value.
  const valueNode = el('div', {
    class: 'tl-value' + (help || card ? ' help' : '') + (tone ? ' ' + tone : ''),
  });
  for (const part of Array.isArray(value) ? value : [value]) {
    valueNode.appendChild(el('div', {}, part));
  }
  if (help) setFormula(valueNode, help);
  // card: a structured CSP TO ENTER / CC TO EXIT tooltip (tipCardNode).
  setTipCard(valueNode, card);
  cell.appendChild(valueNode);
  // `foot` is one line, or an array for several stacked lines.
  if (foot) {
    for (const line of Array.isArray(foot) ? foot : [foot]) {
      cell.appendChild(el('div', { class: 'tl-foot' }, line));
    }
  }
  // `extra`: an already-built node (e.g. a drill-down toggle) appended after
  // the foot line(s) -- the one thing this cell can't express as plain text.
  if (extra) cell.appendChild(extra);
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
 * A full account snapshot: one sortable row per open covered call /
 * cash-secured put across every wheel -- `data.open_positions`, built by
 * `_build_open_positions` in wheel/api.py -- plus one row for any position
 * with no open leg at all right now, synthesized server-side by
 * `_no_contract_open_position_row`: a wheel with a current phase but no
 * contract yet (e.g. assigned shares with no covered call written), or
 * simply shares held with nothing written against them (a plain
 * buy-and-hold lot). Fields that genuinely need a contract (strike,
 * expiration, moneyness, yield, collateral, quantity, premium, ...) are
 * `null` there; every cell below already reads that as its own "no value"
 * case, so no extra branching is needed here beyond the Type badge and Qty,
 * which otherwise have no null case of their own. Two fields that look
 * contract-specific but aren't are filled anyway: Last Price % (the
 * ticker's own daily move) and, while shares are held, Breakeven (cost
 * basis, since no call written means no premium to net against it).
 * Filter-independent (an open contract needs watching whatever date window
 * is on screen), so it is not redrawn on filter changes beyond the single
 * render() pass. Symbols stay alphabetical and their rows stay contiguous; a
 * column click re-orders the rows *within* each symbol group.
 */
const OPEN_POS_COLUMNS = [
  { key: 'underlying', label: 'Symbol', left: true },
  { key: 'type', label: 'Type', left: true },
  { key: 'cc_to_exit', label: 'CC TO EXIT' },
  { key: 'csp_to_enter', label: 'CSP TO ENTER' },
  { key: 'last_close', label: 'Last Price' },
  { key: 'last_close_pct', label: 'Last Price %' },
  { key: 'profit_target', label: 'Profit Target' },
  { key: 'gap_pct', label: 'Gap to Target' },
  { key: 'cost_basis', label: 'Cost Basis' },
  { key: 'breakeven', label: 'Breakeven' },
  { key: 'wheel_breakeven', label: 'Wheel Breakeven' },
  { key: 'strike', label: 'Strike' },
  { key: 'signed_contracts', label: 'Qty' },
  { key: 'net_premium', label: 'Net Premium' },
  { key: 'expiration', label: 'Expiration' },
  { key: 'moneyness_pct', label: 'ITM/OTM (%)' },
  { key: 'shares_held', label: 'Shares Held' },
  { key: 'annualized_yield_pct', label: 'Ann. Yield' },
  { key: 'cycle_id', label: 'Wheel', left: true },
  { key: 'days_to_earnings', label: 'Earnings' },
];

// Header tooltips, one per column that isn't self-explanatory from its raw
// value. Kept short: the exact per-row math already lives in each cell's own
// tooltip (see cc_to_exit_card, positionBreakevenMath, etc.) --
// these just say what the column means and, where there's a real formula
// behind it, spell that out too.
const OPEN_POS_HEAD_HELP = {
  cc_to_exit: formula([
    'Strike for the next call on shares no open call covers yet.',
    'At least 2% above what those shares cost and profitable if assigned;',
    'of those, the one with delta closest to 0.30. Hover a cell for the math.',
  ]),
  csp_to_enter: formula([
    'Put strike to sell next: inside or near historical support below the price,',
    'delta near 0.25 and at least 30% annualized on strike collateral.',
    'NO QUALIFYING CSP when no put passes. Hover a cell for the math.',
  ]),
  profit_target: formula([
    'Shares held only: fixed profitable-exit price.',
    'Green once last price reaches it, red below.',
  ]),
  gap_pct: formula([
    'Gap % = (Target - Last Price) ÷ Last Price',
    'Target: Profit Target on CC rows, the CSP TO ENTER strike on CSP rows.',
    'Colored green/red on CC rows only.',
  ]),
  breakeven: formula(['This contract alone.']),
  wheel_breakeven: formula([
    'Whole cycle:',
    'Wheel Breakeven = Cost Basis - Banked/share (premium, realized P/L, dividends)',
  ]),
  expiration: formula(['⚠ marks a contract under 8 days from expiry.']),
  moneyness_pct: formula([
    'Strike vs. last close.',
    'Green is the cushion (short) or the intrinsic value (long).',
  ]),
  shares_held: formula(['Currently held from this wheel, net of any shares sold or called away.']),
  annualized_yield_pct: formula(['Ann. Yield = Net Premium ÷ Collateral × (365 ÷ Days open)']),
  cycle_id: formula(["Click the id to open that wheel's Trade Log."]),
  days_to_earnings: formula([
    'Next report date; within 14 days the cell turns amber with ⚠.',
    'Override in data/earnings.json.',
  ]),
};

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

// The break-even math, spelled out with this row's own numbers, per type --
// same 3-line formula/substitution/result shape as the other target tooltips.
// Derived from breakeven itself (rather than a separately-carried
// premium/share field) since breakeven already encodes it: e.g. a CSP's
// breakeven is strike - premium/share, so premium/share = strike - breakeven.
// Returns a lines array for the caller to join with formula(); [] when there's
// nothing to explain.
function positionBreakevenMath(row) {
  if (row.breakeven === null || row.breakeven === undefined) return [];
  const be = money(row.breakeven, { cents: true });
  const fmt = (n) => money(n, { cents: true });
  if (row.type === 'CSP' && row.strike != null) {
    return ['Breakeven = strike - premium/share', `= ${fmt(row.strike)} - ${fmt(row.strike - row.breakeven)}`, `= ${be}`];
  }
  if (row.type === 'CC' && row.cost_basis != null) {
    return ['Breakeven = cost basis - premium/share', `= ${fmt(row.cost_basis)} - ${fmt(row.cost_basis - row.breakeven)}`, `= ${be}`];
  }
  if (row.type === 'LP' && row.strike != null) {
    return [
      'Breakeven = strike - cost/share (profit below it)',
      `= ${fmt(row.strike)} - ${fmt(row.strike - row.breakeven)}`,
      `= ${be}`,
    ];
  }
  if (row.type === 'LC' && row.strike != null) {
    return [
      'Breakeven = strike + cost/share (profit above it)',
      `= ${fmt(row.strike)} + ${fmt(row.breakeven - row.strike)}`,
      `= ${be}`,
    ];
  }
  // No open leg at all (a synthesized row -- see _no_contract_open_position_row):
  // shares held with no call written yet, so this leg's premium is zero and
  // the breakeven is just the raw cost basis.
  if (row.type == null && row.wheel_phase === 'cc' && row.cost_basis != null) {
    return ['Breakeven = cost basis (no call written yet, no premium collected)', `= ${be}`];
  }
  return [];
}

// Wheel breakeven = cost basis - (banked P/L per share). Read backwards from
// the two numbers already on screen (Cost Basis column, Wheel Breakeven cell)
// rather than re-deriving the banked total, which the row doesn't carry.
// Returns a lines array, same convention as positionBreakevenMath.
function wheelBreakevenMath(row) {
  if (row.wheel_breakeven == null || row.cost_basis == null) return [];
  const fmt = (n) => money(n, { cents: true });
  const bankedPerShare = row.cost_basis - row.wheel_breakeven;
  return [
    'Wheel breakeven = cost basis - banked/share (premium, realized P/L, dividends)',
    `= ${fmt(row.cost_basis)} - ${fmt(bankedPerShare)}`,
    `= ${fmt(row.wheel_breakeven)}`,
  ];
}

function openPositionRow(row, isGroupStart, groupSize) {
  const isLong = row.side === 'LONG';
  const tr = el('tr', {
    class: 'op-row' + (isGroupStart ? ' op-group-start' : '') + (isLong ? ' op-long' : ''),
  });

  const symCell = el('td', { class: 'left ticker-cell' });
  if (isGroupStart) {
    const bits = [row.name].filter(Boolean);
    if (groupSize > 1) bits.push(`${groupSize} open positions`);
    if (bits.length) symCell.title = bits.join(' · ');
    symCell.appendChild(tickerLink(row.underlying));
  }

  // No `type` at all means no open contract on this wheel right now (a
  // synthesized row -- see _no_contract_open_position_row): a plain dash,
  // not a badge with nothing to label.
  const typeCell = el('td', { class: 'left' });
  if (row.type == null) {
    typeCell.appendChild(document.createTextNode('—'));
  } else {
    const optionSymbol = tradingViewOptionSymbol(row);
    const label = OP_TYPE_LABEL[row.type] || row.type;
    const badge = optionSymbol
      ? el(
          'a',
          {
            class: 'badge op-type op-type-' + row.type,
            href: `https://www.tradingview.com/chart/?symbol=${encodeURIComponent(optionSymbol)}`,
            target: '_blank',
            rel: 'noopener noreferrer',
            title: `${label}. Click to open this contract on TradingView.`,
          },
          row.type
        )
      : el('span', { class: 'badge op-type op-type-' + row.type, title: label }, row.type);
    typeCell.appendChild(badge);
  }

  const strikeCell = el('td', { class: 'num' }, money(row.strike, { cents: true }));

  // CC TO EXIT / CSP TO ENTER / Profit Target, all the backend's
  // already-computed numbers (ccToExitCell / cspToEnterCell /
  // profit_target_explanation in wheel/api.py). The first two are strike
  // suggestions (violet, neutral); Profit Target is the one real pass/fail
  // threshold, colored green/red by whether last price has reached it.
  const strikeFloorCell = ccToExitCell(row);
  const cspEntryCell = cspToEnterCell(row.underlying);

  const metTone =
    row.wheel_phase === 'cc' && row.profit_target != null && row.last_close != null
      ? toneOf(row.last_close - row.profit_target)
      : '';
  const profitTargetCell = el('td', { class: 'num ' + metTone }, money(row.profit_target, { cents: true }));
  if (row.profit_target_explanation) profitTargetCell.title = row.profit_target_explanation;

  // Same figure and tone as the Profit Target cell above -- how far last
  // price sits from it, CC-phase only (a CSP row has no Profit Target to gap
  // against).
  const gapCell = el('td', { class: 'num ' + metTone }, signedPct(row.gap_pct));

  // Expiration, formatted the same as the Earnings column (longDate: "Sep
  // 19, 2026") plus the weekday and days-left, with a warning glyph when the
  // contract is inside a week.
  const nearExpiry =
    row.days_to_expiry !== null && row.days_to_expiry !== undefined && row.days_to_expiry < 8;
  const expCell = el('td', { class: 'num' + (nearExpiry ? ' op-near-expiry' : '') });
  if (row.expiration) {
    const dow = parseDay(row.expiration).toLocaleDateString('en-US', { weekday: 'short' });
    expCell.appendChild(document.createTextNode(`${longDate(row.expiration)} · ${dow} · ${row.days_to_expiry}d`));
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

  // Average per-share cost basis of the cycle's currently-held shares -- what
  // the CC break-even (cost_basis - premium/share) is built from. Blank for a
  // share-less CSP wheel or a cycle holding shares with unknown basis.
  const costBasisCell = el(
    'td',
    { class: 'num' },
    row.cost_basis === null || row.cost_basis === undefined ? '—' : money(row.cost_basis, { cents: true })
  );
  costBasisCell.title = 'Average cost basis per share of the wheel\'s currently-held shares.';

  // The two break-even cells carry different signals. Position break-even is
  // this trade alone: last price vs. its own break-even, with a short put/call
  // favorable above it and a long put favorable below it (a long call is
  // favorable above it, same side as the shorts). Wheel break-even is the
  // whole cycle: last price vs. the wheel's cost basis net of everything
  // banked so far (for a share-less CSP wheel, which has no wheel break-even,
  // its own position break-even stands in). No color without a reference and
  // a price -- and a standalone directional long (no wheel behind it) stays
  // uncolored on the wheel cell: its break-even isn't a wheel-profit signal.
  const positionFavorable =
    row.breakeven === null || row.breakeven === undefined ||
    row.last_close === null || row.last_close === undefined
      ? null
      : row.type === 'LP'
        ? row.last_close <= row.breakeven
        : row.last_close >= row.breakeven;
  const positionTone = positionFavorable === null ? '' : positionFavorable ? 'pos' : 'neg';

  const wheelRef = row.wheel_breakeven === null || row.wheel_breakeven === undefined
    ? row.breakeven
    : row.wheel_breakeven;
  const standaloneLong = isLong && (row.wheel_breakeven === null || row.wheel_breakeven === undefined);
  const wheelUnderwater =
    standaloneLong ||
    wheelRef === null || wheelRef === undefined || row.last_close === null || row.last_close === undefined
      ? null
      : row.last_close < wheelRef;
  const wheelTone = wheelUnderwater === null ? '' : wheelUnderwater ? 'neg' : 'pos';
  const wheelStatus =
    wheelUnderwater === null
      ? null
      : wheelUnderwater
        ? 'Whole wheel underwater (last price below the wheel break-even).'
        : 'Whole wheel in profit (last price above the wheel break-even).';
  // Appends the shared wheel-status caveat as its own trailing line, blank
  // line first -- the "formula, then a blank line, then one caveat" shape --
  // instead of the old single run-on sentence.
  const withWheelStatus = (lines) => (wheelStatus ? [...lines, '', wheelStatus] : lines);

  const beCell = el('td', { class: 'num ' + positionTone }, money(row.breakeven, { cents: true }));
  const beLines = positionBreakevenMath(row);
  if (beLines.length) beCell.title = formula(withWheelStatus(beLines));

  const wheelBeCell = el('td', { class: 'num ' + wheelTone }, money(row.wheel_breakeven, { cents: true }));
  const wheelBeLines = wheelBreakevenMath(row);
  wheelBeCell.title = formula(
    withWheelStatus(
      wheelBeLines.length
        ? wheelBeLines
        : [
            'The whole wheel: cost of shares still held, less everything banked',
            '(premium, realized P/L, dividends). A dash with no shares held.',
          ]
    )
  );

  // `shares_untracked` (wheel/api.py) is the broker Positions snapshot's real
  // quantity minus what the wheel model can trace to a known lot -- shares
  // bought before every loaded export begins, the same gap net_worth's own
  // `untracked_equity_value` names at the portfolio level. Shown as the real,
  // total share count (what's actually in the account), with a tooltip
  // explaining that the wheel math on this row (breakeven, P&L) only covers
  // the smaller tracked piece -- rather than silently displaying just the
  // tracked number with no sign anything is missing.
  const totalShares = (row.shares_held || 0) + (row.shares_untracked || 0);
  const sharesHeldCell = el(
    'td',
    { class: 'num' },
    totalShares ? Math.round(totalShares).toLocaleString('en-US') : '—'
  );
  if (row.shares_untracked > 0) {
    sharesHeldCell.title = formula([
      `${Math.round(totalShares).toLocaleString('en-US')} held per the Positions snapshot; ${Math.round(row.shares_untracked).toLocaleString('en-US')} predate the export`,
      `  (no cost basis), so breakeven and P&L cover only ${Math.round(row.shares_held || 0).toLocaleString('en-US')}.`,
    ]);
  }

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
    ? 'Strike vs. last price. In-the-money (green) is where a long put/call has intrinsic value.'
    : 'Strike vs. last price. Positive = out-of-the-money cushion; negative = in-the-money (assignment risk).';

  const lastCloseCell = el('td', { class: 'num' }, money(row.last_close, { cents: true }));
  const lastClosePctCell = el('td', { class: 'num ' + toneOf(row.last_close_pct) }, signedPct(row.last_close_pct));

  const qtyCell = el('td', { class: 'num' }, row.signed_contracts == null ? '—' : row.signed_contracts);
  const netPremiumCell = el(
    'td',
    { class: 'num ' + toneOf(row.net_premium) },
    money(row.net_premium, { cents: true, sign: true })
  );

  const wheelCell = el('td', { class: 'left op-wheel' });
  wheelCell.appendChild(
    row.cycle_id ? wheelLink(row.cycle_id, row.underlying) : document.createTextNode('—')
  );

  // Next earnings date -- same amber-within-14-days treatment as the
  // Covered-call / CSP candidate tables.
  const earningsTd = earningsCell(row).cell;

  const yieldCell = el('td', { class: 'num ' + toneOf(row.annualized_yield_pct) }, pct(row.annualized_yield_pct));
  yieldCell.title =
    'Net premium ÷ (strike × 100 × contracts), scaled to a year over the contract\'s open→expiry span.';

  // Appended in the same order as OPEN_POS_COLUMNS.
  tr.appendChild(symCell);
  tr.appendChild(typeCell);
  tr.appendChild(strikeFloorCell);
  tr.appendChild(cspEntryCell);
  tr.appendChild(lastCloseCell);
  tr.appendChild(lastClosePctCell);
  tr.appendChild(profitTargetCell);
  tr.appendChild(gapCell);
  tr.appendChild(costBasisCell);
  tr.appendChild(beCell);
  tr.appendChild(wheelBeCell);
  tr.appendChild(strikeCell);
  tr.appendChild(qtyCell);
  tr.appendChild(netPremiumCell);
  tr.appendChild(expCell);
  tr.appendChild(moneyness);
  tr.appendChild(sharesHeldCell);
  tr.appendChild(yieldCell);
  tr.appendChild(wheelCell);
  tr.appendChild(earningsTd);

  return tr;
}

function renderOpenPositions() {
  const table = $('open-positions-table');
  if (!table) return;
  const card = $('open-positions-card');
  const rows = ((state.data && state.data.open_positions) || []).map((row) => ({
    ...row,
    csp_to_enter: cspToEnterStrike(row.underlying),
  }));
  if (card) card.hidden = rows.length === 0;
  const countLine = $('open-positions-count');
  if (countLine) {
    // "Position" here means an actual open contract (`type` set) -- the
    // no-contract synthetic rows (shares held, nothing written) aren't one.
    const optionCount = rows.filter((row) => row.type).length;
    clear(countLine);
    if (rows.length) {
      countLine.appendChild(
        el('strong', {}, `${optionCount} open option position${optionCount === 1 ? '' : 's'}`)
      );
    }
  }
  clear(table);
  renderRecommendedBtc();
  if (!rows.length) return;

  const { key, dir } = state.openPosSort;

  const thead = el('thead');
  const headRow = el('tr');
  for (const column of OPEN_POS_COLUMNS) {
    const th = el('th', { class: `sortable${column.left ? ' left' : ''}` }, column.label);
    if (key === column.key) th.textContent = column.label + (dir === 1 ? ' ▲' : ' ▼');
    setFormula(th, OPEN_POS_HEAD_HELP[column.key]);
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

/* --------------------------------------- table: recommended buy to close */

const RECOMMENDED_BTC_COLUMNS = [
  { key: 'underlying', label: 'Symbol', left: true },
  { key: 'type', label: 'Type', left: true },
  { key: 'btc_targets', label: 'Recommended BTC (50% / 20% / 10%)' },
  { key: 'min_roll_premium', label: 'Min Roll Premium' },
  { key: 'premium_if_btc', label: 'Premium Collected if BTC' },
  { key: 'last_close', label: 'Last Price' },
  { key: 'strike', label: 'Strike' },
  { key: 'signed_contracts', label: 'Qty' },
  { key: 'cost_basis_per_contract', label: 'Cost Basis / Contract' },
  { key: 'net_premium', label: 'Net Premium' },
  { key: 'expiration', label: 'Expiration' },
  { key: 'cycle_id', label: 'Wheel', left: true },
];

// Labels the three rungs share everywhere they're spelled out: cell text,
// tooltips, the ladder order itself (matches `btc_targets`, wheel/api.py).
const BTC_LADDER = [
  { fraction: 0.5, label: '50%' },
  { fraction: 0.2, label: '20%' },
  { fraction: 0.1, label: '10%' },
];

/**
 * One open short put/call, paired with the standing buy-to-close ladder --
 * see `_btc_targets` (wheel/api.py) for the 50% / 20% / 10%-of-premium
 * rungs themselves, always shown together rather than the app picking one
 * (that judgment call -- which rung fits where this position stands right
 * now -- is left to the trader). Reuses the Open option positions table's
 * own row material (same `state.data.open_positions`, already
 * merged/sorted) rather than a separate payload, so the two tables can
 * never drift out of sync.
 */
function recommendedBtcRow(row) {
  const tr = el('tr');

  const symCell = el('td', { class: 'left' });
  symCell.appendChild(tickerLink(row.underlying));
  tr.appendChild(symCell);

  const label = OP_TYPE_LABEL[row.type] || row.type;
  const typeCell = el('td', { class: 'left' });
  typeCell.appendChild(el('span', { class: 'badge op-type op-type-' + row.type, title: label }, row.type));
  tr.appendChild(typeCell);

  const targets = row.btc_targets;
  const contracts = row.contracts == null ? null : Math.abs(row.contracts);

  // Rounded down to the nearest dime server-side (wheel/api.py,
  // _btc_target) once a rung clears ten cents a share -- below that it
  // falls back to ordinary nearest-cent rounding, so either way this is
  // never more than two decimal places.
  const btcCell = el(
    'td',
    { class: 'num cc-target' },
    targets ? targets.map((v) => money(v, { cents: true })).join(' / ') : '—'
  );
  if (targets && row.open_price) {
    btcCell.title = formula([
      'BTC targets (rounded down to $0.10 above $0.10/share):',
      ...BTC_LADDER.map(
        (rung, i) => `  ${rung.label} × ${money(row.open_price, { cents: true })} = ${money(targets[i], { cents: true })}`
      ),
    ]);
  }
  tr.appendChild(btcCell);

  // Minimum credit worth collecting to roll rather than let the current
  // contract resolve (assignment for a CSP, call-away for a CC) -- see
  // rollPremiumFormula() / _min_roll_premium in wheel/api.py. Same
  // key-figure styling as the BTC targets cell above -- it's the other
  // actionable threshold in this row.
  const rollCell = el(
    'td',
    { class: 'num cc-target' },
    row.min_roll_premium == null ? '—' : money(row.min_roll_premium, { cents: true })
  );
  const rollTitle = rollPremiumFormula(row);
  if (rollTitle) rollCell.title = rollTitle;
  tr.appendChild(rollCell);

  // What buying back at each rung actually leaves you with: the original
  // credit, less the cost of closing it there.
  const kept =
    targets && contracts && row.net_premium != null
      ? targets.map((v) => row.net_premium - v * contracts * 100)
      : null;
  const netCell = el(
    'td',
    { class: 'num' },
    kept ? kept.map((v) => money(v, { cents: true, sign: true })).join(' / ') : '—'
  );
  if (kept) {
    netCell.title = formula([
      'Net Premium, less the cost of closing at each rung above:',
      ...BTC_LADDER.map(
        (rung, i) =>
          `  ${rung.label}: ${money(row.net_premium, { cents: true })} - (${money(targets[i], { cents: true })} × 100 × ${contracts}) = ${money(kept[i], { cents: true })}`
      ),
    ]);
  }
  tr.appendChild(netCell);

  tr.appendChild(el('td', { class: 'num' }, money(row.last_close, { cents: true })));
  tr.appendChild(el('td', { class: 'num' }, money(row.strike, { cents: true })));
  tr.appendChild(el('td', { class: 'num' }, row.signed_contracts == null ? '—' : row.signed_contracts));

  // Net premium per contract, then per share (a contract covers 100 shares)
  // -- the per-share cost basis this credit was actually collected at,
  // fees included (unlike `open_price`, the raw quoted price).
  const perContract =
    contracts && row.net_premium != null ? row.net_premium / contracts / 100 : null;
  const perContractCell = el(
    'td',
    { class: 'num ' + toneOf(perContract) },
    perContract == null ? '—' : money(perContract, { cents: true, sign: true })
  );
  if (perContract != null) {
    perContractCell.title = formula([
      'Cost Basis / Contract = Net Premium ÷ contracts ÷ 100',
      `= ${money(row.net_premium, { cents: true })} ÷ ${contracts} ÷ 100`,
      `= ${money(perContract, { cents: true })}`,
    ]);
  }
  tr.appendChild(perContractCell);

  tr.appendChild(
    el('td', { class: 'num ' + toneOf(row.net_premium) }, money(row.net_premium, { cents: true, sign: true }))
  );

  const nearExpiry = row.days_to_expiry !== null && row.days_to_expiry !== undefined && row.days_to_expiry < 8;
  const expCell = el('td', { class: 'num' + (nearExpiry ? ' op-near-expiry' : '') });
  if (row.expiration) {
    const dow = parseDay(row.expiration).toLocaleDateString('en-US', { weekday: 'short' });
    expCell.appendChild(document.createTextNode(`${longDate(row.expiration)} · ${dow} · ${row.days_to_expiry}d`));
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

  const wheelCell = el('td', { class: 'left op-wheel' });
  wheelCell.appendChild(row.cycle_id ? wheelLink(row.cycle_id, row.underlying) : document.createTextNode('—'));
  tr.appendChild(wheelCell);

  return tr;
}

function renderRecommendedBtc() {
  const table = $('recommended-btc-table');
  if (!table) return;
  const card = $('recommended-btc-card');
  const rows = ((state.data && state.data.open_positions) || []).filter(
    (row) => row.type === 'CC' || row.type === 'CSP'
  );
  if (card) card.hidden = rows.length === 0;
  clear(table);
  if (!rows.length) return;

  // Rows can disagree on the roll target now (whichever is later of next
  // week's Friday and the contract's own expiry -- see _min_roll_premium
  // in wheel/api.py), so the DTE can't read once in the header; the
  // per-row tooltip spells out each one's own days and math instead.
  const thead = el('thead');
  const headRow = el('tr');
  for (const column of RECOMMENDED_BTC_COLUMNS) {
    headRow.appendChild(el('th', { class: column.left ? 'left' : '' }, column.label));
  }
  thead.appendChild(headRow);
  table.appendChild(thead);

  const tbody = el('tbody');
  for (const row of rows) tbody.appendChild(recommendedBtcRow(row));
  table.appendChild(tbody);
}

/* -------------------------------------------- table: covered-call candidates */

const CC_CAND_COLUMNS = [
  { key: 'underlying', label: 'Symbol', left: true },
  { key: 'cc_to_exit', label: 'CC TO EXIT' },
  { key: 'contracts_available', label: 'Qty' },
  { key: 'breakeven', label: 'Breakeven' },
  { key: 'wheel_breakeven', label: 'Wheel Breakeven' },
  { key: 'last_close', label: 'Last Price' },
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
 * Positions holding shares not yet covered by an open covered call, from
 * `data.cc_candidates` (see `_build_cc_candidates` in wheel/api.py). Positions
 * with 100+ uncovered shares lead the table with a negative "Qty" (the
 * covered-call position that could be opened); sub-100 lots follow for
 * visibility. CC TO EXIT is the wheel's own value (ccToExitCell), the same
 * one every other table shows. Sortable.
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
    const symCell = el('td', { class: 'left ticker-cell' });
    if (row.name) symCell.title = row.name;
    symCell.appendChild(tickerLink(row.underlying));
    tr.appendChild(symCell);

    // `shares_untracked` (wheel/api.py, same concept net_worth's own
    // `untracked_equity_value` names portfolio-wide): shares this broker
    // account really holds -- and that a call really can be written against
    // -- but with no known cost basis (bought before every loaded export
    // begins), so every dollar figure below can only be computed from the
    // smaller tracked piece.
    const trackedShares = (row.shares_held || 0) - (row.shares_untracked || 0);
    // Untracked shares get a cost once the broker file reports one
    // (`untracked_cost_per_share`); only without it are they left unpriced.
    const brokerCosted = row.shares_untracked && row.untracked_cost_per_share != null;
    const unpricedShares = row.shares_untracked && !brokerCosted ? row.shares_untracked : 0;
    const pricedShares = (row.shares_held || 0) - unpricedShares;
    const brokerCostNote = brokerCosted
      ? [
          `${shares(row.shares_untracked)} pre-history shares use your broker's avg cost`,
          `  of ${money(row.untracked_cost_per_share, { cents: true })}.`,
        ]
      : [];

    tr.appendChild(ccToExitCell(row));

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
    // price: dollars and percent in one cell, green up / red down.
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
        'Gain / Loss = Shares × (Last price - Cost basis)',
        `= ${shares(pricedShares)} × (${money(row.last_close, { cents: true })} - ${money(row.cost_basis_per_share, { cents: true })})`,
        `= ${money(gl, { cents: true })}`,
        'Raw cost basis; collected premium not included.',
        ...brokerCostNote,
        ...(unpricedShares
          ? [`Only the ${shares(trackedShares)} shares with a known cost basis.`]
          : []),
      ]);
    }
    tr.appendChild(glCell);

    // Whole-share count for the eye; the exact (sometimes fractional) figure,
    // and any untracked-shares note, sit in the tooltip -- fractional-share
    // buys and DRIP dust leave odd remainders that don't matter for a
    // 100-lot covered call.
    const rounded = Math.round(row.shares_held);
    const sharesCell = el('td', { class: 'num' }, rounded.toLocaleString('en-US'));
    const sharesTitle = [];
    if (Math.abs(row.shares_held - rounded) > 1e-6) sharesTitle.push(`${shares(row.shares_held)} exact.`);
    if (brokerCosted) sharesTitle.push(...brokerCostNote);
    if (unpricedShares) {
      sharesTitle.push(
        `${shares(row.shares_untracked)} of these have no known cost basis (bought before every`,
        `  loaded export begins), so Target price and Gain/Loss cover only the`,
        `  other ${shares(trackedShares)}.`
      );
    }
    if (row.shares_covered) {
      sharesTitle.push(
        `${shares(row.shares_covered)} already back an open covered call;`,
        `  the other ${shares(row.shares_held - row.shares_covered)} are free to write calls against.`
      );
    }
    if (sharesTitle.length) sharesCell.title = formula(sharesTitle);
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
 * glance: CC candidates + this. A second line names that same collateral
 * directly -- Available Cash only implies it as a subtraction.
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

  // These two are one short fact, not a dashboard-tile's worth of screen: render
  // them as a single line -- label plain, only the figures bold, each carrying
  // its own formula on hover.
  const line = el('div', { class: 'csp-cash-line' });

  line.appendChild(document.createTextNode('Available Cash: '));
  const cashAmt = el('strong', {}, money(available, { cents: true }));
  setFormula(
    cashAmt,
    formula([
      'Available Cash = Account cash - Open CSP collateral',
      '  Excludes unrealized gains and equity value.',
      `= ${money(cashTotal, { cents: true })} - ${money(putCollateral, { cents: true })}`,
      `= ${money(available, { cents: true })}`,
    ])
  );
  line.appendChild(cashAmt);

  if (pct !== null) {
    line.appendChild(document.createTextNode('  ·  '));
    const shareAmt = el('strong', {}, `${pct.toFixed(1)}%`);
    setFormula(
      shareAmt,
      formula([
        'Share of account = Available Cash ÷ Total account value',
        `= ${money(available, { cents: true })} ÷ ${money(totalValue, { cents: true })}`,
        `= ${pct.toFixed(1)}%`,
      ])
    );
    line.appendChild(shareAmt);
    line.appendChild(document.createTextNode(' of account'));
  }

  // The Positions snapshot these figures are built from can predate real
  // cash the broker's ledger already has -- an assignment/call-away posts
  // days after the option's own expiry. `cash_pending_since_snapshot` (same
  // "combined" sum as cashTotal/totalValue above) is the exact net cash flow
  // of every transaction posted after the snapshot's own date; flagged only
  // once it is both a real dollar amount and a meaningful slice of the
  // account, so a stray dividend doesn't light this up daily.
  const pendingCash =
    totals && typeof totals.cash_pending_since_snapshot === 'number'
      ? totals.cash_pending_since_snapshot
      : 0;
  const pendingPct = totalValue ? (100 * Math.abs(pendingCash)) / totalValue : 0;
  if (Math.abs(pendingCash) >= 250 && pendingPct >= 0.5) {
    const snapshotDate = netWorth && netWorth.as_of ? longDate(netWorth.as_of.slice(0, 10)) : null;
    line.appendChild(document.createTextNode('  '));
    const warn = el(
      'span',
      { class: 'csp-cash-warn' },
      `⚠ Upload a newer Positions export: ${money(pendingCash, { cents: true, sign: true })} not reflected yet`
    );
    setFormula(
      warn,
      formula([
        `Positions snapshot${snapshotDate ? ` (${snapshotDate})` : ''} is older than the ledger.`,
        `${money(pendingCash, { cents: true })} of net cash since then (assignments, expiries,`,
        '  dividends) is not yet in Cash / Available Cash.',
      ])
    );
    line.appendChild(warn);
  }

  host.appendChild(line);

  // The other side of Available Cash's subtraction, named instead of left
  // implicit: what's currently tied up backing open cash-secured puts.
  const reservedLine = el('div', { class: 'csp-cash-line' });
  reservedLine.appendChild(document.createTextNode('Cash reserved for options strategies: '));
  const reservedAmt = el('strong', {}, money(putCollateral, { cents: true }));
  setFormula(
    reservedAmt,
    formula([
      'Cash reserved = Collateral on every open CSP',
      `= ${money(putCollateral, { cents: true })}`,
    ])
  );
  reservedLine.appendChild(reservedAmt);
  if (totalValue) {
    const reservedPct = (100 * putCollateral) / totalValue;
    reservedLine.appendChild(document.createTextNode('  ·  '));
    const reservedPctAmt = el('strong', {}, `${reservedPct.toFixed(1)}%`);
    setFormula(
      reservedPctAmt,
      formula([
        'Share of account = Cash reserved ÷ Total account value',
        `= ${money(putCollateral, { cents: true })} ÷ ${money(totalValue, { cents: true })}`,
        `= ${reservedPct.toFixed(1)}%`,
      ])
    );
    reservedLine.appendChild(reservedPctAmt);
    reservedLine.appendChild(document.createTextNode(' of account'));
  }
  host.appendChild(reservedLine);

  renderCspCandidates(available);
}

const CSP_CAND_COLUMNS = [
  { key: 'underlying', label: 'Symbol', left: true },
  { key: 'stars', label: 'Signal' },
  { key: 'csp_to_enter', label: 'CSP TO ENTER' },
  { key: 'contracts', label: 'Qty' },
  { key: 'cash_per_contract', label: 'Cash / Contract' },
  { key: 'avg_annualized_roc_pct', label: 'Avg Ann. ROC' },
  { key: 'monthly_premium_pct', label: 'Mo. Prem %' },
  { key: 'ppd', label: 'PPD' },
  { key: 'last_close', label: 'Last Price' },
  { key: 'wheels', label: 'Past Wheels' },
  { key: 'net_realized_pl', label: 'Realized P/L' },
  { key: 'days_to_earnings', label: 'Earnings' },
  { key: 'sector', label: 'Sector', left: true },
];

const EARNINGS_WARN_DAYS = 14;

/**
 * Sort key for the Signal column: the absolute `raw_stars` behind the whole
 * stars shown, so names on the same star level still order by score. Only
 * an ordering: the displayed stars are the backend's `stars`, never
 * re-graded against whichever other rows happen to be listed.
 */
function cspSignalSortKey(row) {
  const bk = row.star_breakdown || {};
  return bk.raw_stars != null ? bk.raw_stars : row.stars;
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
  const mod = (x) => (x > 0 ? `+${x.toFixed(1)}` : x < 0 ? `-${Math.abs(x).toFixed(1)}` : '±0');
  const inline = (x) => (x > 0 ? ` + ${x.toFixed(1)}` : x < 0 ? ` - ${Math.abs(x).toFixed(1)}` : '');
  const em = bk.modifiers && bk.modifiers.earnings ? bk.modifiers.earnings : { stars: 0, note: '' };
  const sm = bk.modifiers && bk.modifiers.sector ? bk.modifiers.sector : { stars: 0, note: '' };
  const pct1 = (x) => (x === null || x === undefined ? '—' : x.toFixed(1) + '%');
  const rawLine = `  base ${bk.base_stars}${inline(em.stars)}${inline(sm.stars)} = ${bk.raw_stars}, rounded`;
  return formula([
    `Signal ${bk.stars} / 5`,
    rawLine,
    "  A rating of your past wheels on this ticker and today's market; not a chance of profit.",
    '  It does not rate the company: no profit, debt or valuation data is loaded.',
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
          (soon ? ', within 14 days: hold off on writing here' : '')
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
 * Inside the Cash for CSPs card: tickers wheeled at a net profit before. A
 * ticker with a CSP TO ENTER strike is sized at that strike: "Cash /
 * Contract" is the backend's collateral_per_contract (strike × 100) and
 * "Qty" how many such puts `available` cash secures; it's shown only when
 * that is at least one. A ticker with NO QUALIFYING CSP has nothing to size
 * (Qty and Cash / Contract read a dash) and keeps the card's original
 * visibility gate, one 100-share lot at its last price within the cash, so
 * the reason stays visible. "Signal" flags a ticker whose past wheels paid
 * well for the capital; a row whose earnings land within 14 days is amber.
 * Data: `data.csp_candidates` (`_build_csp_candidates`).
 */
function renderCspCandidates(available) {
  const table = $('csp-candidates-table');
  const hint = $('csp-candidates-hint');
  if (!table) return;
  clear(table);

  const base = (state.data && state.data.csp_candidates) || [];
  const rows = base
    .map((row) => {
      const entry = cspToEnterFor(row.underlying);
      const collateral = entry ? entry.collateral_per_contract : null;
      return {
        ...row,
        contracts: collateral ? Math.floor(available / collateral) : null,
        cash_per_contract: collateral,
        // The ticker's canonical CSP TO ENTER, the same entry every other
        // table shows (cspToEnterCell); carried here only as the sort key.
        csp_to_enter: cspToEnterStrike(row.underlying),
      };
    })
    .filter((row) =>
      row.cash_per_contract
        ? row.contracts >= 1
        : row.last_close > 0 && available >= row.last_close * 100
    );

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

  const sortValue = (row) => (key === 'stars' ? cspSignalSortKey(row) : row[key]);
  const sorted = rows.slice().sort((a, b) => {
    const av = sortValue(a);
    const bv = sortValue(b);
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

    const symCell = el('td', { class: 'left ticker-cell' });
    if (row.name) symCell.title = row.name;
    symCell.appendChild(tickerLink(row.underlying));
    // UNVERIFIED (backend vetting.status): no eligibility check failed, but
    // at least one couldn't be run for missing data. A QUALIFIED row has no flag.
    const unvetted = (row.vetting && row.vetting.unvetted) || [];
    if (unvetted.length) {
      const flag = el('sup', { class: 'unvetted-flag' }, '?');
      flag.title = 'Unverified: these checks had no data, so they were not passed:\n· ' + unvetted.join('\n· ');
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

    tr.appendChild(cspToEnterCell(row.underlying));

    const sized = row.cash_per_contract != null;
    const strikeText = sized ? money(row.csp_to_enter, { cents: true }) : null;
    const qtyCell = el('td', { class: 'num' }, sized ? String(row.contracts) : '—');
    qtyCell.title = sized
      ? formula([
          'Qty = Available Cash ÷ (CSP TO ENTER strike × 100), rounded down',
          `= ${money(available, { cents: true })} ÷ (${strikeText} × 100)`,
          `= ${row.contracts} contract${row.contracts === 1 ? '' : 's'}`,
        ])
      : formula(['NO QUALIFYING CSP: no strike, so nothing to size.']);
    tr.appendChild(qtyCell);

    const cpcCell = el('td', { class: 'num' }, sized ? money(row.cash_per_contract) : '—');
    cpcCell.title = sized
      ? formula([
          'Cash / Contract = CSP TO ENTER strike × 100',
          `= ${strikeText} × 100`,
          `= ${money(row.cash_per_contract, { cents: true })}`,
          'Collateral one put at that strike ties up.',
        ])
      : formula(['NO QUALIFYING CSP: no strike, so no collateral to show.']);
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

// The math behind one row's running_profit_target: the highest of this
// row's cost basis, breakeven (the lots' own) and wheel breakeven,
// cushioned -- the same three-way floor as the summary's Profit Target and
// wheel/api.py's per-row loop, so this never re-derives which one wins,
// only formats the numbers the backend already picked between. `fmt` lets
// the chart (priceLabel) and the ledger table (perShare) share this one
// source. This is the Profit Target concept only -- CC TO EXIT and
// CSP TO ENTER have no historical per-row series.
function runningProfitTargetFormulaLines(row, entry, fmt) {
  const inputs = [
    ['cost basis', row.running_cost_basis],
    ['breakeven', row.running_lot_break_even],
    ['wheel breakeven', row.running_break_even],
  ].filter(([, value]) => typeof value === 'number');
  const [winner, floorValue] = inputs.reduce((best, item) => (item[1] > best[1] ? item : best), inputs[0]);
  return [
    `Profit Target = max(${inputs.map(([label]) => label).join(', ')}) × ${(1 + entry.profit_target_cushion_pct / 100).toFixed(2)}, rounded up to $0.50`,
    `Floor = max(${inputs.map(([, value]) => fmt(value)).join(', ')}) = ${fmt(floorValue)} (${winner})`,
    `= ${fmt(floorValue)} × ${(1 + entry.profit_target_cushion_pct / 100).toFixed(2)} = ${fmt(row.running_profit_target)}`,
  ];
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
  const legend = $('legend-tradelog-breakeven');
  if (legend) clear(legend);
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
  // Profit Target only -- set alongside running_break_even server-side
  // (wheel/api.py), so this is numeric everywhere `be` is -- except the rare
  // case where the "now" pin couldn't compute a target for the very last
  // row; handled as a gap below. CC TO EXIT / CSP TO ENTER have
  // no historical series, so there's nothing else to plot here.
  const target = rows.map((r) => r.running_profit_target);
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
  // Target is always >= break-even by construction, so folding it into the
  // same outlier-clamped domain only ever raises `hi`.
  const targetWithin = target.filter((v) => typeof v === 'number' && Math.abs(v - med) <= 6 * mad);
  let lo = Math.min(...within, ...targetWithin, cur === null ? Infinity : cur);
  let hi = Math.max(...within, ...targetWithin, cur === null ? -Infinity : cur);
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
  const lastVal = be[be.length - 1];
  let lastTargetIdx = -1;
  for (let i = target.length - 1; i >= 0; i--) {
    if (typeof target[i] === 'number') {
      lastTargetIdx = i;
      break;
    }
  }

  // ---- under-water / cushion state ----------------------------------------
  // "Under water" = the stock now sits BELOW this wheel's current break-even,
  // so selling here books a loss. "Above water" = at or above it, a
  // realized-if-sold cushion. Shade the band between the two references and
  // badge it -- a red ⚠ warning under water, a green 🙂 above.
  let water = null; // 'under' | 'above' | null
  if (cur !== null && Number.isFinite(lastVal)) {
    water = cur < lastVal ? 'under' : 'above';
    const waterColor = water === 'under' ? cssVar('--neg') : cssVar('--good');
    const yNow = clampY(cur);
    const yBe = clampY(lastVal);
    group.appendChild(
      svgEl('rect', {
        x: margin.left,
        y: Math.min(yNow, yBe),
        width: plotWidth,
        height: Math.max(Math.abs(yNow - yBe), 1),
        fill: waterColor,
        'fill-opacity': water === 'under' ? 0.16 : 0.1,
      })
    );
    const gap = Math.abs(cur - lastVal);
    // Total dollars across the shares actually held, not the per-share price
    // gap -- "$0.98 above break-even" reads as trivial when it's really
    // hundreds of dollars across a real position. The % is that gap as a
    // share of the break-even price itself (a return-on-cost reading).
    const gapTotal = gap * (entry.shares_held || 0);
    const gapPct = lastVal ? (100 * gap) / Math.abs(lastVal) : null;
    const icon = water === 'under' ? '⚠' : '🙂';
    group.appendChild(
      svgEl(
        'text',
        {
          class: `be-water-badge be-water-${water}`,
          x: margin.left + 8,
          y: margin.top + 18,
          'text-anchor': 'start',
        },
        `${icon} ${money(gapTotal)} (${pct(gapPct)}) ${water === 'under' ? 'below' : 'above'} break-even`
      )
    );
  }

  // ---- distance to profit target ---------------------------------------------------
  // Same $ + % readout, against the Profit Target line instead of Break-even,
  // and the same red/green treatment (reusing .be-water-under/.be-water-above):
  // red while still short of it, green with a distinct 🎯 once reached. Only
  // drawn where a target exists (see the chart caveat: CSP-phase wheels have
  // no historical target line, so no badge either). Stacks under the
  // break-even badge when both are shown.
  if (cur !== null && lastTargetIdx >= 0) {
    const targetVal = target[lastTargetIdx];
    const atTarget = cur >= targetVal;
    const tGap = Math.abs(cur - targetVal);
    const tGapTotal = tGap * (entry.shares_held || 0);
    const tGapPct = targetVal ? (100 * tGap) / Math.abs(targetVal) : null;
    group.appendChild(
      svgEl(
        'text',
        {
          class: `be-water-badge ${atTarget ? 'be-water-above' : 'be-water-under'}`,
          x: margin.left + 8,
          y: margin.top + (water !== null ? 36 : 18),
          'text-anchor': 'start',
        },
        `${atTarget ? '🎯' : '↗'} ${money(tGapTotal)} (${pct(tGapPct)}) ${atTarget ? 'above' : 'below'} profit target`
      )
    );
  }

  // Current stock price -- the gap between this and the line is what is left to
  // recover. Drawn red and bold so "where we are now" reads at a glance.
  if (cur !== null && cur >= lo && cur <= hi) {
    const cy = y(cur);
    group.appendChild(
      svgEl('line', {
        x1: margin.left,
        x2: margin.left + plotWidth,
        y1: cy,
        y2: cy,
        stroke: cssVar('--neg'),
        'stroke-width': 2.5,
        'stroke-dasharray': '6 3',
      })
    );
    // Just the price -- the legend below already labels this line "Now".
    const nowX = margin.left + plotWidth + 4;
    group.appendChild(
      svgEl('text', { class: 'be-now-label', x: nowX, y: cy + 4, 'text-anchor': 'start' }, priceLabel(cur))
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
  const lastCy = clampY(lastVal);
  const targetCy = lastTargetIdx >= 0 ? clampY(target[lastTargetIdx]) : null;
  // Break-even's label position is unchanged from before this line existed
  // (it already coexists with the "now" price tag below it) -- when target
  // sits close to break-even in price, push target's label further up
  // instead, since target is always >= break-even (positive cushion) and so
  // already stacks above it.
  const beLabelY = lastCy - 8;
  let targetLabelY = targetCy !== null ? targetCy - 8 : null;
  if (targetLabelY !== null && beLabelY - targetLabelY < 14) {
    targetLabelY = beLabelY - 14;
  }
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
        // .be-value, not the shared .chart-endpoint-value: this label is its
        // own line's color (blue), not the violet "key figure" treatment --
        // now that the Target line also ends here, sharing violet with it
        // made the two endpoints unreadable together.
        class: 'chart-endpoint-value be-value',
        x: Math.min(lastCx + 8, margin.left + plotWidth + margin.right - 4),
        y: beLabelY,
        'text-anchor': lastCx + 8 > margin.left + plotWidth ? 'end' : 'start',
      },
      priceLabel(lastVal)
    )
  );

  // ---- profit target line ---------------------------------------------------------
  // Only drawn where a target exists (the wheel's CC/share-holding history --
  // CSP TO ENTER and CC TO EXIT are current-state numbers, not
  // something the ledger can reconstruct historically). Split into
  // contiguous segments so a stray gap can't corrupt the path.
  const targetColor = cssVar('--target');
  const targetSegments = [];
  let seg = [];
  target.forEach((v, i) => {
    if (typeof v === 'number') {
      seg.push(i);
    } else if (seg.length) {
      targetSegments.push(seg);
      seg = [];
    }
  });
  if (seg.length) targetSegments.push(seg);

  for (const idxs of targetSegments) {
    group.appendChild(
      svgEl('path', {
        d: 'M' + idxs.map((i) => `${centers[i]},${y(target[i])}`).join('L'),
        fill: 'none',
        stroke: targetColor,
        'stroke-width': 2,
        'stroke-dasharray': '5 3',
        'stroke-linejoin': 'round',
        'stroke-linecap': 'round',
        'clip-path': `url(#${clipId})`,
      })
    );
  }

  rows.forEach((r, i) => {
    if (typeof target[i] !== 'number') return;
    if (target[i] >= lo && target[i] <= hi) {
      group.appendChild(svgEl('circle', { cx: centers[i], cy: y(target[i]), r: 2, fill: targetColor }));
    }
    const hit = svgEl('circle', { cx: centers[i], cy: clampY(target[i]), r: 7, fill: 'transparent' });
    attachTip(
      hit,
      `${r.type} · ${r.date}`,
      [
        { label: 'Profit Target after this fill', value: priceLabel(target[i]) },
        { label: 'Break-even after this fill', value: priceLabel(r.running_break_even) },
      ],
      formula(runningProfitTargetFormulaLines(r, entry, priceLabel))
    );
    group.appendChild(hit);
  });

  if (lastTargetIdx >= 0) {
    const tcx = centers[lastTargetIdx];
    group.appendChild(
      svgEl('circle', {
        cx: tcx,
        cy: targetCy,
        r: 4,
        fill: targetColor,
        stroke: cssVar('--surface-1'),
        'stroke-width': 2,
      })
    );
    group.appendChild(
      svgEl(
        'text',
        {
          class: 'chart-endpoint-value',
          x: Math.min(tcx + 8, margin.left + plotWidth + margin.right - 4),
          y: targetLabelY,
          'text-anchor': tcx + 8 > margin.left + plotWidth ? 'end' : 'start',
        },
        priceLabel(target[lastTargetIdx])
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
      (lastTargetIdx >= 0 ? `, with a profit target line ending at ${priceLabel(target[lastTargetIdx])}` : '') +
      (cur !== null
        ? `, with the stock now at ${priceLabel(cur)} (${water === 'under' ? 'under water' : 'above water'}).`
        : '.')
  );

  if (legend) {
    const swatchItem = (colorVar, label, value) => {
      const item = el('span');
      const swatch = el('i', { class: 'line' });
      swatch.style.background = colorVar;
      item.appendChild(swatch);
      item.appendChild(document.createTextNode(label));
      item.appendChild(el('b', { class: 'legend-value' }, value));
      return item;
    };
    legend.appendChild(swatchItem(lineColor, 'Break-even', priceLabel(lastVal)));
    if (lastTargetIdx >= 0) {
      legend.appendChild(swatchItem(targetColor, 'Profit Target', priceLabel(target[lastTargetIdx])));
    }
    if (cur !== null) {
      legend.appendChild(swatchItem(cssVar('--neg'), 'Now', priceLabel(cur)));
    }
  }
}

/* ------------------------------------------------ chart: wheel timeline (trade log) */

// Arrow label for a roll's `direction` (see wheel.engine.Roll.direction):
// strike move first, then whether the expiry pushed out.
const ROLL_DIRECTION_LABEL = {
  UP: 'Rolled up',
  DOWN: 'Rolled down',
  FLAT: 'Rolled, same strike',
  OUT: 'Rolled out',
  OUT_AND_UP: 'Rolled up and out',
  OUT_AND_DOWN: 'Rolled down and out',
  UNKNOWN: 'Rolled',
};

/**
 * One leg bar on the Trade Log's Wheel timeline chart -- same visual
 * vocabulary as the Dashboard's Wheel timelines chart (drawTimeline): a
 * rounded bar over the days held, colored by strategy, ▲ at sell/buy-to-open
 * and ▼ (or ◆ if assigned) at the close. `leg` is one entry from a wheel's
 * `legs` -- the same shape `leg_rows()` feeds the Dashboard's own
 * `cycle.legs`, just every leg this wheel ever had, not only the filtered
 * cycle list's.
 */
function drawTimelineLeg(group, leg, y, laneHeight, x, through, surface) {
  const style = LEG_STYLES[leg.strategy] || LEG_STYLES.CSP;
  const color = cssVar(style.varName);
  const endIso = leg.close_date || through;
  const x1 = x(leg.open_date);
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
      'fill-opacity': leg.remaining > 0 ? 0.45 : 0.9,
      stroke: surface,
      'stroke-width': 1.5,
    })
  );
  // ▲ sold/bought to open.
  group.appendChild(
    svgEl('path', {
      d: `M${x1},${mid - 4.5}L${x1 + 3.6},${mid + 2}L${x1 - 3.6},${mid + 2}Z`,
      fill: color,
      stroke: surface,
      'stroke-width': 1.5,
    })
  );
  // ▼ bought/sold to close, or ◆ assigned.
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
  // Strike, sitting right above the bar -- the one thing worth reading at a
  // glance without hovering (dates and premium are tooltip-only). The lane
  // pitch (see LANE_PITCH in drawTradeLogTimeline) leaves a gutter above
  // every bar just for this, so it never runs into the lane above. Skipped
  // on a bar too narrow to hold it without overlapping its neighbors; still
  // in the tooltip either way.
  if (x2 - x1 >= 28) {
    group.appendChild(
      svgEl(
        'text',
        { class: 'tick-label', x: (x1 + x2) / 2, y: y - 2, 'text-anchor': 'middle' },
        leg.strike != null ? '$' + leg.strike : ''
      )
    );
  }

  const hit = svgEl('rect', { class: 'hit', x: x1 - 8, y: y - 2, width: x2 - x1 + 16, height: laneHeight + 4 });
  attachTip(hit, leg.symbol, [
    { label: 'Strategy', value: style.label, color },
    { label: 'Opened', value: `${leg.open_date} · ${leg.contracts}x @ ${leg.open_price ?? '—'}` },
    { label: leg.close_date ? 'Closed' : 'Status', value: leg.close_date ? `${leg.close_date} · ${leg.outcome}` : 'OPEN' },
    {
      label: 'Realized P/L',
      value: money(leg.realized_pl, { cents: true }),
      valueClass: leg.realized_pl > 0 ? 'pos' : leg.realized_pl < 0 ? 'neg' : '',
    },
    { label: 'Collateral', value: money(leg.collateral) },
  ]);
  group.appendChild(hit);
  return { x1, x2, mid };
}

/**
 * The whole wheel, graphically: every share lot and every option leg it
 * ever had, laid out on one shared time axis exactly like one row of the
 * Dashboard's Wheel timelines chart (same bars, same ▲▼◆■□ marks), plus a
 * dashed arrow wherever a roll closed one leg straight into the next --
 * the relationship the old "Roll chain" ledger column used to describe in
 * text (e.g. "RC1 (win)") and that nobody could really follow row by row.
 * One picture of the whole wheel: shares, puts, calls, and how the rolls
 * connect them. Hidden for a wheel with nothing to draw yet.
 */
function drawTradeLogTimeline(entry) {
  const host = $('tradelog-timeline');
  const svg = $('chart-tradelog-timeline');
  const legend = $('legend-tradelog-timeline');
  const legs = entry.legs || [];
  const lots = (entry.share_lots || []).filter((lot) => lot.shares > 0);
  if (legend) clear(legend);
  if (!legs.length && !lots.length) {
    host.hidden = true;
    clear(svg);
    return;
  }
  host.hidden = false;

  const through = (state.data && state.data.meta && state.data.meta.through) || entry.end_date || localIso(Date.now());
  const rollCreditOf = new Map((entry.roll_events || []).map((r) => [r.roll_id, r]));

  // BAR_SLOT is the bar's own height (drawTimelineLeg/drawTimelineStock's
  // usual box); LANE_PITCH is the vertical step between lanes -- taller
  // than BAR_SLOT on purpose, so each lane keeps a quiet gutter above its
  // own bar for the strike label, rather than that label landing in the
  // lane above (see slotTop below).
  const BAR_SLOT = 18;
  const LANE_PITCH = 30;
  const margin = { top: 14, right: 18, bottom: 30, left: 20 };
  const DAY = 86400000;
  const slotTop = (lane) => margin.top + lane * LANE_PITCH + (LANE_PITCH - BAR_SLOT);

  // Pack legs and share lots into sub-lanes so anything overlapping in time
  // (concurrent puts at different strikes, a put rolled the same day shares
  // are still held, ...) stays legible -- the same packing drawTimeline uses
  // per cycle row, just for this one wheel's full history.
  const items = [
    ...legs.map((leg) => ({
      kind: 'leg',
      leg,
      start: parseDay(leg.open_date).getTime(),
      finish: parseDay(leg.close_date || through).getTime(),
    })),
    ...lots.map((lot) => {
      const sells = (lot.disposals || []).map((d) => parseDay(d.date).getTime());
      const held = lot.remaining > 1e-9 || !sells.length;
      return {
        kind: 'stock',
        lot,
        start: parseDay(lot.acquired).getTime(),
        finish: held ? parseDay(through).getTime() : Math.max(...sells),
      };
    }),
  ].sort((a, b) => a.start - b.start);

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
  const lanes = Math.max(1, laneEnds.length);

  const width = chartWidth(svg, 520);
  const height = margin.top + lanes * LANE_PITCH + margin.bottom;
  const plotWidth = width - margin.left - margin.right;

  let tMin = Math.min(...items.map((item) => item.start));
  let tMax = Math.max(...items.map((item) => item.finish));
  // A wheel with just one same-day event (e.g. a single pre-history share
  // sale, nothing else) would otherwise divide by zero and collapse every
  // bar onto the same point -- pad the domain a day either side so there is
  // an actual axis to place it on, instead of an invisible zero-width chart.
  if (tMax === tMin) {
    tMin -= DAY;
    tMax += DAY;
  }
  const x = (iso) => margin.left + ((parseDay(iso).getTime() - tMin) / (tMax - tMin)) * plotWidth;

  clear(svg);
  svg.setAttribute('width', width);
  svg.setAttribute('height', height);
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  const group = svgEl('g');
  svg.appendChild(group);

  const surface = cssVar('--surface-1');
  const STATUS_COLOR = {
    OPEN: cssVar('--warning'),
    WIN: cssVar('--good'),
    LOSS: cssVar('--neg'),
    FLAT: cssVar('--text-muted'),
  };

  // Each leg's bar geometry, computed up front (pure math, nothing drawn
  // yet) so the roll connectors below can be painted *before* the bars
  // themselves -- SVG hit-tests in paint order, last on top, so without
  // this a connector's invisible hit-path (it has to be fairly wide to be
  // hoverable at all) could sit over a nearby bar and steal its hover/click.
  // Painting connectors first means a bar's own hit rect always wins
  // wherever the two overlap; the connector only responds in the open gap.
  const placedByLeg = new Map();
  placed.forEach(({ item, lane }) => {
    if (item.kind !== 'leg') return;
    const y = slotTop(lane);
    const x1 = x(item.leg.open_date);
    const x2 = Math.max(x(item.leg.close_date || through), x1 + 3);
    placedByLeg.set(item.leg.leg_id, { x1, x2, mid: y + (BAR_SLOT - 1) / 2 });
  });

  // Roll connectors: an arrow from the leg a roll closed to the leg it
  // opened, labeled with that roll's own net credit/debit and direction
  // (up/down/out) from `roll_events` -- the exact same figures the Roll
  // rate summary tile uses, not re-derived here. Colored by the chain's
  // own win/loss/open verdict (`leg.chain_status`, stamped server-side by
  // the same roll-chain detector the Roll rate tile scores).
  for (const leg of legs) {
    const rollIds = new Set((leg.closes || []).map((c) => c.roll_id).filter(Boolean));
    for (const rollId of rollIds) {
      const next = legs.find((candidate) => candidate.opened_by_roll === rollId);
      if (!next || next.leg_id === leg.leg_id) continue;
      const from = placedByLeg.get(leg.leg_id);
      const to = placedByLeg.get(next.leg_id);
      if (!from || !to) continue;
      const roll = rollCreditOf.get(rollId);
      const statusColor = STATUS_COLOR[leg.chain_status] || cssVar('--text-secondary');
      const ax1 = from.x2;
      const ax2 = Math.max(to.x1, ax1 + 4);
      const ay1 = from.mid;
      const ay2 = to.mid;
      // Same lane (the common case): a straight line. Different lane: a
      // gentle S-curve, its control-point offset capped to a fraction of
      // the gap so two legs rolled back-to-back (a few pixels apart,
      // common on a same-day roll) never loop back on themselves.
      const sameLane = Math.abs(ay1 - ay2) < 0.5;
      const off = Math.min(10, (ax2 - ax1) / 2);
      const d = sameLane
        ? `M${ax1},${ay1}L${ax2 - 3},${ay2}`
        : `M${ax1},${ay1}C${ax1 + off},${ay1} ${ax2 - off},${ay2} ${ax2 - 3},${ay2}`;
      const connector = svgEl('path', {
        d,
        fill: 'none',
        stroke: statusColor,
        'stroke-width': 1.5,
        'stroke-dasharray': '3 2',
        'stroke-opacity': 0.8,
      });
      group.appendChild(connector);
      group.appendChild(
        svgEl('path', {
          d: `M${ax2},${ay2}L${ax2 - 5},${ay2 - 3}L${ax2 - 5},${ay2 + 3}Z`,
          fill: statusColor,
          'fill-opacity': 0.8,
        })
      );
      const hit = svgEl('path', {
        class: 'hit',
        d,
        fill: 'none',
        stroke: 'transparent',
        'stroke-width': 7,
      });
      attachTip(
        hit,
        roll ? ROLL_DIRECTION_LABEL[roll.direction] || 'Rolled' : 'Rolled',
        [
          { label: 'Date', value: (roll && roll.date) || next.open_date },
          { label: 'Strike', value: `${leg.strike} → ${next.strike}` },
          { label: 'Expiry', value: `${leg.expiry} → ${next.expiry}` },
          ...(roll
            ? [
                {
                  label: 'Net credit',
                  value: money(roll.net_credit, { cents: true }),
                  valueClass: roll.net_credit > 0 ? 'pos' : roll.net_credit < 0 ? 'neg' : '',
                },
              ]
            : []),
        ]
      );
      group.appendChild(hit);
    }
  }

  // Bars drawn last, on top of the connectors, so their own hit rects
  // always win the hover/click wherever the two overlap (see the paint-
  // order note above placedByLeg).
  placed.forEach(({ item, lane }) => {
    const y = slotTop(lane);
    if (item.kind === 'stock') {
      drawTimelineStock(group, entry, item.lot, y, BAR_SLOT, x, through, surface, () => {});
    } else {
      drawTimelineLeg(group, item.leg, y, BAR_SLOT, x, through, surface);
    }
  });

  const axisY = margin.top + lanes * LANE_PITCH;
  const ticks = Math.min(6, items.length);
  group.appendChild(
    svgEl('line', { class: 'axis-line', x1: margin.left, x2: margin.left + plotWidth, y1: axisY, y2: axisY })
  );
  for (let i = 0; i <= ticks; i += 1) {
    const time = tMin + ((tMax - tMin) * i) / ticks;
    group.appendChild(
      svgEl(
        'text',
        { class: 'tick-label', x: margin.left + (plotWidth * i) / ticks, y: axisY + 16, 'text-anchor': 'middle' },
        dayLabel(localIso(time))
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
  if (lots.length) {
    const stockItem = el('span');
    const stockSwatch = el('i');
    stockSwatch.style.background = cssVar('--series-2');
    stockItem.appendChild(stockSwatch);
    stockItem.appendChild(document.createTextNode('Shares held'));
    legend.appendChild(stockItem);
  }
  legend.appendChild(
    el('span', {}, '▲ open  ▼ close  ◆ assign  ■ buy  □ sell  ➜ rolled into next leg')
  );
  legend.appendChild(el('span', {}, 'Faded = still open'));
  if (legs.some((leg) => leg.chain_id)) {
    const statusItem = el('span');
    statusItem.appendChild(document.createTextNode('Roll: '));
    for (const [status, label] of [
      ['WIN', 'win'],
      ['LOSS', 'loss'],
      ['OPEN', 'open'],
      ['FLAT', 'flat'],
    ]) {
      const dot = el('i');
      dot.style.background = STATUS_COLOR[status];
      dot.style.borderRadius = '50%';
      dot.style.width = '8px';
      dot.style.height = '8px';
      statusItem.append(dot, document.createTextNode(' ' + label + '   '));
    }
    legend.appendChild(statusItem);
  }
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
  // Untracked shares count too: a wheel whose shares were all bought before
  // the oldest loaded export (0 tracked) is still holding and writing calls.
  const totalShares = (entry.shares_held || 0) + (entry.shares_untracked || 0);
  if (totalShares > 1e-9) {
    return {
      cls: 'cc',
      spin: true,
      label: 'Covered Call',
      sub: `Holding ${Math.round(totalShares).toLocaleString('en-US')} sh, writing calls against them.`,
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
      'Cash-Secured Put: no shares, short put open.',
      'Covered Call: shares held, short call open.',
      'Dormant: flat, nothing open; a new put resumes it.',
      'Closed: wheel finished.',
    ])
  );
}

/* --------------------------------------------------- directional trade
 * management: per-position settings the reader configures on the
 * directional (LC/LP) card -- a target DTE for a roll/exit review, an
 * optional stop-loss (dollars or % of cost basis), and an optional
 * underlying-price exit trigger -- plus the pure functions that turn those
 * settings and the card's own already-computed data (DTE, cost basis,
 * unrealized P&L, current price) into a readout and one summary Action
 * status. Settings persist via state.directionalManagement / data/
 * config.json (see buildConfigSnapshot/applyConfigSnapshot above);
 * nothing here ever recomputes P&L, delta, or exposure -- those stay
 * exactly what entry.directional already carries from wheel/api.py.
 *
 * These are reminders, never automatic trading instructions: reaching a
 * threshold flips a status badge, nothing more.
 */

const MANAGEMENT_DEFAULTS = Object.freeze({
  targetDte: 90, // the roll/exit review deadline -- "Management deadline"
  planRollDte: 120, // earlier "plan ahead" warning, before that deadline
  stopLossMode: null, // 'dollar' | 'percent' | null (not configured)
  stopLossValue: null, // dollars, or points of cost-basis % (50 = 50%)
  underlyingTrigger: null, // a stock/ETF price, or null (not configured)
});

/** Validates/clamps one cycle's saved settings from config.json -- never
 * trusts a hand-edited or stale file, same reasoning as every other field
 * applyConfigSnapshot() restores. Returns null only when `raw` isn't even
 * an object (caller then just leaves that cycle_id at the defaults). */
function sanitizeManagementSettings(raw) {
  if (!raw || typeof raw !== 'object') return null;
  const out = { ...MANAGEMENT_DEFAULTS };
  if (Number.isFinite(raw.targetDte) && raw.targetDte > 0) out.targetDte = Math.round(raw.targetDte);
  if (Number.isFinite(raw.planRollDte) && raw.planRollDte > 0) out.planRollDte = Math.round(raw.planRollDte);
  if (raw.stopLossMode === 'dollar' || raw.stopLossMode === 'percent') out.stopLossMode = raw.stopLossMode;
  if (Number.isFinite(raw.stopLossValue) && raw.stopLossValue >= 0) out.stopLossValue = raw.stopLossValue;
  if (Number.isFinite(raw.underlyingTrigger) && raw.underlyingTrigger > 0) out.underlyingTrigger = raw.underlyingTrigger;
  // A mode with no value (or vice versa) isn't a configured stop -- treat it
  // as not set rather than guessing which half the reader meant.
  if (!out.stopLossMode || out.stopLossValue === null) {
    out.stopLossMode = null;
    out.stopLossValue = null;
  }
  // The deadline must come before the early warning, or "plan roll" could
  // never fire -- swap a reversed pair back into a usable order instead of
  // silently disabling the feature.
  if (out.targetDte > out.planRollDte) {
    const swap = out.targetDte;
    out.targetDte = out.planRollDte;
    out.planRollDte = swap;
  }
  return out;
}

function getManagementSettings(cycleId) {
  return { ...MANAGEMENT_DEFAULTS, ...(state.directionalManagement[cycleId] || {}) };
}

/** Merges `patch` onto this cycle's current settings, sanitizes the result
 * (so a bad input value can't corrupt the saved file), and persists it. */
function setManagementSetting(cycleId, patch) {
  const merged = sanitizeManagementSettings({ ...getManagementSettings(cycleId), ...patch });
  state.directionalManagement[cycleId] = merged || { ...MANAGEMENT_DEFAULTS };
  queueConfigSave();
}

/** 'due' (deadline arrived/passed), 'plan' (approaching it), or 'hold'. */
function directionalLegDteTier(daysToExpiry, settings) {
  if (daysToExpiry <= settings.targetDte) return 'due';
  if (daysToExpiry <= settings.planRollDte) return 'plan';
  return 'hold';
}

/**
 * The one summary Action status for a directional position, plus the
 * detail each readout cell needs. Priority, per the spec: a reached
 * stop-loss or underlying trigger (EXIT_REVIEW) outranks an arrived DTE
 * deadline (ROLL_EXIT_DUE), which outranks the early "plan ahead" warning
 * (PLAN_ROLL); an unconfigured stop-loss/trigger can never count as
 * reached (sanitizeManagementSettings already keeps an unset one at
 * `null`/`not_set`, never a value this function could mistake for zero).
 */
function directionalActionStatus(entry, settings) {
  const d = entry.directional;
  const openLegs = d.legs.filter((leg) => leg.is_open);

  const dueLegs = [];
  const planLegs = [];
  for (const leg of openLegs) {
    const tier = directionalLegDteTier(leg.days_to_expiry, settings);
    if (tier === 'due') dueLegs.push(leg);
    else if (tier === 'plan') planLegs.push(leg);
  }
  // Every open leg shares the same configured target DTE, so the leg with
  // the fewest days to expiry is always the one whose own review date
  // (expiration - target DTE) comes first -- no need to compute every
  // leg's review date just to find the earliest.
  const soonestLeg = openLegs.length
    ? openLegs.reduce((a, b) => (a.days_to_expiry <= b.days_to_expiry ? a : b))
    : null;

  let stopLossStatus = 'not_set';
  let allowedLoss = null;
  let currentLoss = null;
  let remainingAllowance = null;
  let estimatedValueAtThreshold = null;
  if (settings.stopLossMode && settings.stopLossValue !== null && d.total_cost_basis_open) {
    allowedLoss =
      settings.stopLossMode === 'percent'
        ? d.total_cost_basis_open * (settings.stopLossValue / 100)
        : settings.stopLossValue;
    estimatedValueAtThreshold = d.total_cost_basis_open - allowedLoss;
    if (d.total_unrealized_pl !== null) {
      // 0 while flat or profitable -- nothing has been lost yet, so the
      // full configured allowance is still available (never a negative
      // "loss" just because the position is up).
      currentLoss = Math.max(0, -d.total_unrealized_pl);
      remainingAllowance = Math.max(0, allowedLoss - currentLoss);
      stopLossStatus = currentLoss >= allowedLoss - 1e-9 ? 'reached' : 'within_limit';
    }
  }

  // A call is reviewed if the stock falls below the trigger; a put, if it
  // rises above -- the direction that threatens each thesis. Skipped for a
  // mixed (MULTI) position, where "below"/"above" has no single meaning.
  let triggerReached = false;
  if (settings.underlyingTrigger !== null && d.current_price !== null && d.position_type !== 'MULTI') {
    triggerReached =
      d.position_type === 'LP'
        ? d.current_price > settings.underlyingTrigger
        : d.current_price < settings.underlyingTrigger;
  }

  const exitTriggered = stopLossStatus === 'reached' || triggerReached;
  let status;
  if (exitTriggered) status = 'EXIT_REVIEW';
  else if (dueLegs.length) status = 'ROLL_EXIT_DUE';
  else if (planLegs.length) status = 'PLAN_ROLL';
  else status = 'HOLD';

  return {
    status,
    soonestLeg,
    dueLegs,
    planLegs,
    stopLossStatus,
    allowedLoss,
    currentLoss,
    remainingAllowance,
    estimatedValueAtThreshold,
    triggerReached,
  };
}

const ACTION_STATUS_LABEL = {
  HOLD: 'Hold',
  PLAN_ROLL: 'Plan roll',
  EXIT_REVIEW: 'Exit review',
  ROLL_EXIT_DUE: 'Roll / exit due',
};
const ACTION_STATUS_TONE = { HOLD: undefined, PLAN_ROLL: 'warn', EXIT_REVIEW: 'neg', ROLL_EXIT_DUE: 'neg' };

/**
 * The Trade Management section of the directional card: DTE/management
 * deadline, an optional stop-loss, an optional underlying-price exit
 * trigger, and the one summary Action status those three feed into. Reads
 * `entry.directional` (cost basis, unrealized P&L, current price, each
 * open leg's own DTE) but never changes any of it -- every number here is
 * either a reminder the reader configured or a plain comparison against
 * numbers already on the card. Every input re-renders the whole card on
 * change (`rerender`, which redraws into the same host this card was drawn
 * in -- the main grid or the wheel card's extra section), the same cheap client-side
 * redraw the rest of the dashboard already uses for a settings change --
 * no refetch, `entry` is already in memory.
 */
function renderDirectionalManagement(host, entry, section, rerender) {
  const d = entry.directional;
  const cycleId = entry.cycle_id;
  const settings = getManagementSettings(cycleId);
  const info = directionalActionStatus(entry, settings);
  const openLegs = d.legs.filter((leg) => leg.is_open);
  const oneOpenLeg = openLegs.length === 1 ? openLegs[0] : null;
  const signed = (v) => money(v, { cents: true, sign: true });
  const perShare = (v) => (v === null || v === undefined ? '—' : '$' + v.toFixed(2));
  const numInput = (value, extra = {}) =>
    el('input', { type: 'number', class: 'tl-mgmt-input', inputmode: 'decimal', value: value === null ? '' : String(value), ...extra });

  section('Trade Management');

  // ---- DTE ------------------------------------------------------------
  const distinctDays = [...new Set(openLegs.map((leg) => leg.days_to_expiry))].sort((a, b) => a - b);
  host.appendChild(
    tradeLogCell('DTE', info.soonestLeg ? `${info.soonestLeg.days_to_expiry} days` : 'Position closed', {
      foot: distinctDays.length > 1 ? `other open legs: ${distinctDays.slice(1).join(', ')} days` : null,
      help: formula([
        'Calendar days to expiration (the soonest, if several).',
      ]),
    })
  );

  // ---- Management deadline (editable) ----------------------------------
  // Two separate thresholds, both counting the same DTE down from the
  // expiration date -- shown in the order they're actually reached as
  // time passes: the early warning (a larger DTE, reached first) fires
  // "Plan roll"; the review deadline (a smaller DTE, reached later, closer
  // to expiration) fires "Roll / exit due" -- see directionalActionStatus.
  // Each input carries its own plain-English label so the two are never
  // confused for the same setting.
  {
    const planInput = numInput(settings.planRollDte, { min: '1', step: '1', 'aria-label': 'Early-warning DTE' });
    const targetInput = numInput(settings.targetDte, { min: '1', step: '1', 'aria-label': 'Review-deadline DTE' });

    const planField = el('label', { class: 'tl-mgmt-field' }, 'Start watching at ');
    planField.appendChild(planInput);
    planField.appendChild(el('span', {}, ' DTE'));

    const targetField = el('label', { class: 'tl-mgmt-field' }, 'Roll / exit by ');
    targetField.appendChild(targetInput);
    targetField.appendChild(el('span', {}, ' DTE'));

    const row = el('div', { class: 'tl-mgmt-row' });
    row.appendChild(planField);
    row.appendChild(targetField);

    planInput.addEventListener('change', () => {
      const n = parseInt(planInput.value, 10);
      if (Number.isFinite(n) && n > 0) setManagementSetting(cycleId, { planRollDte: n });
      rerender();
    });
    targetInput.addEventListener('change', () => {
      const n = parseInt(targetInput.value, 10);
      if (Number.isFinite(n) && n > 0) setManagementSetting(cycleId, { targetDte: n });
      rerender();
    });

    host.appendChild(
      tradeLogCell('Management deadline', `${settings.targetDte} DTE`, {
        extra: row,
        help: formula([
          '"Start watching": early heads-up. "Roll / exit by": deadline to act.',
          'Reminders only, not automatic trades.',
          'Defaults: watch at 120 DTE, roll/exit by 90 DTE.',
        ]),
      })
    );
  }

  // ---- Review date -------------------------------------------------------
  {
    const reviewDate = info.soonestLeg
      ? localIso(parseDay(info.soonestLeg.expiration).getTime() - settings.targetDte * 86400000)
      : null;
    host.appendChild(
      tradeLogCell('Review date', reviewDate || '—', {
        foot: reviewDate ? `${settings.targetDte} days before the ${info.soonestLeg.expiration} expiration` : 'no open contract',
        help: formula(['= Expiration date - Target DTE']),
      })
    );
  }

  // ---- Stop-loss (editable) ----------------------------------------------
  {
    const modeSelect = el('select', { class: 'tl-mgmt-select', 'aria-label': 'Stop-loss type' });
    modeSelect.appendChild(el('option', { value: 'dollar' }, 'Dollar amount'));
    modeSelect.appendChild(el('option', { value: 'percent' }, '% of cost basis'));
    modeSelect.value = settings.stopLossMode || 'dollar';
    const valueInput = numInput(settings.stopLossValue, {
      min: '0',
      step: settings.stopLossMode === 'percent' ? '0.5' : '1',
      'aria-label': 'Stop-loss value',
    });
    const clearBtn = el('button', { type: 'button' }, 'Clear');
    const row = el('div', { class: 'tl-mgmt-row' });
    row.appendChild(modeSelect);
    row.appendChild(valueInput);
    row.appendChild(clearBtn);
    const apply = () => {
      const n = parseFloat(valueInput.value);
      if (Number.isFinite(n) && n >= 0) {
        setManagementSetting(cycleId, { stopLossMode: modeSelect.value, stopLossValue: n });
      }
      rerender();
    };
    modeSelect.addEventListener('change', apply);
    valueInput.addEventListener('change', apply);
    clearBtn.addEventListener('click', () => {
      setManagementSetting(cycleId, { stopLossMode: null, stopLossValue: null });
      rerender();
    });

    const thresholdValue =
      info.allowedLoss === null
        ? 'Not set'
        : [signed(-info.allowedLoss), `${pct((100.0 * info.allowedLoss) / d.total_cost_basis_open)} of cost`];
    host.appendChild(
      tradeLogCell('Stop-loss threshold', thresholdValue, {
        extra: row,
        tone: info.stopLossStatus === 'reached' ? 'neg' : undefined,
        foot:
          info.estimatedValueAtThreshold === null
            ? null
            : `market value at threshold ≈ ${money(info.estimatedValueAtThreshold)}`,
        help: formula([
          'Configured max loss, in $ and as % of cost basis.',
          '"Not set": no stop-loss configured. A reminder, never an auto-sell.',
        ]),
      })
    );
  }

  // ---- Distance to stop ---------------------------------------------------
  {
    let value = 'Not set';
    let tone;
    let foot = null;
    if (info.allowedLoss !== null) {
      if (info.currentLoss === null) {
        value = 'Unavailable';
        foot = 'no current option market value';
      } else if (info.stopLossStatus === 'reached') {
        value = 'Threshold reached';
        tone = 'neg';
        foot = `already ${signed(-info.currentLoss)} of the ${signed(-info.allowedLoss)} allowed`;
      } else {
        value = [money(info.remainingAllowance), `${pct((100.0 * info.remainingAllowance) / info.allowedLoss)} of the limit left`];
        tone = 'warn';
      }
    }
    host.appendChild(
      tradeLogCell('Distance to stop', value, {
        tone,
        foot,
        help: formula([
          'Loss remaining before the stop-loss is hit.',
          'Reads "Threshold reached" instead of going negative.',
        ]),
      })
    );
  }

  // ---- Underlying exit trigger (editable) --------------------------------
  {
    const triggerInput = numInput(settings.underlyingTrigger, { min: '0.01', step: '0.01', 'aria-label': 'Underlying exit trigger price' });
    const clearBtn = el('button', { type: 'button' }, 'Clear');
    const row = el('div', { class: 'tl-mgmt-row' });
    row.appendChild(triggerInput);
    row.appendChild(clearBtn);
    triggerInput.addEventListener('change', () => {
      const n = parseFloat(triggerInput.value);
      if (Number.isFinite(n) && n > 0) setManagementSetting(cycleId, { underlyingTrigger: n });
      rerender();
    });
    clearBtn.addEventListener('click', () => {
      setManagementSetting(cycleId, { underlyingTrigger: null });
      rerender();
    });

    const direction = d.position_type === 'LP' ? 'above' : 'below';
    const value =
      settings.underlyingTrigger === null
        ? 'Not set'
        : [perShare(settings.underlyingTrigger), `now ${perShare(d.current_price)}`];
    host.appendChild(
      tradeLogCell('Underlying exit trigger', value, {
        extra: row,
        tone: info.triggerReached ? 'neg' : undefined,
        foot:
          settings.underlyingTrigger === null
            ? d.position_type === 'MULTI'
              ? 'not available for a mixed call/put position'
              : null
            : info.triggerReached
            ? 'reached: review this position'
            : `reviews if the stock moves ${direction} this price`,
        help: formula([
          d.position_type === 'LP'
            ? 'Put: a stock price above this triggers a review.'
            : 'Call: a stock price below this triggers a review.',
          'Set by you; not derived from the strike.',
        ]),
      })
    );
  }

  // ---- Action status ------------------------------------------------------
  {
    const reasons = [];
    if (info.stopLossStatus === 'reached') reasons.push('stop-loss threshold reached');
    if (info.triggerReached) reasons.push('underlying exit trigger reached');
    if (!reasons.length && info.dueLegs.length) {
      reasons.push(
        `review deadline reached on ${info.dueLegs.map((leg) => `${perShare(leg.strike)} exp ${leg.expiration}`).join(', ')}`
      );
    }
    if (!reasons.length && info.planLegs.length) {
      reasons.push(
        `approaching review deadline on ${info.planLegs.map((leg) => `${perShare(leg.strike)} exp ${leg.expiration}`).join(', ')}`
      );
    }
    host.appendChild(
      tradeLogCell('Action status', ACTION_STATUS_LABEL[info.status], {
        tone: ACTION_STATUS_TONE[info.status],
        foot: reasons.length ? reasons : 'nothing configured needs attention right now',
        help: formula([
          'A reminder, never an automatic trade.',
          'Priority: stop-loss or underlying trigger, then DTE reminder;',
          '  a reached deadline outranks the "plan ahead" warning.',
        ]),
      })
    );
  }

  // ---- Roll / exit checklist ------------------------------------------------
  if (info.status === 'ROLL_EXIT_DUE' || info.status === 'EXIT_REVIEW') {
    const box = el('div', { class: 'tl-mgmt-checklist' });
    box.appendChild(el('strong', {}, 'Before rolling or exiting, check:'));
    const list = el('ul');
    [
      'Current option market value and unrealized P&L (above).',
      'Remaining extrinsic (time) value vs. intrinsic value (above).',
      'Current delta and delta-adjusted $ exposure (above).',
      oneOpenLeg && oneOpenLeg.bid !== null && oneOpenLeg.ask !== null
        ? `Liquidity: bid ${money(oneOpenLeg.bid)} / ask ${money(oneOpenLeg.ask)}` +
          (oneOpenLeg.bid_ask_spread_pct !== null ? ` (${pct(oneOpenLeg.bid_ask_spread_pct)} spread)` : '') +
          '; a wide spread costs more to trade.'
        : 'Liquidity and bid/ask spread; a wide spread costs more to trade.',
      'The cost and expiration of a replacement contract, if rolling.',
    ].forEach((text) => list.appendChild(el('li', {}, text)));
    box.appendChild(list);
    box.appendChild(
      el('p', { class: 'hint' }, 'These are considerations to review, not a recommendation to roll or exit.')
    );
    host.appendChild(box);
  }
}

/**
 * Trade Log summary card for a directional (LC/LP) cycle: a lone bought call
 * or put, never a cash-secured put or covered call (see Cycle.kind on the
 * backend -- this never re-derives "directional" on its own, it only reads
 * `entry.directional`). Laid out in the order an options trader scans a
 * position: identity/P&L, then exposure, then cost/breakeven, then risk,
 * then the optional Greeks -- each a `.tl-section` divider inside the same
 * `#tradelog-summary` grid the wheel card uses, so the two stay visually
 * consistent without sharing any wheel-specific cell.
 *
 * Every per-leg number (strike, expiration, breakeven, Greeks) is shown for
 * one leg only when there is exactly one open leg; a multi-leg position
 * (different strikes/expirations, or both a call and a put) instead gets a
 * compact table, one row per leg -- see _build_directional_card's own
 * comment on why those figures are never blended across legs. Only
 * money/exposure totals that are safe to sum (cost, P&L, delta-adjusted
 * exposure, capital committed) are shown in the top cells for a multi-leg
 * position.
 *
 * `host` defaults to the main `#tradelog-summary` grid -- a lone
 * directional cycle (`entry.kind === 'directional'`) takes that over
 * entirely, since there is no wheel data to show alongside it. A wheel (or
 * hold) cycle that also carries a long leg instead renders this into
 * `#tradelog-directional-extra`, its own section below the wheel's own
 * summary; `manageNote` is false there so it doesn't re-touch
 * `#tradelog-note`, which the wheel render already set.
 */
function renderDirectionalSummary(entry, { host = $('tradelog-summary'), manageNote = true } = {}) {
  clear(host);
  host.hidden = false;
  const d = entry.directional;
  const isClosed = d.status === 'CLOSED';
  host.classList.toggle('closed', isClosed);
  host.classList.remove('no-activity');

  const signed = (v) => money(v, { cents: true, sign: true });
  const perShare = (v) => (v === null || v === undefined ? '—' : '$' + v.toFixed(2));
  const openLegs = d.legs.filter((leg) => leg.is_open);
  const oneLeg = d.legs.length === 1 ? d.legs[0] : null;
  const oneOpenLeg = openLegs.length === 1 ? openLegs[0] : null;
  const section = (label) => host.appendChild(el('div', { class: 'tl-section' }, label));
  const typeLabel = { LC: 'Long call', LP: 'Long put', MULTI: 'Multiple legs' }[d.position_type] || d.position_type;

  // Reuses the exact breakeven formula/substitution the Open positions table
  // already shows for an LC/LP row (positionBreakevenMath), via a shim row
  // carrying the same field names it expects -- only when there is truly
  // one open leg, since that substitution is written for one strike/cost;
  // a multi-leg position still gets the overall figure (d.position_breakeven),
  // just with a plainer explanation instead of that one-leg substitution.
  const breakevenLines = oneOpenLeg
    ? positionBreakevenMath({ type: oneOpenLeg.type_code, strike: oneOpenLeg.strike, breakeven: oneOpenLeg.breakeven_price })
    : [];

  // ---- 1. Position & P&L ---------------------------------------------------
  section('Position & P&L');
  host.appendChild(tradeLogCell('Ticker', entry.underlying, { help: formula(['Underlying stock symbol for this position.']) }));
  host.appendChild(
    tradeLogCell('Name', entry.name || '—', { help: formula(['Issuer name, best-effort from the broker description.']) })
  );
  host.appendChild(
    tradeLogCell('Position', typeLabel, {
      help: formula(
        d.position_type === 'LP'
          ? [
              'Long put: the right to sell 100 shares per contract at the strike.',
              'Bought to profit if the stock falls, or to protect shares you hold.',
            ]
          : d.position_type === 'LC'
          ? ['Long call: the right to buy 100 shares per contract at the strike.', 'Bought to profit if the stock rises.']
          : [
              'More than one kind of leg: different strikes, different',
              'expirations, or both a call and a put. See the table below.',
            ]
      ),
    })
  );
  host.appendChild(
    tradeLogCell('Status', d.status === 'OPEN' ? 'Open' : 'Closed', {
      help: formula(['Open: at least one contract is still live.', 'Closed: every contract has been sold, expired, or exercised.']),
    })
  );
  if (oneLeg) {
    host.appendChild(
      tradeLogCell('Contracts', String(oneLeg.is_open ? oneLeg.contracts_open : oneLeg.contracts_opened), {
        foot: oneLeg.contracts_closed ? `${oneLeg.contracts_closed} of ${oneLeg.contracts_opened} closed` : null,
        help: formula(['1 contract = 100 shares of exposure.']),
      })
    );
    host.appendChild(tradeLogCell('Strike', perShare(oneLeg.strike), { help: formula(['The price written into the contract.']) }));
    host.appendChild(
      tradeLogCell('Expiration', oneLeg.expiration, {
        foot: oneLeg.is_open ? `${oneLeg.days_to_expiry} day${oneLeg.days_to_expiry === 1 ? '' : 's'} to go` : 'contract settled',
        help: formula(['The date this contract stops trading and settles.']),
      })
    );
    host.appendChild(
      tradeLogCell('Opened', oneLeg.open_date, {
        foot: `${oneLeg.days_held} day${oneLeg.days_held === 1 ? '' : 's'} held`,
        help: formula(['The date this contract was bought.']),
      })
    );
  } else {
    host.appendChild(
      tradeLogCell('Legs', isClosed ? `${d.total_leg_count}, all closed` : `${d.open_leg_count} open, ${d.total_leg_count} total`, {
        help: formula(['Each strike/expiration combination is its own leg, listed below.']),
      })
    );
    if (isClosed) {
      host.appendChild(
        tradeLogCell('Opened', d.first_open_date || '—', { help: formula(['The date the first contract was bought.']) })
      );
    }
  }
  if (isClosed) {
    host.appendChild(
      tradeLogCell('Closed', d.last_close_date || '—', {
        help: formula(['The date the last contract was sold, expired, or exercised.']),
      })
    );
    host.appendChild(
      tradeLogCell('Total P&L', signed(d.total_pl), {
        tone: toneOf(d.total_pl),
        foot: d.total_return_on_cost_pct === null ? null : `${pct(d.total_return_on_cost_pct)} of what was paid`,
        help: formula(['Profit or loss locked in across every contract, fees included.']),
      })
    );
    host.appendChild(
      tradeLogCell('Total paid', money(d.total_cost_basis_all), {
        foot: 'every contract bought, fees included',
        help: formula(['What was paid for all the contracts in this position, fees included.']),
      })
    );
    if (oneLeg) {
      host.appendChild(
        tradeLogCell('Avg cost / contract', money(oneLeg.cost_per_contract), {
          foot: `${money(oneLeg.cost_per_share)}/share`,
          help: formula(['Total cost of the contract(s) ÷ number of contracts.']),
        })
      );
    }
    // A closed position has no live exposure, breakeven, risk, or anything
    // left to manage, so those sections are left out rather than shown as
    // rows of dashes and $0.
    renderDirectionalLegTable(host, d, { isClosed });
    if (manageNote) renderDirectionalNote(entry);
    return;
  }
  host.appendChild(
    tradeLogCell('Underlying price', perShare(d.current_price), {
      help: formula(['Latest available price for the stock or ETF itself, not the option.']),
    })
  );
  {
    const mvFoot =
      d.total_market_value === null
        ? 'no live quote on hand'
        : d.market_value_is_live_mark
        ? 'from your last Positions upload'
        : 'estimated, no live quote';
    host.appendChild(
      tradeLogCell(
        'Options market value',
        d.total_market_value === null ? '—' : money(d.total_market_value),
        {
          foot: mvFoot,
          help: formula([
            'Current value of all open contracts (the options, not the stock).',
            'From your last uploaded Positions file, not a live feed.',
          ]),
        }
      )
    );
  }
  host.appendChild(
    tradeLogCell('Total P&L', signed(d.total_pl), {
      tone: toneOf(d.total_pl),
      foot: `${signed(d.total_realized_pl)} realized, ${d.total_unrealized_pl === null ? 'none open' : signed(d.total_unrealized_pl) + ' unrealized'}`,
      help: formula([
        'Total P&L = Realized (closed contracts) + Unrealized (open contracts',
        '  at current value, or cost if none is available)',
        `= ${money(d.total_realized_pl, { cents: true })} + ${
          d.total_unrealized_pl === null ? '$0' : money(d.total_unrealized_pl, { cents: true })
        }`,
      ]),
    })
  );

  // ---- 2. Exposure ----------------------------------------------------------
  section('Exposure');
  if (oneOpenLeg) {
    host.appendChild(
      tradeLogCell('Delta', oneOpenLeg.delta === null ? 'Unavailable' : oneOpenLeg.delta.toFixed(2), {
        foot:
          oneOpenLeg.delta === null
            ? oneOpenLeg.greeks_unavailable_reason
            : oneOpenLeg.implied_volatility_pct === null
            ? null
            : `model IV ${pct(oneOpenLeg.implied_volatility_pct)}`,
        help: formula([
          'Option price change per $1 move in the stock, per share.',
          'Black-Scholes estimate, not broker-quoted.',
        ]),
      })
    );
  }
  // A leg with no usable quote/IV contributes nothing to these two totals
  // (see _build_directional_card) -- never a guessed delta -- so a multi-leg
  // position with one leg missing shows a real but partial number, flagged
  // in the foot line rather than presented as the whole position's exposure.
  const deltaPartialFoot =
    openLegs.length > 1 && !d.net_delta_known_for_all_open_legs ? 'partial: delta missing on 1 or more legs' : null;
  host.appendChild(
    tradeLogCell(
      'Delta-adjusted shares',
      d.net_delta_adjusted_shares === null
        ? 'Unavailable'
        : d.net_delta_adjusted_shares.toLocaleString('en-US', { maximumFractionDigits: 1 }),
      {
        tone: toneOf(d.net_delta_adjusted_shares),
        foot: deltaPartialFoot,
        help: formula([
          'Delta-adjusted shares = Open contracts × 100 × Delta, summed over open legs',
          'Shifts with price, IV and time.',
        ]),
      }
    )
  );
  host.appendChild(
    tradeLogCell('Delta-adjusted $', d.net_delta_adjusted_dollars === null ? 'Unavailable' : signed(d.net_delta_adjusted_dollars), {
      tone: toneOf(d.net_delta_adjusted_dollars),
      foot: deltaPartialFoot,
      help: formula([
        'Delta-adjusted $ = Delta-adjusted shares × Underlying price',
        'Stock-equivalent exposure, not money invested or borrowed.',
      ]),
    })
  );
  if (d.exposure_to_capital_ratio !== null) {
    host.appendChild(
      tradeLogCell('Exposure / capital', d.exposure_to_capital_ratio.toFixed(2) + 'x', {
        foot: deltaPartialFoot,
        help: formula([
          'Exposure / capital = Delta-adjusted $ (sign dropped) ÷ Capital committed',
          'Stock-equivalent exposure per $1 paid in. Not borrowed money.',
        ]),
      })
    );
  }
  if (oneOpenLeg && oneOpenLeg.delta_pct_of_100_shares !== null) {
    host.appendChild(
      tradeLogCell('Delta vs. 100 sh', pct(oneOpenLeg.delta_pct_of_100_shares), {
        help: formula(['How this one contract compares to owning 100 shares outright.']),
      })
    );
  }
  if (d.position_type === 'LP' && d.net_delta_adjusted_shares !== null) {
    host.appendChild(
      tradeLogCell('Hedge direction', 'Offsets stock exposure', {
        help: formula([
          'Negative delta: gains as the stock falls, so it can offset',
          'losses on shares held elsewhere.',
        ]),
      })
    );
  }

  // ---- 3. Cost & breakeven ---------------------------------------------------
  section('Cost & breakeven');
  host.appendChild(
    tradeLogCell('Cost basis', money(d.total_cost_basis_open), {
      foot: 'still open, fees included',
      help: formula(['Paid for the contract(s), fees included.', 'Fixed once closed; only the open part can change.']),
    })
  );
  if (oneLeg) {
    host.appendChild(
      tradeLogCell('Avg cost / contract', money(oneLeg.cost_per_contract), {
        foot: `${money(oneLeg.cost_per_share)}/share`,
        help: formula(['Total cost of the contract(s) ÷ number of contracts.']),
      })
    );
  }
  host.appendChild(
    tradeLogCell('Unrealized P&L', d.total_unrealized_pl === null ? '—' : signed(d.total_unrealized_pl), {
      tone: toneOf(d.total_unrealized_pl),
      help: formula(['Options market value minus the open position’s cost basis.']),
    })
  );
  host.appendChild(
    tradeLogCell('Return on cost', d.total_unrealized_pl_pct === null ? '—' : pct(d.total_unrealized_pl_pct), {
      tone: toneOf(d.total_unrealized_pl_pct),
      help: formula(['= unrealized P&L ÷ cost basis of the open position.']),
    })
  );
  {
    // One overall breakeven for the whole position -- exact, not a rough
    // average, whenever every open leg shares the same option type and
    // expiration (see wheel/api.py's _position_breakeven: a cost-weighted
    // figure for same-strike fills, an exact piecewise root for different
    // strikes). Withheld only when legs mix calls and puts or carry
    // different expirations, where no single "at expiration" price exists
    // -- the contract table below still has each leg's own breakeven.
    const noSingleFigure = d.position_breakeven === null && openLegs.length > 0;
    host.appendChild(
      tradeLogCell('Breakeven at expiration', perShare(d.position_breakeven), {
        foot: noSingleFigure
          ? 'varies by leg; see the contract table below'
          : d.position_breakeven_distance === null
          ? null
          : `now ${perShare(d.current_price)}, ${perShare(Math.abs(d.position_breakeven_distance))} ${
              d.position_breakeven_distance >= 0 ? 'above' : 'short'
            }`,
        help: formula(
          noSingleFigure
            ? [
                'This position mixes calls and puts, or carries more than',
                'one expiration, so there is no single "at expiration"',
                'price for the whole position; see each leg’s own',
                'breakeven in the contract table below.',
              ]
            : [
                ...(breakevenLines.length
                  ? breakevenLines
                  : [
                      'Stock price at expiration where the whole position',
                      '(every open leg combined) exactly nets to $0.',
                    ]),
                'Before expiration the option can be worth more or less than this;',
                '  it also depends on time left and implied volatility.',
              ]
        ),
      })
    );
  }
  if (oneOpenLeg) {
    host.appendChild(
      tradeLogCell('Intrinsic value', oneOpenLeg.intrinsic_value === null ? '—' : money(oneOpenLeg.intrinsic_value), {
        help: formula(['What this contract would be worth if exercised right now.']),
      })
    );
    host.appendChild(
      tradeLogCell('Extrinsic value', oneOpenLeg.extrinsic_value === null ? '—' : money(oneOpenLeg.extrinsic_value), {
        foot: oneOpenLeg.extrinsic_value_pct === null ? null : `${pct(oneOpenLeg.extrinsic_value_pct)} of market value`,
        help: formula([
          'Time value = Market value - Intrinsic value',
          'Decays to $0 at expiration.',
        ]),
      })
    );
  }

  // ---- 4. Risk ----------------------------------------------------------------
  section('Risk');
  host.appendChild(
    tradeLogCell('Max loss', money(d.max_loss_remaining), {
      help: formula([
        'Max loss = Premium paid on open contract(s), fees included.',
        'A long option cannot lose more than that.',
      ]),
    })
  );
  host.appendChild(
    tradeLogCell('Capital committed', money(d.capital_committed), {
      foot: d.capital_committed_pct_of_account === null ? null : `${pct(d.capital_committed_pct_of_account)} of account value`,
      help: formula([
        'Capital committed = Premium paid on open contract(s).',
        'Not the stock exposure it controls.',
      ]),
    })
  );

  // ---- 4b. Trade Management ------------------------------------------------
  renderDirectionalManagement(host, entry, section, () => renderDirectionalSummary(entry, { host, manageNote }));

  // ---- 5. Additional Greeks -----------------------------------------------
  if (oneOpenLeg) {
    section('Additional Greeks');
    if (oneOpenLeg.delta === null) {
      host.appendChild(
        tradeLogCell('Greeks', 'Unavailable', {
          foot: oneOpenLeg.greeks_unavailable_reason || null,
          help: formula([
            'Greeks need a current stock price and implied volatility;',
            'one or both are unavailable, so none are shown.',
          ]),
        })
      );
    } else {
      host.appendChild(
        tradeLogCell('Theta / day', oneOpenLeg.theta_per_day === null ? '—' : signed(oneOpenLeg.theta_per_day), {
          tone: toneOf(oneOpenLeg.theta_per_day),
          help: formula([
            'Estimated value lost per day to time decay,',
            'price and IV held constant. Model estimate.',
          ]),
        })
      );
      host.appendChild(
        tradeLogCell('Gamma', oneOpenLeg.gamma === null ? '—' : oneOpenLeg.gamma.toFixed(4), {
          help: formula(['How much delta itself would shift for a $1 move in the stock.']),
        })
      );
      host.appendChild(
        tradeLogCell('Vega', oneOpenLeg.vega === null ? '—' : money(oneOpenLeg.vega), {
          help: formula(['Estimated value change for a 1 percentage point move in implied volatility.']),
        })
      );
      host.appendChild(
        tradeLogCell(
          'Implied volatility',
          oneOpenLeg.implied_volatility_pct === null ? '—' : pct(oneOpenLeg.implied_volatility_pct),
          {
            foot: oneOpenLeg.quote_last_trade ? `quote traded ${oneOpenLeg.quote_last_trade.slice(0, 10)}` : null,
            help: formula([
              'Expected move the market is pricing in, implied by this option’s price.',
            ]),
          }
        )
      );
    }
  }

  renderDirectionalLegTable(host, d, { isClosed });
  if (manageNote) renderDirectionalNote(entry);
}

/**
 * Per-leg table for a multi-leg directional position. A closed position
 * gets a short "how each contract ended" table (dates, cost, P&L) -- the
 * live columns (DTE, market value, delta, breakeven) have nothing to say
 * once every contract is gone. An open position keeps the live columns;
 * any already-closed leg in it shows dashes there, never a stale figure.
 */
function renderDirectionalLegTable(host, d, { isClosed }) {
  if (d.legs.length <= 1) return;
  const signed = (v) => money(v, { cents: true, sign: true });
  const perShare = (v) => (v === null || v === undefined ? '—' : '$' + v.toFixed(2));
  const dash = '—';

  const columns = isClosed
    ? [
        ['Type', (leg) => leg.type_code],
        ['Strike', (leg) => perShare(leg.strike)],
        ['Expiration', (leg) => leg.expiration],
        ['Contracts', (leg) => String(leg.contracts_opened)],
        ['Opened', (leg) => leg.open_date || dash],
        ['Closed', (leg) => leg.close_date || dash],
        ['Cost (row)', (leg) => money(leg.cost_basis_total)],
        ['P&L (row)', (leg) => signed(leg.total_pl)],
      ]
    : [
        ['Type', (leg) => leg.type_code],
        ['Strike', (leg) => perShare(leg.strike)],
        ['Expiration', (leg) => leg.expiration],
        ['DTE', (leg) => (leg.is_open ? String(leg.days_to_expiry) : dash)],
        ['Contracts', (leg) => String(leg.is_open ? leg.contracts_open : leg.contracts_opened)],
        ['Cost (row)', (leg) => money(leg.is_open ? leg.cost_basis_open : leg.cost_basis_total)],
        ['Mkt value (row)', (leg) => (leg.market_value === null ? dash : money(leg.market_value))],
        ['P&L (row)', (leg) => signed(leg.total_pl)],
        ['Delta', (leg) => (leg.delta === null ? dash : leg.delta.toFixed(2))],
        ['Delta $ (row)', (leg) => (leg.delta_adjusted_dollars === null ? dash : signed(leg.delta_adjusted_dollars))],
        // A closed leg's breakeven is a price it can no longer reach.
        ['Breakeven', (leg) => (leg.is_open ? perShare(leg.breakeven_price) : dash)],
      ];

  const wrap = el('div', { class: 'tl-leg-table-wrap' });
  wrap.appendChild(
    el(
      'p',
      { class: 'hint' },
      isClosed
        ? 'Cost and P&L are totals for each row’s contracts, fees included.'
        : 'Cost, Mkt value, P&L, and Delta $ are totals for this row’s contracts; Delta is per contract.'
    )
  );
  const table = el('table', { class: 'tl-leg-table' });
  const thead = el('thead');
  const headRow = el('tr');
  columns.forEach(([label]) => headRow.appendChild(el('th', {}, label)));
  thead.appendChild(headRow);
  table.appendChild(thead);
  const tbody = el('tbody');
  for (const leg of d.legs) {
    const tr = el('tr');
    columns.forEach(([, cell]) => tr.appendChild(el('td', {}, cell(leg))));
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  wrap.appendChild(table);
  host.appendChild(wrap);
}

function renderDirectionalNote(entry) {
  const note = $('tradelog-note');
  clear(note);
  if (entry.attribution_note) {
    note.hidden = false;
    note.appendChild(el('span', {}, entry.attribution_note));
  } else {
    note.hidden = true;
  }
}

function renderTradeLogSummary(entry) {
  const extraHost = $('tradelog-directional-extra');

  // A directional (LC/LP) cycle gets its own card layout entirely -- most of
  // the wheel grid below (cost basis/share, roll rate, win rate, ...) has no
  // meaning for a lone bought option, so it is not reused here with cells
  // blanked out; see renderDirectionalSummary. There is no wheel data at
  // all in this case, so nothing goes in the supplementary section either.
  if (entry.kind === 'directional' && entry.directional) {
    clear(extraHost);
    extraHost.hidden = true;
    renderDirectionalSummary(entry);
    return;
  }

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
    'Not a wheel, so wheel-return ratios do not apply.',
    'The P&L is still real.',
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
      help: formula(['Issuer name, best-effort from the broker description.']),
    })
  );
  host.appendChild(
    tradeLogCell('Status', statusLabel(entry.status), {
      help: formula([
        'ACTIVE: a contract or shares still open.',
        'NO ACTIVITY: flat, same calendar year as the latest trade; a new put or call resumes it.',
        'CLOSED: the year has turned, or the shares were called away.',
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
        "First trade → last trade, or 'current' while open, in calendar days.",
      ]),
    })
  );
  host.appendChild(
    tradeLogCell('Cost basis / share', perShare(entry.cost_basis_per_share), {
      help: formula([
        'Avg purchase price of shares held (raw 1099-B basis).',
        ...(entry.untracked_cost_per_share != null
          ? [
              `${Math.round(entry.shares_untracked).toLocaleString('en-US')} pre-history shares use your broker's avg cost of ${perShare(entry.untracked_cost_per_share)}.`,
            ]
          : []),
        'Dash: no shares held.',
        'Rounded to the cent; lot-level P&L math may differ by a few cents.',
      ]),
    })
  );
  host.appendChild(
    tradeLogCell('Break-even price', perShare(entry.break_even_price), {
      foot:
        entry.current_price === null || entry.break_even_price === null
          ? null
          : `now ${perShare(entry.current_price)}, ${perShare(
              Math.abs(entry.break_even_price - entry.current_price)
            )} ${entry.current_price >= entry.break_even_price ? 'above' : 'to go'}`,
      help: formula([
        'Price where the whole campaign nets to $0:',
        '  raw cost of shares held, less everything else banked or paid',
        '  (premium, fees, dividends, open options valued at expiry).',
        'Dash: no shares held.',
      ]),
    })
  );
  // Mirrors the Wheel price targets table exactly: CC TO EXIT and Profit
  // Target in the CC phase (same backend fields as that table's row), CSP TO
  // ENTER in the CSP phase (the ticker's same backend entry, cspToEnterFor)
  // -- so the numbers here always match the table, never a separately-derived
  // figure.
  if (entry.wheel_phase === 'cc') {
    const metTone =
      entry.profit_target != null && entry.current_price != null
        ? toneOf(entry.current_price - entry.profit_target)
        : undefined;
    host.appendChild(
      tradeLogCell('Profit Target', perShare(entry.profit_target), {
        tone: metTone,
        foot:
          entry.current_price === null || entry.profit_target === null
            ? null
            : `now ${perShare(entry.current_price)}, ${perShare(
                Math.abs(entry.profit_target - entry.current_price)
              )} ${entry.current_price >= entry.profit_target ? 'at/above' : 'to go'}`,
        help: formula([entry.profit_target_explanation || 'Not enough data yet (no shares and no price).']),
      })
    );
    if (entry.cc_to_exit_status && entry.cc_to_exit_status !== 'no_shares') {
      const detail = entry.cc_to_exit_detail || {};
      host.appendChild(
        tradeLogCell('CC TO EXIT', entry.cc_to_exit != null ? perShare(entry.cc_to_exit) : ccToExitText(entry), {
          tone: 'cc-target',
          // Same value and tooltip as every table's CC TO EXIT cell
          // (ccToExitCell); the foot just names the contract it points at.
          foot:
            entry.cc_to_exit != null && detail.delta != null
              ? `delta ${detail.delta.toFixed(2)}, ${detail.days_to_expiry} days`
              : null,
          card: entry.cc_to_exit_card,
        })
      );
    }
  } else if (entry.wheel_phase === 'csp') {
    const cspEntry = cspToEnterFor(entry.underlying);
    host.appendChild(
      tradeLogCell(
        'CSP TO ENTER',
        cspEntry && cspEntry.recommended_strike != null ? perShare(cspEntry.recommended_strike) : cspToEnterText(cspEntry),
        {
          tone: cspEntry ? 'cc-target' : undefined,
          // Same value and tooltip as every table's CSP TO ENTER cell
          // (cspToEnterCell); the foot just names the contract it points at.
          foot:
            cspEntry && cspEntry.recommended_strike != null
              ? `delta ${cspEntry.delta.toFixed(2)}, ${cspEntry.days_to_expiry} days`
              : null,
          card: cspEntry ? cspEntry.card : null,
          help: cspEntry ? null : 'Not enough data yet (no price).',
        }
      )
    );
  }
  {
    const totalShares = (entry.shares_held || 0) + (entry.shares_untracked || 0);
    host.appendChild(
      tradeLogCell('Shares held', totalShares.toLocaleString('en-US'), {
        help: formula(
          entry.shares_untracked
            ? [
                'Shares you hold now: your latest broker positions file,',
                'plus any trades in your history since it was downloaded.',
                `${Math.round(entry.shares_held || 0).toLocaleString('en-US')} with a known cost + `,
                `${Math.round(entry.shares_untracked).toLocaleString('en-US')} bought before your loaded history = ${Math.round(totalShares).toLocaleString('en-US')}.`,
                entry.untracked_cost_per_share != null
                  ? `Those ${Math.round(entry.shares_untracked).toLocaleString('en-US')} use your broker's average cost of ${perShare(entry.untracked_cost_per_share)}.`
                  : 'Cost basis and break-even count only the shares with a known cost.',
              ]
            : [
                'Shares still held from assignment(s),',
                'net of any sold or called away.',
                '0 once the wheel is flat.',
              ]
        ),
      })
    );
  }
  host.appendChild(
    tradeLogCell('Open contracts', String(entry.open_contracts || 0), {
      help: formula(['Short option contracts still open, summed across all legs.']),
    })
  );
  host.appendChild(
    tradeLogCell('Gross premium received', cents(entry.gross_premium_received), {
      help: formula([
        'Sum of credits on every sell-to-open (STO).',
        'Not net of buy-backs.',
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
      help: formula(['Sum of fees + commissions over every row in the table below.']),
    })
  );
  host.appendChild(
    tradeLogCell('Capital committed now', money(entry.capital_committed_now), {
      foot:
        entry.capital_committed_pct === null || entry.capital_committed_pct === undefined
          ? null
          : `${pct(entry.capital_committed_pct, 1)} of ${entry.capital_committed_pct_of}`,
      help: formula([
        'Collateral = Short puts (Strike × 100 × Contracts)',
        '  + Cost basis of shares held',
        '  + Strike × 100 per short call with untracked shares (~)',
        '$0 once the wheel is flat.',
      ]),
    })
  );
  host.appendChild(
    tradeLogCell('Realized P&L', cents(entry.net_realized_pl), {
      tone: entry.net_realized_pl >= 0 ? 'pos' : 'neg',
      foot: `${cents(entry.option_realized_pl)} option · ${cents(entry.stock_realized_pl)} stock`,
      help: formula([
        'Realized P&L = Option P/L + Stock realized P/L (closed only)',
        `= ${cents(entry.option_realized_pl)} + ${cents(entry.stock_realized_pl)}`,
        `= ${cents(entry.net_realized_pl)}`,
        'Excludes open shares and options.',
      ]),
    })
  );
  // Always exactly these parts, in this order, summing to Mark-to-market P&L --
  // realized stock P/L and dividends only appear when non-zero, so the
  // common case stays a plain 3-term line without losing reconciliation
  // on a wheel where they matter.
  const asMoney = (v) => money(v, { cents: true });
  const mtmTerms = [
    [entry.option_realized_pl, 'realized options'],
    ...(entry.stock_realized_pl ? [[entry.stock_realized_pl, 'realized stock']] : []),
    ...(entry.dividends ? [[entry.dividends, 'dividends']] : []),
    [entry.stock_unrealized_pl, 'shares'],
    [entry.open_option_pl, `open options${entry.open_option_pl_marked ? '' : ' (est.)'}`],
  ];
  host.appendChild(
    tradeLogCell('Mark-to-market P&L', entry.mark_to_market_pl === null ? '—' : asMoney(entry.mark_to_market_pl), {
      tone:
        entry.mark_to_market_pl === null ? null : entry.mark_to_market_pl >= 0 ? 'pos' : 'neg',
      foot:
        entry.mark_to_market_pl === null
          ? 'no share price available'
          : mtmTerms.map(([v, label]) => `${asMoney(v)} ${label}`).join(' · '),
      help:
        entry.mark_to_market_pl === null
          ? null
          : formula([
              `Mark-to-market P&L = ${mtmTerms.map(([, label]) => label).join(' + ')}`,
              `= ${mtmTerms.map(([v]) => asMoney(v)).join(' + ')}`,
              `= ${asMoney(entry.mark_to_market_pl)}`,
            ]),
    })
  );

  const days1 = (value) => (value === null || value === undefined ? '—' : value.toFixed(1));
  host.appendChild(
    tradeLogCell(
      'Profit / day',
      notWheel ? 'n/a' : entry.profit_per_day === null ? '—' : cents(entry.profit_per_day) + '/day',
      {
        foot:
          notWheel || entry.profit_per_day === null
            ? null
            : `${cents(entry.option_realized_pl)} option P/L over ${entry.days_active.toLocaleString(
                'en-US'
              )} days active`,
        help: notWheel
          ? wheelHelp
          : formula([
              'PPD = Realized option P&L ÷ Calendar days active',
              `= ${cents(entry.option_realized_pl)} ÷ ${entry.days_active} days`,
              `= ${entry.profit_per_day === null ? '—' : cents(entry.profit_per_day)}/day`,
            ]),
      }
    )
  );
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
              'P&L per day held = Closed-leg option P&L ÷ Total days held',
              `= ${cents(entry.closed_leg_pl)} ÷ ${entry.total_days_held} days`,
              `= ${entry.pl_per_day_held === null ? '—' : cents(entry.pl_per_day_held)}/day`,
              'Each roll segment counts separately; a same-day trade counts as 1 day.',
            ]),
      }
    )
  );
  // Both cells point at the ledger table below rather than repeating its own
  // rows in a drill-down: every leg's own Result is already a column there
  // (one row per opening fill, one per closing fill; a single order that
  // closed several separately-opened lots at once is already split into
  // separate rows, each with its own verdict), and the Wheel timeline chart
  // above the table shows how rolls connect those legs.
  host.appendChild(
    tradeLogCell('Win rate (legs)', notWheel ? 'n/a' : pct(entry.win_rate_pct), {
      foot: notWheel ? null : `${entry.wins} / ${entry.wins + entry.losses} closed legs`,
      help: notWheel
        ? wheelHelp
        : formula([
            'Win rate = Winning legs ÷ (Winning + Losing) closed legs',
            '  Each opening lot is scored on its own, even if one order closes several.',
            '  Open and $0 legs excluded; not adjusted for rolls.',
          ]),
    })
  );
  host.appendChild(
    tradeLogCell('Roll rate', notWheel ? 'n/a' : pct(entry.roll_rate_pct), {
      foot:
        notWheel || !entry.rollable_legs
          ? null
          : [
              `${entry.rolled_legs} of ${entry.rollable_legs} eligible closes rolled`,
              `Resolved: ${entry.resolved_roll_wins}W / ${entry.resolved_roll_losses}L`,
              entry.open_roll_chains
                ? `${entry.open_roll_chains} open roll chain${entry.open_roll_chains === 1 ? '' : 's'}, ` +
                  `${money(entry.open_roll_credit, { sign: true })} cumulative credit` +
                  (entry.open_chain_credits && entry.open_chain_credits.length > 1
                    ? ` (${entry.open_chain_credits
                        .map((c) => `${c.chain_id}: ${money(c.net_cash, { sign: true })}`)
                        .join(' · ')})`
                    : '')
                : 'No roll chain is currently open',
            ],
      help: notWheel
        ? wheelHelp
        : formula([
            'Roll rate = Puts/calls closed by a same-day roll ÷ Closed puts/calls',
            '  Long hedge legs excluded.',
            `= ${entry.rolled_legs} ÷ ${entry.rollable_legs}`,
            '',
            'Resolved: chain fully closed, not just rolled again. Open chains excluded.',
            'Cumulative credit: net cash in an open chain so far, not marked to market.',
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
      foot:
        notWheel || entry.annualized_wheel_roc_pct === null
          ? undefined
          : [
              `${cents(entry.option_realized_pl)} realized ÷ ${money(entry.avg_collateral)} avg collateral` +
                ` × 365/${entry.days_active}`,
              `${money(entry.capital_committed_now)} committed today - not part of this ratio`,
            ],
      help: notWheel
        ? wheelHelp
        : formula([
            'Annualized Wheel ROC = (Option P/L ÷ Avg collateral) × (365 ÷ Days active)',
            '  Avg collateral time-weighted while open; no stock P/L.',
            `= (${cents(entry.option_realized_pl)} ÷ ${money(entry.avg_collateral)}) × (365 ÷ ${entry.days_active})`,
            `= ${pct(entry.annualized_wheel_roc_pct)}`,
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

  // This wheel (or hold) cycle also bought a long call/put somewhere along
  // the way -- e.g. a protective put alongside covered calls, still
  // visible here even though it never made the cycle "directional" on its
  // own (see Cycle.kind / _build_directional_card). Shown as its own
  // section below the wheel's own summary above, never replacing it.
  // Only while that long position is still open: once every long contract
  // is closed, its transaction rows already read gray ("settled") in the
  // table below, so a second closed-position card would only repeat them.
  if (entry.directional && entry.directional.status !== 'CLOSED') {
    renderDirectionalSummary(entry, { host: extraHost, manageNote: false });
  } else {
    clear(extraHost);
    extraHost.hidden = true;
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
    'Result',
    'Initial CSP collateral',
    'Fees',
    'Net cash flow',
    'Cumulative cash flow',
    'Profit Target',
    'Break-even',
  ];
  const HEAD_HELP = {
    Type: formula([
      'Sell/buy put or call, expire, assign, shares in/out, dividend.',
    ]),
    Date: formula([
      'Trade (as-of) date, not the ledger post date.',
    ]),
    Expiration: formula(['Option expiry.', 'Blank for stock and dividend rows.']),
    Strike: 'Option strike price.',
    'Shares / Contracts': formula([
      'Signed fill size: positive = bought/long, negative = sold/short.',
      'Dash: dividend row.',
    ]),
    'Price / Premium': formula([
      'Per-share fill price (stock or option premium), as broker-quoted.',
      'Green: cash received. Red: cash paid.',
    ]),
    'Return %': formula([
      'Short: (Open - Close) ÷ Open; 0.50 → 0.25 buyback = 50%',
      'Long: (Close - Open) ÷ Open; 1.00 → 0.20 sale = -80%',
      'Expiry/assignment = close at 0. Each lot scored on its own.',
      'Blank on opening fills.',
    ]),
    Result: formula([
      "This leg's win/loss once fully closed (same scoring as Win rate).",
      'Blank on opening fills and partial closes.',
    ]),
    'Initial CSP collateral': formula([
      'Strike × 100 × contracts.',
      'Shown on a cash-secured put open only.',
    ]),
    Fees: formula([
      'Regulatory/exchange fees + broker commission on this fill.',
    ]),
    'Net cash flow': formula([
      "The broker's Amount for this row, net of fees and commission.",
    ]),
    'Cumulative cash flow': formula([
      'Running sum of Net cash flow down the rows.',
      'A cash ledger, not a P&L figure.',
    ]),
    'Profit Target': formula([
      'Higher of running Cost basis and Break-even, plus X%, rounded up to $0.50.',
      'A floor for exiting at a real profit, not just breaking even.',
      'Dash while under one share is held.',
      'Last share-holding row uses the current Profit Target exactly.',
      'Hover a cell for that row\'s math.',
    ]),
    'Break-even': formula([
      'Break-even = -Cumulative cash flow ÷ Shares held',
      'Falls as premium and dividends come in.',
      'Dash while under one share is held, or once banked cash covers the shares.',
      'Last share-holding row = current Break-even price.',
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
  // Leg-class badge column, inserted right after "Type". No header text -- the
  // table isn't sortable and the badge speaks for itself.
  headRow.insertBefore(el('th', { class: 'left' }), headRow.children[1]);
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

  // Which row the backend pinned to the wheel's current official Profit
  // Target (see wheel/api.py: the last share-holding row, when shares are
  // still held) -- that row's Profit Target cell uses a different formula
  // than the rest of the column, so its tooltip needs to say so explicitly.
  let pinnedRowIdx = -1;
  if (entry.shares_held > 1e-9) {
    for (let i = entry.transactions.length - 1; i >= 0; i--) {
      if (typeof entry.transactions[i].running_break_even === 'number') {
        pinnedRowIdx = i;
        break;
      }
    }
  }

  const tbody = el('tbody');
  entry.transactions.forEach((row, rowIndex) => {
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
      row.result || '—',
      cents(row.initial_csp_collateral),
      cents(row.fees),
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
      if (index === 7 && (row.result === 'WIN' || row.result === 'LOSS')) {
        td.classList.add(row.result === 'WIN' ? 'pos' : 'neg');
      }
      if (index === 10 && typeof row.net_cash_flow === 'number') {
        td.classList.add(row.net_cash_flow >= 0 ? 'pos' : 'neg');
      }
      if (index === 11 && typeof row.running_cash_flow === 'number') {
        td.classList.add(row.running_cash_flow >= 0 ? 'pos' : 'neg');
      }
      tr.appendChild(td);
    });

    // Profit Target: the highest of this row's cost basis, breakeven and
    // wheel breakeven, cushioned (runningProfitTargetFormulaLines) -- except the pinned row (see pinnedRowIdx
    // above), which is snapped to the wheel's current official Profit Target
    // (the exact cost basis / position breakeven / wheel breakeven figures,
    // not this column's running approximation of them) and says so.
    const isPinned = rowIndex === pinnedRowIdx;
    const targetCell = el('td', { class: 'num' }, perShare(row.running_profit_target));
    if (isPinned && entry.profit_target_explanation) {
      setFormula(
        targetCell,
        formula([
          "Pinned to the wheel's current Profit Target (exact, not the running estimate):",
          '',
          entry.profit_target_explanation,
        ])
      );
    } else if (typeof row.running_profit_target === 'number') {
      setFormula(targetCell, formula(runningProfitTargetFormulaLines(row, entry, perShare)));
    }
    if (targetCell.title) targetCell.style.cursor = 'help';
    tr.insertBefore(targetCell, tr.lastElementChild);

    // Leg-class badge (CSP / CC / LP / LC), same look as the Open option
    // positions "Type" column. Blank for stock, dividend and expiry rows.
    const codeCell = el('td', { class: 'left' });
    if (row.type_code) {
      codeCell.appendChild(
        el(
          'span',
          {
            class: 'badge op-type op-type-' + row.type_code,
            title: OP_TYPE_LABEL[row.type_code] || row.type_code,
          },
          row.type_code
        )
      );
    }
    tr.insertBefore(codeCell, tr.children[1]);
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);

  // Brief key to the row shading (each sample carries the style it names).
  const legend = el('div', { class: 'tl-row-legend' });
  legend.appendChild(document.createTextNode('Row shading: '));
  legend.appendChild(el('span', { class: 'tl-sw settled' }, 'settled'));
  legend.appendChild(document.createTextNode(' a position that is fully closed (bought / sold back, expired, or assigned); '));
  legend.appendChild(el('span', { class: 'tl-sw open-hedge' }, 'open long leg'));
  legend.appendChild(document.createTextNode(' a bought option not yet closed; '));
  legend.appendChild(el('span', { class: 'tl-sw synthetic' }, 'synthesized'));
  legend.appendChild(document.createTextNode(' an assignment with no share leg in the export.'));
  host.appendChild(legend);

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
    $('tradelog-directional-extra').hidden = true;
    $('tradelog-bridge').hidden = true;
    $('tradelog-ppd').hidden = true;
    $('tradelog-breakeven').hidden = true;
    $('tradelog-timeline').hidden = true;
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
    $('tradelog-directional-extra').hidden = true;
    $('tradelog-bridge').hidden = true;
    $('tradelog-ppd').hidden = true;
    $('tradelog-breakeven').hidden = true;
    $('tradelog-timeline').hidden = true;
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
  drawTradeLogTimeline(entry);
  renderTradeLogTable(entry);
  drawTradeLogBreakeven(entry);
}

function render() {
  const data = state.data;
  if (!data) return;
  const {
    meta,
    portfolio,
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
    queueConfigSave();
    load();
  });

  renderNotices(meta, reconciliation);
  renderTiles(portfolio, reconciliation);
  renderWheelTargets();
  renderHedgeBanner();
  renderDashboardInsights();
  renderNetWorth(net_worth, benchmark, wheel_return, wheel_state);

  drawCapital(capital_series, net_worth);
  drawWheelState(wheel_state, net_worth);
  drawPnl(appendMarkToMarketPoint(pnl_series, meta, portfolio));

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
              'PPD = Wheel option P/L ÷ Days',
              '',
              'N/A; buy-and-hold, no puts or calls sold.',
            ]),
          }
        : {
            text: money(row.profit_per_day, { cents: true }) + '/day',
            title: formula([
              'PPD = (Premium collected - Closeout cost) ÷ Days',
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
          'Annualized Wheel ROC = (Wheel option P/L ÷ Avg capital) × (365 ÷ Days)',
          '',
          'N/A; buy-and-hold (no puts or calls sold) or no wheel capital committed.',
        ])
      : formula([
          'Annualized Wheel ROC = (Wheel option P/L ÷ Avg capital) × (365 ÷ Days)',
          '  Wheel option P/L = CSP + covered-call + hedge P/L; excludes stock P/L.',
          `= (${money(row.option_realized_pl, { cents: true })} ÷ ${money(row.avg_capital)}) × (365 ÷ ${row.days_span})`,
          `= ${pct(row.roi_on_avg_wheel_pct, 2)} × ${(365 / row.days_span).toFixed(2)}`,
          `= ${pct(row.annualized_wheel_roc_pct)}`,
        ]);

  const tickerNetOptionYieldFormula = (row) =>
    row.annualized_net_option_yield_pct === null
      ? formula([
          'Annualized Net Option Yield = (Option P/L ÷ Total initial collateral) × (365 ÷ Days)',
          '',
          'N/A; buy-and-hold (no puts or calls sold) or no initial collateral.',
        ])
      : formula([
          'Annualized Net Option Yield =',
          '  (Option P/L ÷ Total initial collateral) × (365 ÷ Days)',
          '  Denominator = sum of each cycle\'s day-one collateral.',
          `= (${money(row.option_realized_pl, { cents: true })} ÷ ${money(row.total_initial_collateral)}) × (365 ÷ ${row.days_span})`,
          `= ${pct(row.net_option_yield_pct, 2)} × ${(365 / row.days_span).toFixed(2)}`,
          `= ${pct(row.annualized_net_option_yield_pct)}`,
        ]);

  const tickerTotalPositionRoiFormula = (row) =>
    row.annualized_total_position_roi_pct === null
      ? formula([
          'Annualized Total Position ROI = (Option P/L + Stock P/L + Dividends)',
          '  ÷ Total initial collateral × (365 ÷ Days)',
          '',
          'N/A, no initial collateral committed.',
        ])
      : formula([
          'Annualized Total Position ROI =',
          '  (Option P/L + Stock realized P/L + Stock unrealized P/L + Dividends)',
          '  ÷ Total initial collateral × (365 ÷ Days)',
          '  Excludes open long-option (hedge) P/L.',
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
          '"yes": a covered call backed by shares bought before this export.',
          'Their capital is estimated as Strike × 100 (cost basis unknown).',
          'Marked ~ next to the ticker on the chart.',
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

  applyScatterFilter();

  applyTimelineFilter();
  applySequencesFilter();

  // Planner / Realized-Gains render last and are isolated: a failure in one of
  // their (chart-heavy) panels must never blank the dashboard cards above.
  try {
    renderPlanner();
    renderRealizedGains();
    renderCapitalProjection();
    updateExportLinks();
  } catch (error) {
    console.error('Planner/Realized-Gains render failed:', error);
  }

  // The Trade Log is filter-independent (payload's `trade_log` is built from
  // full history), but re-render it so the picker tracks an account switch.
  renderTabs();
  if (state.activeTab === 'tradelog') renderTradeLog();

  applySearchFilter();
}

/* ----------------------------------------------------------- table print */

// Elements (`table` or `.card-subsection`-scoped filter shared below) that
// belong directly to `root` (a .card or .card-subsection), not to a nested
// .card-subsection -- so a card's own print button doesn't also grab its
// subsections' tables, which get their own button instead.
function ownScoped(root, selector) {
  return Array.from(root.querySelectorAll(selector)).filter((node) => {
    const sub = node.closest('.card-subsection');
    return !sub || sub === root;
  });
}

// The actual <table> elements to print for `root`, read live at print time
// (a chart's table twin is only ever populated once its data has loaded).
// Excludes a <table> nested inside another <table> -- buildTable() clears
// and rebuilds *inside* whatever container id it's given, and a few tables
// (e.g. capital-projection-summary) use a bare <table> as that container,
// leaving the real, populated table sitting as a child of an empty outer
// one of the same tag; cloning the outer one already brings the inner one
// along, so counting both would print the same table twice. A legitimately
// nested table -- e.g. a Wheel sequences row's expanded leg/spread detail,
// sitting in a <td> of the outer cycles-table -- is excluded the same way,
// for the same reason: it's already part of the outer table's clone.
function ownTables(root) {
  return ownScoped(root, 'table').filter((table) => !table.parentElement.closest('table'));
}

// `root`'s own chart(s) -- each an { svg, legend } pair -- read live at
// print time. A chart lives in a .chart-scroll wrapper with a sibling
// .legend div; both belong to root's direct chart, not a nested
// subsection's, by the same ownScoped() rule as everything else here.
function ownCharts(root) {
  return ownScoped(root, '.chart-scroll')
    .map((scroll) => ({
      svg: scroll.querySelector('svg[id]'),
      legend: scroll.parentElement ? scroll.parentElement.querySelector(':scope > .legend') : null,
    }))
    .filter((chart) => chart.svg);
}

// `root`'s own .tiles stat block(s), if any (e.g. Net worth & benchmark's
// XIRR-vs-SPY tiles, Monthly cash flow's totals) -- part of what the card
// visibly shows above its chart, not just the chart itself.
function ownTiles(root) {
  return ownScoped(root, '.tiles');
}

// What to actually print for `root`: its own tiles (if any) + chart(s), if
// its table is a .table-twin (a genuine toggle counterpart to a chart, e.g.
// Capital deployed or Realized P/L by ticker) -- since a picture of the
// trend, with the same headline figures the card itself leads with, beats a
// dump of the same numbers -- plus any other table `root` owns that isn't
// that twin (Capital projection's always-shown summary table sits beside its
// own monthly table twin; the chart swaps in for the twin, the summary table
// still prints). A plain <table> or a .table-host (an always-shown table
// with no chart alternative, e.g. Wheel price targets or the Trade Log
// ledger) is never swapped out, even if the card happens to also draw an
// unrelated supplementary chart elsewhere (the Trade Log's own
// profit-over-time / PPD charts are just that: the ledger table is still the
// thing to print).
function ownPrintable(root) {
  const hasTwin = ownScoped(root, '.table-twin').length > 0;
  if (hasTwin) {
    const charts = ownCharts(root);
    if (charts.length) {
      const standaloneTables = ownTables(root).filter((table) => !table.closest('.table-twin'));
      return { kind: 'chart', tiles: ownTiles(root), charts, tables: standaloneTables };
    }
  }
  return { kind: 'table', tables: ownTables(root) };
}

function printableHasContent(printable) {
  return printable.kind === 'chart' ? printable.charts.length > 0 : printable.tables.length > 0;
}

// Whether `root` has anything to print, checked at startup before any data
// has loaded: a chart's table twin (.table-twin) or an always-on table
// container (.table-host, e.g. the Trade Log ledger) starts as an empty div
// and only gains its <table> once render() runs, so that div's mere
// presence -- not yet holding a <table> -- is what has to gate the button.
// A card with only a chart and no table at all (e.g. Expiration calendar)
// never had a button before this and still doesn't -- printing that chart
// wasn't asked for, only swapping charts in for their table twins.
function hasOwnTableHost(root) {
  return ownScoped(root, 'table, .table-twin, .table-host').length > 0;
}

/**
 * Global search (the field above the tab bar, reachable from every tab):
 * hides any .card / .card-subsection with no match, and, for one that owns
 * a table, hides just its non-matching rows rather than the whole card. A
 * card's own heading matching is enough to show all of its rows.
 *
 * Table bodies are rebuilt from scratch on every render() (and thus on the
 * 15s live-price poll too), which would otherwise wipe any .search-hide
 * classes on old <tr> elements -- so this re-runs at the end of render()
 * and switchTab(), not just on input.
 */
// A few dashboard/planner panels are built straight into a bare <section>
// (no .card wrapper or <header><h2>, so they're invisible to the .card,
// .card-subsection query below) but still carry real per-ticker rows or text
// worth searching: in-the-money legs, the open-hedge banner, the earnings
// strip, and the Trade Log's own hedge block. Named explicitly rather than
// widening the .card selector, since none of them has a heading to match on.
const SEARCH_EXTRA_ROOTS = ['#assignment-risk', '#hedge-banner', '#earnings-in-view', '#tradelog-hedge'];

function applySearchFilter() {
  const input = $('global-search');
  const term = input ? input.value.trim().toLowerCase() : '';
  const items = Array.from(
    document.querySelectorAll(['.card', '.card-subsection', ...SEARCH_EXTRA_ROOTS].join(', '))
  );
  // Reversed so a card-subsection (later in document order, being nested
  // deeper) is settled before the ancestor .card that needs its outcome.
  items.reverse().forEach((root) => {
    if (!term) {
      root.classList.remove('search-hide');
      ownScoped(root, 'tbody tr').forEach((tr) => tr.classList.remove('search-hide'));
      return;
    }

    const header = root.querySelector(':scope > header');
    const heading = header ? header.querySelector(':scope > h2, :scope > h3') : null;
    const headingMatch = heading ? heading.textContent.toLowerCase().includes(term) : false;

    const ownRows = ownScoped(root, 'tbody tr');
    let anyRowVisible = false;
    ownRows.forEach((tr) => {
      const match = headingMatch || tr.textContent.toLowerCase().includes(term);
      tr.classList.toggle('search-hide', !match);
      if (match) anyRowVisible = true;
    });

    const subsectionVisible = Array.from(root.querySelectorAll('.card-subsection')).some(
      (sub) => sub.closest('.card') === root && !sub.classList.contains('search-hide')
    );

    // A card with no table at all (tiles/insights/chips only, e.g.
    // Performance or Workflow) can't be filtered row by row -- fall back to
    // matching its whole text.
    const fallbackMatch = !hasOwnTableHost(root) && root.textContent.toLowerCase().includes(term);

    const visible = headingMatch || anyRowVisible || subsectionVisible || fallbackMatch;
    root.classList.toggle('search-hide', !visible);
  });
}

// Groups a header's non-heading children (CSV links, toggle buttons, ...)
// into one .card-actions box, if they aren't already, so a newly added
// button lands beside them at the header's right edge instead of spreading
// out across it.
function ensureActionsGroup(header) {
  const existing = header.querySelector(':scope > .card-actions');
  if (existing) return existing;
  const heading = header.querySelector(':scope > h2, :scope > h3');
  const actions = el('div', { class: 'card-actions' });
  Array.from(header.children).forEach((child) => {
    if (child !== heading) actions.appendChild(child);
  });
  header.appendChild(actions);
  return actions;
}

// The account the print button should credit, matching the label the
// account chips show (see renderAccountChips()) -- "Combined" for the
// aggregate view, otherwise the reader's own name for that data/<account>/
// folder.
function currentAccountLabel() {
  if (state.account === COMBINED_ACCOUNT_ID) return 'Combined';
  return accountLabelsById[state.account] || state.account;
}

// The active Date range filter (top of the page, applies to every tab), in
// the same words as its own <select> -- e.g. "Last 90 days", plus the actual
// bounds once a preset has resolved to concrete dates (state.start/state.end;
// null on either end means unbounded that direction, not "no filter" -- only
// the 'all' preset leaves both null).
function currentDateRangeLabel() {
  const presetEl = $('preset');
  const option = presetEl && presetEl.selectedIndex >= 0 ? presetEl.options[presetEl.selectedIndex] : null;
  const label = option ? option.textContent.replace('…', '').trim() : '';
  const bounds = state.start || state.end ? `${state.start || 'earliest'} to ${state.end || 'latest'}` : '';
  if (label && bounds && label !== 'All history') return `${label} (${bounds})`;
  return label || bounds || 'All history';
}

// Card/subsection titles that default to *excluded* from Print All -- the
// deep-dive analytics charts (cumulative P/L, capital deployed, the Cash
// Flow & P/L Analytics breakdowns, ticker/ROC/timeline views, wheel
// sequences, the Realized-Gains cross-check, capital projection): useful on
// screen, but not what a reader wants padding out a combined printout. Each
// still has its own "Add to Print All" checkbox to opt back in; see
// initTablePrintButtons().
const PRINT_ALL_EXCLUDED_TITLES = new Set([
  'Cumulative Wheel P/L: realized & mark-to-market',
  'Capital deployed',
  'Net worth & benchmark',
  'Where the wheel is right now',
  'Monthly cash flow',
  'Periodic P/L',
  'Cash flow vs. wheel P/L gap',
  'Wheel PPD by week',
  'Realized P/L by ticker',
  'Annualized Wheel ROC',
  'Ticker efficiency: P/L vs. Annualized ROC',
  'Wheel timelines',
  'Wheel sequences',
  'Closed lots',
  'Per-ticker cross-check',
  'Capital projection',
]);

// root (.card/.card-subsection) -> its "Add to Print All" checkbox, set by
// initTablePrintButtons().
const printAllCheckboxByRoot = new WeakMap();

// Every card/subsection currently showing at least one table, checked in for
// Print All, in document order (dashboard top-to-bottom, then Planner,
// Realized Gains, Trade Log, Tools -- every tab's markup lives in the DOM at
// once, only hidden). Feeds printAllTables(); a card conditionally hidden
// for lack of data (its own `hidden` attribute, e.g. no net worth snapshots
// yet) is skipped, but a section is never skipped just because its *tab*
// isn't the active one.
function collectPrintableSections() {
  const sections = [];
  document.querySelectorAll('.card, .card-subsection').forEach((root) => {
    if (root.hidden) return;
    const checkbox = printAllCheckboxByRoot.get(root);
    if (checkbox && !checkbox.checked) return;
    const header = root.querySelector(':scope > header');
    if (!header) return;
    const printable = ownPrintable(root);
    if (!printableHasContent(printable)) return;
    const heading = header.querySelector(':scope > h2, :scope > h3');
    sections.push({ title: heading ? heading.textContent.trim() : 'Table', printable });
  });
  return sections;
}

// Builds the throwaway hidden iframe every print job runs through: links the
// real stylesheet (not a hand-copied one) so a table's colors -- gain/loss
// blue-and-red, the violet target figure, status badges, ... -- come out on
// paper exactly as they read on screen, and pins data-theme to light
// regardless of the page's current theme, since dark mode's palette is tuned
// for a dark screen, not white paper. Calls `populate(idoc)` once the
// stylesheet has actually loaded (async; printing before it lands would
// flash an unstyled, colorless table), then prints and cleans up.
function withPrintFrame(populate) {
  const frame = el('iframe', { class: 'print-frame', 'aria-hidden': 'true' });
  document.body.appendChild(frame);
  const idoc = frame.contentDocument;
  idoc.open();
  idoc.write('<!doctype html><html><head></head><body></body></html>');
  idoc.close();
  idoc.documentElement.setAttribute('data-theme', 'light');

  const link = idoc.createElement('link');
  link.rel = 'stylesheet';
  link.href = '/styles.css';

  const finish = () => {
    const override = idoc.createElement('style');
    override.textContent = `
      * {
        -webkit-print-color-adjust: exact;
        print-color-adjust: exact;
        color-adjust: exact;
      }
      @page { size: letter; margin: 0.5in; }
      body { margin: 24px; background: #fff; }
      h2 { font-size: 15px; margin: 0 0 2px; }
      .print-account { font-size: 12px; color: var(--text-secondary); margin: 0 0 16px; }
      table { margin-bottom: 22px; }
      table:last-child { margin-bottom: 0; }
      th { position: static; }
      svg { max-width: 100%; margin-bottom: 12px; }
      .legend { margin-bottom: 22px; }
      .print-page { page-break-after: always; break-after: page; }
      .print-page:last-child { page-break-after: auto; break-after: auto; }
      .chart-page-fit { transform-origin: top left; }
    `;
    idoc.head.appendChild(override);

    populate(idoc);

    frame.contentWindow.focus();
    frame.contentWindow.print();
    setTimeout(() => frame.remove(), 1000);
  };
  // error still finishes -- plain black-on-white beats a silently stuck button.
  link.addEventListener('load', finish);
  link.addEventListener('error', finish);
  idoc.head.appendChild(link);
}

// One printed page's content box in CSS px (96px/in is the CSS spec's fixed
// print reference, not actual printer DPI) -- Letter, minus the @page margin
// set in withPrintFrame()'s stylesheet, minus a ~5% safety margin for a
// reader whose print dialog ends up on a slightly smaller sheet (A4) or
// margin than assumed.
const PRINT_PAGE_CONTENT_HEIGHT = Math.round((11 - 2 * 0.5) * 96 * 0.95);

// Scales `inner`'s content down (never up) so it fits within `maxHeight`,
// instead of spilling onto a second page -- only ever applied to a chart
// page's tiles/chart/legend (see addPrintSection()); a table is left to
// flow across pages on its own, per the ask that only tables should ever run
// long. `box` is `inner`'s parent: transform doesn't shrink what an element
// reserves in normal page flow, only how it paints, so the box that actually
// has to end up one-page-tall gets an explicit height + clipping instead,
// while `inner` (still its natural, larger size) is visually scaled down to
// fit inside it.
function fitToOnePage(box, inner, maxHeight) {
  const naturalHeight = inner.scrollHeight;
  if (naturalHeight <= maxHeight) return; // already fits -- leave both at their natural size
  const scale = Math.max(maxHeight / naturalHeight, 0.15);
  inner.style.transform = `scale(${scale})`;
  inner.style.width = `${100 / scale}%`;
  box.style.height = `${maxHeight}px`;
  box.style.overflow = 'hidden';
}

// Appends one printable section (a heading, the account line, and its
// chart(s) or table(s) -- see ownPrintable()) to the print document, each
// wrapped so it starts its own page when there's more than one (see
// .print-page above). A chart is cloned as-is (colors are already resolved,
// literal values on its shapes, or CSS that the linked stylesheet + forced
// light theme reproduce) and stretched to the page width in place of its
// fixed on-screen pixel size; its legend comes along so a color still reads
// as a series name on paper, not just a hue. The tiles/chart/legend block is
// then shrunk, if it has to be, to keep the whole card on one page; a
// standalone table appended after it (Capital projection's summary table) is
// not, and is free to run onto another page if it's long.
function addPrintSection(idoc, title, printable) {
  const page = idoc.createElement('section');
  page.className = 'print-page';
  idoc.body.appendChild(page); // attached now, so scrollHeight below reads real layout

  const h2 = idoc.createElement('h2');
  h2.textContent = title;
  page.appendChild(h2);

  const account = idoc.createElement('div');
  account.className = 'print-account';
  account.textContent = `Account: ${currentAccountLabel()}`;
  page.appendChild(account);

  if (printable.kind === 'chart') {
    const headerHeight = page.scrollHeight; // space the title/account line above already used
    const box = idoc.createElement('div');
    const inner = idoc.createElement('div');
    inner.className = 'chart-page-fit';
    box.appendChild(inner);
    page.appendChild(box);

    (printable.tiles || []).forEach((tiles) => inner.appendChild(idoc.importNode(tiles, true)));
    printable.charts.forEach(({ svg, legend }) => {
      const svgClone = idoc.importNode(svg, true);
      svgClone.removeAttribute('width');
      svgClone.removeAttribute('height');
      svgClone.style.width = '100%';
      svgClone.style.height = 'auto';
      inner.appendChild(svgClone);
      if (legend) inner.appendChild(idoc.importNode(legend, true));
    });

    fitToOnePage(box, inner, PRINT_PAGE_CONTENT_HEIGHT - headerHeight);
  }
  // A chart section can still carry its own standalone table alongside the
  // chart (Capital projection's always-shown summary table beside its
  // monthly table twin, which the chart above already replaced) -- appended
  // outside the shrink-to-fit box above, so it flows and paginates normally.
  printable.tables.forEach((table) => page.appendChild(idoc.importNode(table, true)));
}

// Opens the browser print dialog on just `title` + its chart or table(s) --
// so an item buried in a long dashboard prints on its own, without the
// surrounding cards, filters, or rest of the page.
function printOne(title, printable) {
  if (!printableHasContent(printable)) return;
  withPrintFrame((idoc) => {
    idoc.title = title;
    addPrintSection(idoc, title, printable);
  });
}

// Opens the browser print dialog on every printable card/subsection in the
// app at once, one section per page (see collectPrintableSections()) -- the
// same print flow as a single card's button, just walking every tab instead
// of only the active one. Printing to "Save as PDF" turns this into one PDF
// with every item on its own page.
// Print All's opening page: the Performance card's headline tiles (and its
// insights box, if one is showing) plus the account and date range this
// report covers -- context a reader needs before the per-card pages that
// follow, so it always leads the report regardless of where the Performance
// card itself sits in the dashboard's document order.
function buildOverviewSection(idoc) {
  const page = idoc.createElement('section');
  page.className = 'print-page';

  const h2 = idoc.createElement('h2');
  h2.textContent = 'Performance';
  page.appendChild(h2);

  const account = idoc.createElement('div');
  account.className = 'print-account';
  account.textContent = `Account: ${currentAccountLabel()}`;
  page.appendChild(account);

  const range = idoc.createElement('div');
  range.className = 'print-account';
  range.textContent = `Date range: ${currentDateRangeLabel()}`;
  page.appendChild(range);

  const subtitleEl = $('subtitle');
  if (subtitleEl && subtitleEl.textContent) {
    const subtitle = idoc.createElement('div');
    subtitle.className = 'print-account';
    subtitle.textContent = subtitleEl.textContent;
    page.appendChild(subtitle);
  }

  const tiles = $('tiles');
  if (tiles && tiles.children.length) page.appendChild(idoc.importNode(tiles, true));

  const insights = $('dashboard-insights');
  if (insights && !insights.hidden && insights.children.length) page.appendChild(idoc.importNode(insights, true));

  idoc.body.appendChild(page);
}

function printAllTables() {
  const sections = collectPrintableSections();
  withPrintFrame((idoc) => {
    idoc.title = 'Wheel Dashboard';
    buildOverviewSection(idoc);
    sections.forEach((section) => addPrintSection(idoc, section.title, section.printable));
  });
}

// Adds a print button, top right of its header, to every card and
// card-subsection that owns at least one table (a static table or a chart's
// table twin) -- see hasOwnTableHost(). Runs once at startup: headers are
// static markup, and later re-renders only ever replace a table's rows,
// never the header or the table container itself.
function initTablePrintButtons() {
  document.querySelectorAll('.card, .card-subsection').forEach((root) => {
    const header = root.querySelector(':scope > header');
    if (!header) return;
    if (!hasOwnTableHost(root)) return;
    const heading = header.querySelector(':scope > h2, :scope > h3');
    const title = heading ? heading.textContent.trim() : 'Table';
    const actions = ensureActionsGroup(header);

    const checkbox = el('input', {
      type: 'checkbox',
      class: 'print-all-check',
      'aria-label': `Include "${title}" in Print All`,
    });
    checkbox.checked = !PRINT_ALL_EXCLUDED_TITLES.has(title);
    printAllCheckboxByRoot.set(root, checkbox);
    const checkboxLabel = el('label', {
      class: 'print-all-label',
      title: 'Include this table when using Print All (top right of the app)',
    });
    checkboxLabel.appendChild(checkbox);
    checkboxLabel.appendChild(document.createTextNode('All'));
    actions.appendChild(checkboxLabel);

    const button = el(
      'button',
      { class: 'toggle print-btn', type: 'button', title: `Print ${title}`, 'aria-label': `Print ${title}` },
      '⎙ Print'
    );
    button.addEventListener('click', () => printOne(title, ownPrintable(root)));
    actions.appendChild(button);
  });
}

initTablePrintButtons();
$('print-all').addEventListener('click', printAllTables);

/**
 * Startup sequence. data/config.json (the reader's last settings -- theme,
 * tab, account, filters, every box in the Tools tab, ...) is fetched and
 * applied to `state`/the theme/the controls' own DOM before anything else
 * happens, so the very first render already reflects it instead of
 * flashing plain defaults first, then jumping. `refreshAccounts()` must
 * still finish before `load()` fires -- it may reset a now-unknown restored
 * account back to Combined (see renderAccountChips()) -- so those two stay
 * sequential, same as before this existed.
 */
async function boot() {
  const cfg = await loadConfig();
  applyConfigSnapshot(cfg);
  syncViewControlsFromState();
  wireFilters();
  wireCapitalProjection();
  refreshDatasets();
  refreshAccounts().then(load);
}
boot();

// Live-price auto-refresh: re-poll while the tab is visible, on the same
// cadence as the server's live-quote cache TTL (_LIVE_PRICE_TTL_SECONDS in
// wheel/api.py) so each poll actually lands a fresh quote instead of the
// same server-cached one. Paused while the tab is hidden/backgrounded --
// nothing is looking at it -- and catches up immediately on return instead
// of waiting out whatever's left of the interval.
const LIVE_PRICE_POLL_MS = 15000;
setInterval(() => {
  if (!document.hidden) load({ background: true });
}, LIVE_PRICE_POLL_MS);
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) load({ background: true });
});
