/* Credit Spreads tab.
 *
 * Display only: every number, status, action and reason comes from
 * /api/spreads (wheel/creditspreads.py, assembled by
 * Dashboard.build_credit_spreads). This file formats and lays them out; it
 * never computes a credit, P/L, delta, target or recommendation of its own.
 *
 * Loaded before app.js and uses its shared helpers ($, el, clear, money, pct,
 * formula, setFormula, tileFoot, longDate, dayLabel, dashboardQuery, state,
 * queueConfigSave) at call time. app.js calls loadSpreads() after each
 * successful load() while this tab is open, and renderSpreads() on a tab
 * switch.
 *
 * Settings: `state.creditSpreads` (saved in data/config.json) holds only the
 * values the reader changed, as typed. Anything not there uses the backend's
 * default, and the form always shows the settings the backend actually used
 * (`data.settings`), so the defaults live in one place.
 */
(function (global) {
  'use strict';

  // The scan's chains are fetched live; a background refresh any more often
  // than this would mostly refetch unchanged quotes.
  const SPREADS_BACKGROUND_REFRESH_MS = 60000;
  // Must match API_VERSION in wheel/creditspreads.py. Static files are read
  // from disk on every request but the server only loads new Python code when
  // restarted, so a mismatch means the page is newer than the running server.
  const SPREADS_API_VERSION = 4;
  // Candidate rows shown per ticker before "Show all".
  const SPREADS_ROWS_SHOWN = 12;

  const SETTING_FIELDS = [
    { key: 'underlyings', label: 'Underlyings', type: 'text', list: true, hint: 'Ticker symbols, comma separated.' },
    { key: 'dte_min', label: 'Entry DTE from', type: 'number', step: 1 },
    { key: 'dte_max', label: 'Entry DTE to', type: 'number', step: 1 },
    { key: 'delta_min', label: 'Short delta from', type: 'number', step: 1, hint: 'In delta points: 15 = 0.15.' },
    { key: 'delta_max', label: 'Short delta to', type: 'number', step: 1 },
    { key: 'widths', label: 'Spread width $', type: 'text', list: true, hint: 'One or more, comma separated.' },
    { key: 'min_credit_pct', label: 'Min credit % of width', type: 'number', step: 1 },
    { key: 'profit_target_pct', label: 'Profit target %', type: 'number', step: 5 },
    { key: 'loss_multiple', label: 'Close when cost to close reaches × credit', type: 'number', step: 0.25 },
    { key: 'time_exit_dte', label: 'Time exit DTE', type: 'number', step: 1 },
    { key: 'max_risk_pct', label: 'Max risk % of account', type: 'number', step: 0.25 },
    { key: 'max_open', label: 'Max open spreads', type: 'number', step: 1 },
    {
      key: 'require_trend',
      label: 'Trend filter',
      type: 'select',
      options: [
        ['on', 'Required: bull puts above the 50-day average, bear calls below'],
        ['off', 'Off: trend shown as a risk only'],
      ],
    },
    { key: 'min_iv_pct', label: 'Min short-leg IV %', type: 'number', step: 1, hint: 'Blank = no volatility filter.' },
  ];
  const SETTING_KEYS = new Set(SETTING_FIELDS.map((f) => f.key));

  const STRATEGY_OPTIONS = [
    ['BULL_PUT', 'Bull put'],
    ['BEAR_CALL', 'Bear call'],
  ];

  const spreads = {
    data: null,
    generation: 0,
    lastFetchAt: 0,
    check: null, // {ticker, expiry, strategy, short, long} or null
    showAll: new Set(), // tickers whose full candidate list is expanded
    formBuilt: false,
    checkForm: null,
  };

  /* ------------------------------------------------------------ settings */

  /** Keep only known keys with plain string values: config.json is machine
   *  written but not hand-edit proof. */
  function sanitizeSpreadSettings(raw) {
    const out = {};
    if (!raw || typeof raw !== 'object') return out;
    for (const [key, value] of Object.entries(raw)) {
      if (SETTING_KEYS.has(key) && typeof value === 'string' && value.length <= 80) out[key] = value;
    }
    return out;
  }

  function spreadsQuery() {
    const params = dashboardQuery();
    params.delete('tickers'); // the Wheel ticker chips don't scope spreads
    for (const [key, value] of Object.entries(state.creditSpreads || {})) params.set(key, value);
    if (spreads.check) {
      for (const [key, value] of Object.entries(spreads.check)) params.set(`check_${key}`, value);
    }
    return params;
  }

  function settingDisplay(field, value) {
    if (value === null || value === undefined) return '';
    if (field.type === 'select') return value ? 'on' : 'off';
    if (field.list) return value.join(', ');
    return String(value);
  }

  function buildSettingsForm() {
    const host = $('spreads-settings');
    clear(host);
    for (const field of SETTING_FIELDS) {
      const wrap = el('div', { class: 'field' });
      const id = `spreads-setting-${field.key}`;
      wrap.appendChild(el('label', { for: id }, field.label));
      let input;
      if (field.type === 'select') {
        input = el('select', { id });
        for (const [value, text] of field.options) input.appendChild(el('option', { value }, text));
      } else {
        input = el('input', {
          id,
          type: field.type,
          step: field.step,
          inputmode: field.type === 'number' ? 'decimal' : null,
        });
      }
      if (field.hint) input.title = field.hint;
      input.dataset.key = field.key;
      input.addEventListener('change', () => {
        const typed = input.value.trim();
        const overrides = { ...(state.creditSpreads || {}) };
        if (typed === '') delete overrides[field.key];
        else overrides[field.key] = typed;
        state.creditSpreads = overrides;
        queueConfigSave();
        loadSpreads();
      });
      wrap.appendChild(input);
      host.appendChild(wrap);
    }
    const reset = el('button', { class: 'toggle', type: 'button' }, 'Reset to defaults');
    reset.addEventListener('click', () => {
      state.creditSpreads = {};
      queueConfigSave();
      loadSpreads();
    });
    const wrap = el('div', { class: 'field spreads-settings-reset' });
    wrap.appendChild(reset);
    host.appendChild(wrap);
    spreads.formBuilt = true;
  }

  function renderSettings(data) {
    if (!spreads.formBuilt) buildSettingsForm();
    for (const field of SETTING_FIELDS) {
      const input = $(`spreads-setting-${field.key}`);
      if (input && document.activeElement !== input) input.value = settingDisplay(field, data.settings[field.key]);
    }
    const host = $('spreads-settings-warnings');
    clear(host);
    if (data.settings_warnings.length) {
      const notice = el('div', { class: 'notice' });
      notice.appendChild(el('strong', {}, 'Some settings were not used. '));
      notice.appendChild(document.createTextNode(data.settings_warnings.join(' ')));
      host.appendChild(notice);
      $('spreads-settings-card').open = true;
    }
  }

  /* ------------------------------------------------------------ loading */

  async function loadSpreads({ background = false } = {}) {
    if (background && Date.now() - spreads.lastFetchAt < SPREADS_BACKGROUND_REFRESH_MS) return;
    const generation = ++spreads.generation;
    spreads.lastFetchAt = Date.now();
    const status = $('spreads-status');
    if (!background && !spreads.data) {
      clear(status);
      status.appendChild(el('p', { class: 'hint' }, 'Loading option prices…'));
    }
    try {
      const response = await fetch('/api/spreads?' + spreadsQuery().toString());
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'HTTP ' + response.status);
      if (data.api_version !== SPREADS_API_VERSION) {
        throw new Error(
          'The dashboard server is running older code than this page. Stop it and run python -m wheel.serve again, then reload.'
        );
      }
      if (generation !== spreads.generation) return;
      spreads.data = data;
      renderSpreads();
    } catch (error) {
      if (generation !== spreads.generation) return;
      clear(status);
      const notice = el('div', { class: 'notice err' });
      notice.appendChild(el('strong', {}, 'Could not load credit spreads'));
      notice.appendChild(document.createTextNode(' ' + error.message));
      status.appendChild(notice);
    }
  }

  /* ------------------------------------------------------------ shared cells */

  const usd = (value) => money(value, { cents: true });
  const strikes = (row) => `${row.short_strike}/${row.long_strike}`;
  const signClass = (value) => (value === null || value === undefined ? '' : value < 0 ? 'neg' : value > 0 ? 'pos' : '');

  function statusBadge(text, kind) {
    return el('span', { class: `badge spread-status ${kind}` }, text);
  }

  function candidateStatusKind(status) {
    return status === 'QUALIFIES' ? 'ok' : status === 'UNVERIFIED' ? 'unknown' : 'fail';
  }

  function checksText(checks) {
    const word = (passed) => (passed === true ? 'Pass' : passed === false ? 'Fail' : 'Unknown');
    return checks.map((c) => `${word(c.passed)}, ${c.label}: ${c.detail}`).join('\n');
  }

  function whyText(candidate) {
    // Rule names only, so rows stay one line; the details are in the tooltip.
    if (candidate.status === 'QUALIFIES') return 'Passes every rule';
    const failed = candidate.checks.filter((c) => c.passed === false);
    if (failed.length) return 'Fails: ' + failed.map((c) => c.label).join(', ');
    return 'Missing: ' + candidate.checks.filter((c) => c.passed === null).map((c) => c.label).join(', ');
  }

  /** "Nov 30", with the year only when it isn't this year: keeps date columns narrow. */
  function shortDate(iso) {
    if (!iso) return '—';
    return iso.slice(0, 4) === String(new Date().getFullYear()) ? dayLabel(iso) : longDate(iso);
  }

  /**
   * One table. Each row is an array of cells, or `{cells, detail}` where
   * `detail` is a sentence shown on its own full-width line under the row
   * (long reasons there instead of in a column, so the table fits without a
   * horizontal scroll). A cell is a string, a node, or
   * `{text, sub, title, class}`: `sub` is a smaller second line, which lets
   * two related values share one column.
   */
  function table(head, rows, className) {
    const tableNode = el('table', { class: ['spreads-table', className].filter(Boolean).join(' ') });
    const thead = el('thead');
    const headRow = el('tr');
    head.forEach((label, index) => headRow.appendChild(el('th', { class: index === 0 ? 'left' : '' }, label)));
    thead.appendChild(headRow);
    tableNode.appendChild(thead);
    const tbody = el('tbody');
    for (const row of rows) {
      const cells = Array.isArray(row) ? row : row.cells;
      const tr = el('tr', { class: !Array.isArray(row) && row.detail ? 'has-detail' : null });
      cells.forEach((cell, index) => {
        const td = el('td', { class: index === 0 ? 'left' : 'num' });
        if (cell instanceof Node) td.appendChild(cell);
        else if (cell !== null && typeof cell === 'object') {
          if (cell.sub === undefined) td.textContent = cell.text;
          else {
            td.appendChild(el('div', {}, cell.text));
            td.appendChild(el('div', { class: 'sub' }, cell.sub));
          }
          if (cell.class) td.className += ' ' + cell.class;
          setFormula(td, cell.title);
        } else td.textContent = cell === null || cell === undefined ? '—' : String(cell);
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
      if (!Array.isArray(row) && row.detail) {
        const detailRow = el('tr', { class: 'spread-detail' });
        detailRow.appendChild(el('td', { class: 'left', colspan: String(cells.length) }, row.detail));
        tbody.appendChild(detailRow);
      }
    }
    tableNode.appendChild(tbody);
    return tableNode;
  }

  /* ------------------------------------------------------------ opportunities */

  // Leg bid/ask and the midpoint credit sit in the Credit cell's tooltip
  // (press T), keeping the table narrow enough for the Why column.
  const CANDIDATE_HEAD = [
    'Status', 'Spread', 'Expires', 'Short delta', 'Credit', 'Credit / width', 'Max profit / loss', 'Breakeven',
    'Return on risk', 'Chance of profit (est.)', 'Contracts', 'Why',
  ];

  function candidateRow(c) {
    const status = statusBadge(c.status, candidateStatusKind(c.status));
    status.title = checksText(c.checks);
    return [
      status,
      { text: strikes(c), sub: c.strategy_label },
      { text: shortDate(c.expiry), sub: `${c.dte} DTE` },
      c.short_delta === null ? '—' : c.short_delta.toFixed(1),
      { text: usd(c.credit), title: c.tip },
      { text: pct(c.credit_pct_of_width), title: formula(['Credit / width', `= ${usd(c.credit)} ÷ ${usd(c.width)}`, `= ${pct(c.credit_pct_of_width)}`, '', `Minimum: ${usd(c.min_credit)}`]) },
      { text: usd(c.max_profit), sub: usd(c.max_loss), title: formula([`Max profit: ${usd(c.max_profit)}`, `Max loss: ${usd(c.max_loss)}`, '  Per spread, before fees.']) },
      usd(c.breakeven),
      pct(c.return_on_risk_pct),
      { text: pct(c.pop_pct, 0), title: c.pop_source },
      c.contracts === null ? '—' : { text: String(c.contracts), title: formula([`Risk per spread incl. fees to open: ${usd(c.risk_per_contract)}`]) },
      { text: whyText(c), class: 'spread-why', title: checksText(c.checks) },
    ];
  }

  function renderScan(data) {
    const host = $('spreads-scan');
    clear(host);
    for (const row of data.scan) {
      const block = el('div', { class: 'spreads-ticker' });
      const head = el('div', { class: 'spreads-ticker-head' });
      head.appendChild(el('strong', {}, row.ticker));
      if (row.spot !== null) head.appendChild(el('span', {}, usd(row.spot)));
      if (row.quote) {
        const q = el('span', { class: row.quote.status === 'FRESH' ? '' : 'warn' }, row.quote.label);
        head.appendChild(q);
      }
      head.appendChild(el('span', {}, row.context.trend_label));
      if (row.context.realized_vol_pct !== null) {
        head.appendChild(el('span', {}, `30-day realized volatility ${pct(row.context.realized_vol_pct)}`));
      }
      if (row.context.earnings_date) head.appendChild(el('span', { class: 'warn' }, `Earnings ${longDate(row.context.earnings_date)}`));
      if (row.expirations.length) head.appendChild(el('span', {}, `Expirations checked: ${row.expirations.map(dayLabel).join(', ')}`));
      block.appendChild(head);

      if (row.unavailable_reason) block.appendChild(el('div', { class: 'notice' }, row.unavailable_reason));
      if (row.notes.length) {
        const notes = el('details', { class: 'spreads-notes' });
        notes.appendChild(el('summary', {}, `${row.notes.length} skipped`));
        const list = el('ul', { class: 'hint-list' });
        row.notes.forEach((note) => list.appendChild(el('li', {}, note)));
        notes.appendChild(list);
        block.appendChild(notes);
      }
      if (row.candidates.length) {
        const expanded = spreads.showAll.has(row.ticker);
        const shown = expanded ? row.candidates : row.candidates.slice(0, SPREADS_ROWS_SHOWN);
        const scroll = el('div', { style: 'overflow-x: auto' });
        scroll.appendChild(table(CANDIDATE_HEAD, shown.map(candidateRow)));
        block.appendChild(scroll);
        if (row.candidates.length > SPREADS_ROWS_SHOWN) {
          const toggle = el(
            'button',
            { class: 'toggle', type: 'button' },
            expanded ? 'Show fewer' : `Show all ${row.candidates.length}`
          );
          toggle.addEventListener('click', () => {
            if (expanded) spreads.showAll.delete(row.ticker);
            else spreads.showAll.add(row.ticker);
            renderScan(spreads.data);
          });
          block.appendChild(toggle);
        }
      } else if (!row.unavailable_reason) {
        block.appendChild(el('p', { class: 'hint' }, 'No spread had a short strike inside the delta range.'));
      }
      host.appendChild(block);
    }
  }

  /* ------------------------------------------------------------ check a spread */

  /** Replace a select's options, keeping the current choice when it's still offered. */
  function syncOptions(select, pairs) {
    const current = select.value;
    clear(select);
    for (const [value, text] of pairs) select.appendChild(el('option', { value }, text));
    if (pairs.some(([value]) => value === current)) select.value = current;
  }

  function expirationOptions(ticker) {
    const listed = (spreads.data && spreads.data.check_expirations[ticker]) || [];
    return listed.map((iso) => [iso, longDate(iso)]);
  }

  /** Built once: a refresh only updates the option lists, so it never wipes
   *  what the reader is typing. */
  function buildCheckForm() {
    const host = $('spreads-check-form');
    clear(host);
    const field = (label, input) => {
      const wrap = el('div', { class: 'field' });
      wrap.appendChild(el('label', {}, label));
      wrap.appendChild(input);
      host.appendChild(wrap);
      return input;
    };
    const form = {
      ticker: field('Underlying', el('select')),
      strategy: field('Type', el('select')),
      expiry: field('Expiration', el('select')),
      short: field('Short strike', el('input', { type: 'number', step: '0.5', inputmode: 'decimal' })),
      long: field('Long strike', el('input', { type: 'number', step: '0.5', inputmode: 'decimal' })),
    };
    syncOptions(form.strategy, STRATEGY_OPTIONS);
    form.ticker.addEventListener('change', () => syncOptions(form.expiry, expirationOptions(form.ticker.value)));

    const run = el('button', { class: 'toggle', type: 'button' }, 'Check');
    run.addEventListener('click', () => {
      spreads.check = {
        ticker: form.ticker.value,
        expiry: form.expiry.value,
        strategy: form.strategy.value,
        short: form.short.value.trim(),
        long: form.long.value.trim(),
      };
      loadSpreads();
    });
    const clearButton = el('button', { class: 'toggle', type: 'button' }, 'Clear');
    clearButton.addEventListener('click', () => {
      spreads.check = null;
      form.short.value = '';
      form.long.value = '';
      loadSpreads();
    });
    const actions = el('div', { class: 'field spreads-check-actions' });
    actions.appendChild(run);
    actions.appendChild(clearButton);
    host.appendChild(actions);
    return form;
  }

  function renderCheckForm(data) {
    if (!spreads.checkForm) spreads.checkForm = buildCheckForm();
    const form = spreads.checkForm;
    syncOptions(form.ticker, Object.keys(data.check_expirations).map((t) => [t, t]));
    syncOptions(form.expiry, expirationOptions(form.ticker.value));
  }

  function renderCheckResult(data) {
    const host = $('spreads-check-result');
    clear(host);
    if (!data.check) return;
    if (data.check.error) {
      host.appendChild(el('div', { class: 'notice' }, data.check.error));
      return;
    }
    const c = data.check.candidate;
    const scroll = el('div', { style: 'overflow-x: auto' });
    scroll.appendChild(table(CANDIDATE_HEAD, [candidateRow(c)]));
    host.appendChild(scroll);
    const list = el('ul', { class: 'hint-list' });
    for (const check of c.checks) {
      const li = el('li');
      li.appendChild(el('strong', {}, `${check.label}`));
      li.appendChild(document.createTextNode(`: ${check.passed === true ? 'Pass' : check.passed === false ? 'Fail' : 'Unknown'}. ${check.detail}`));
      list.appendChild(li);
    }
    for (const risk of data.check.risks) {
      const li = el('li');
      li.appendChild(el('strong', {}, 'Risk'));
      li.appendChild(document.createTextNode(`: ${risk}`));
      list.appendChild(li);
    }
    host.appendChild(list);
  }

  /* ------------------------------------------------------------ open spreads */

  function actionKind(action) {
    return { close: 'close', review: 'review', hold: 'ok', unverified: 'unknown' }[action] || 'unknown';
  }

  function renderOpen(data) {
    const host = $('spreads-open');
    clear(host);
    const rows = data.open_spreads;
    if (!rows.length) {
      host.appendChild(el('p', { class: 'hint' }, 'No open credit spreads in the loaded history.'));
      return;
    }
    const combined = rows.some((r) => r.account_label);
    // Paired values share a column (second line in smaller type) and the
    // reason sits on its own line under each spread, so the table fits the
    // page without a horizontal scroll.
    const head = [
      'Spread', 'Expires', 'Action', 'Credit', 'Net collected', 'Breakeven', 'Price', 'Close now',
      'Unrealized P/L', 'Target / loss trigger', 'Time exit', 'Max profit / loss', 'Short delta',
    ];
    const body = rows.map((r) => {
      const distance = r.distance_to_short_pct;
      const distanceText =
        distance === null ? null : distance >= 0 ? `${pct(distance)} from short` : `Past short by ${pct(-distance)}`;
      const spreadSub = [
        r.strategy_label,
        `${r.contracts_open} contract${r.contracts_open === 1 ? '' : 's'}`,
        ...(combined ? [r.account_label] : []),
      ].join(', ');
      return {
        cells: [
          { text: `${r.ticker} ${strikes(r)}`, sub: spreadSub, title: r.trades_tip },
          { text: shortDate(r.expiry), sub: `${r.dte} DTE, opened ${shortDate(r.open_date)}` },
          statusBadge(r.action_label, actionKind(r.action)),
          usd(r.credit),
          { text: usd(r.net_premium_collected), title: r.net_collected_tip },
          { text: usd(r.breakeven), title: r.breakeven_tip },
          {
            text: r.spot === null ? '—' : usd(r.spot),
            sub: distanceText || undefined,
            class: distance !== null && distance < 0 ? 'neg' : '',
            title: 'Distance from the price to the short strike, as % of price.',
          },
          { text: usd(r.closing_debit_now), title: r.tip },
          {
            text: usd(r.unrealized_pl),
            sub: r.pct_of_max_profit === null ? undefined : `${pct(r.pct_of_max_profit, 0)} of max`,
            class: signClass(r.unrealized_pl),
            title: r.unrealized_pl === null ? null : formula([
              'Unrealized P/L = (credit - close now) × 100 × contracts',
              `= (${usd(r.credit)} - ${usd(r.closing_debit_now)}) × 100 × ${r.contracts_open}`,
              `= ${usd(r.unrealized_pl)}`,
              '  Before closing fees.',
            ]),
          },
          {
            text: `Target ${usd(r.target_debit)}`,
            sub: `Loss ${usd(r.loss_trigger)}`,
            title: formula([
              `Profit at target: ${usd(r.target_profit)} before fees`,
              `Loss at trigger: ${usd(r.loss_at_trigger)} before fees`,
            ]),
          },
          shortDate(r.time_exit_date),
          { text: usd(r.max_profit), sub: usd(r.max_loss) },
          r.short_delta === null ? '—' : r.short_delta.toFixed(1),
        ],
        detail: [r.reason, ...r.flags.filter((f) => !r.reason.includes(f))].join(' '),
      };
    });
    host.appendChild(table(head, body));
    host.appendChild(tradesTable(rows, combined));
  }

  /** The broker fills behind each listed spread, straight from the ledger. */
  function tradesTable(rows, combined) {
    const wrap = el('div', { class: 'card-subsection' });
    const header = el('header');
    header.appendChild(el('h3', {}, 'Trades'));
    wrap.appendChild(header);
    const body = [];
    for (const r of rows) {
      for (const t of r.trades) {
        body.push([
          shortDate(t.date),
          ...(combined ? [r.account_label] : []),
          `${r.ticker} ${strikes(r)} ${r.strategy_label.toLowerCase()}`,
          t.action,
          t.description,
          t.price === null ? '—' : usd(t.price),
          { text: usd(t.net_cash), class: signClass(t.net_cash) },
          usd(t.fees),
        ]);
      }
    }
    const head = ['Date', ...(combined ? ['Account'] : []), 'Spread', 'Action', 'Contract', 'Price', 'Net cash', 'Fees'];
    wrap.appendChild(table(head, body));
    wrap.appendChild(el('p', { class: 'hint' }, 'Net cash is the broker amount after commission and fees; money received is positive, money paid negative.'));
    return wrap;
  }

  /* ------------------------------------------------------------ recommendations */

  function renderRecommendations(data) {
    const host = $('spreads-recommendations');
    clear(host);
    for (const rec of data.recommendations) {
      const kind = rec.status === 'RECOMMENDED' ? 'ok' : rec.status === 'AT POSITION LIMIT' ? 'review' : 'fail';
      const card = el('div', { class: 'spreads-rec' });
      const head = el('div', { class: 'spreads-rec-head' });
      head.appendChild(statusBadge(rec.status, kind));
      head.appendChild(el('strong', {}, rec.headline));
      card.appendChild(head);
      const pick = rec.pick;
      if (pick) {
        const facts = el('div', { class: 'spreads-rec-facts' });
        [
          ['Credit', usd(pick.credit)],
          ['Max profit', usd(pick.max_profit)],
          ['Max loss', usd(pick.max_loss)],
          ['Breakeven', usd(pick.breakeven)],
          ['Contracts', pick.contracts === null ? '—' : String(pick.contracts)],
          ['Chance of profit (est.)', pct(pick.pop_pct, 0)],
        ].forEach(([name, value]) => facts.appendChild(el('span', {}, `${name}: ${value}`)));
        setFormula(facts, pick.tip);
        card.appendChild(facts);
        if (pick.quote) card.appendChild(el('div', { class: pick.quote.status === 'FRESH' ? 'hint' : 'hint warn' }, pick.quote.label));
      }
      const reasons = el('ul', { class: 'hint-list' });
      rec.reasons.forEach((reason) => reasons.appendChild(el('li', {}, reason)));
      card.appendChild(reasons);
      if (rec.risks.length) {
        card.appendChild(el('div', { class: 'spreads-rec-sub' }, 'Risks'));
        const risks = el('ul', { class: 'hint-list' });
        rec.risks.forEach((risk) => risks.appendChild(el('li', {}, risk)));
        card.appendChild(risks);
      }
      host.appendChild(card);
    }
  }

  /* ------------------------------------------------------------ performance */

  function perfTile(label, value, title, foot, valueClass) {
    const node = el('div', { class: 'tile' });
    node.appendChild(el('div', { class: 'label' }, label));
    const valueNode = el('div', { class: 'value' + (valueClass ? ' ' + valueClass : '') }, value);
    setFormula(valueNode, title);
    node.appendChild(valueNode);
    const footNode = tileFoot(foot);
    if (footNode) node.appendChild(footNode);
    return node;
  }

  function renderPerformance(data) {
    const p = data.performance;
    const notes = $('spreads-notes');
    clear(notes);
    for (const note of data.notes) notes.appendChild(el('li', {}, note));
    notes.appendChild(el('li', {}, 'Realized figures and win rate cover spreads closed in the selected date range; premium covers spreads opened in it.'));

    const tiles = $('spreads-tiles');
    clear(tiles);
    tiles.appendChild(perfTile('Net premium collected', usd(p.net_premium_collected_opened_in_range),
      formula([
        'Net premium collected = premium collected - opening fees',
        `= ${usd(p.premium_collected_opened_in_range)} - ${usd(p.open_fees_opened_in_range)}`,
        `= ${usd(p.net_premium_collected_opened_in_range)}`,
        '  Spreads opened in range. A credit received is not profit until the spread closes.',
      ]),
      [['Gross premium', usd(p.premium_collected_opened_in_range)], ['Spreads opened', p.spreads_opened_in_range]]));
    tiles.appendChild(perfTile('Net realized P/L', usd(p.realized_net),
      formula(['Net realized P/L = gross realized - fees', `= ${usd(p.realized_gross)} - ${usd(p.realized_fees)}`, `= ${usd(p.realized_net)}`]),
      [['Gross', usd(p.realized_gross)], ['Fees', usd(p.realized_fees)]], signClass(p.realized_net)));
    tiles.appendChild(perfTile('Unrealized P/L', usd(p.unrealized),
      formula(['Unrealized P/L = (credit - close now) × 100 × contracts, open spreads', '  Before closing fees.']),
      [['Open spreads', p.open_count], ...(p.unrealized_unpriced_count ? [['Without a quote', p.unrealized_unpriced_count]] : [])],
      signClass(p.unrealized)));
    tiles.appendChild(perfTile('Win rate', pct(p.win_rate_pct),
      formula([
        'Win rate = closed spreads with net P/L above $0 ÷ closed spreads', `= ${p.win_count} ÷ ${p.closed_count}`, `= ${pct(p.win_rate_pct)}`,
        ...(p.unsettled_closed_count ? ['  Spreads assigned on one leg only are left out of every P/L figure: their result depends on the share trade.'] : []),
      ]),
      [['Closed', p.closed_count], ['Losses', p.loss_count],
        ...(p.unsettled_closed_count ? [['Not settled by the spread', p.unsettled_closed_count]] : [])]));
    tiles.appendChild(perfTile('Avg per closed spread', usd(p.avg_net_per_closed),
      formula(['Avg per closed spread = net realized P/L of closed spreads ÷ closed spreads']),
      [['Avg win', usd(p.avg_win)], ['Avg loss', usd(p.avg_loss)]], signClass(p.avg_net_per_closed)));
    tiles.appendChild(perfTile('Profit factor', p.profit_factor === null ? '—' : p.profit_factor.toFixed(2),
      formula(['Profit factor = total of winning spreads ÷ total lost on losing spreads', '  Blank with no losing spread.']),
      [['Largest loss', usd(p.largest_loss)]]));
    tiles.appendChild(perfTile('Return on max risk', pct(p.return_on_max_risk_pct),
      formula(['Return on max risk = net realized P/L of closed spreads ÷ their max loss', `  Max loss total: ${usd(p.max_risk_closed)}`]),
      [['Avg days held', p.avg_holding_days === null ? '—' : p.avg_holding_days.toFixed(1)]], signClass(p.return_on_max_risk_pct)));
    tiles.appendChild(perfTile('Max drawdown', usd(p.max_drawdown ? -p.max_drawdown : 0),
      formula(['Max drawdown = largest drop in cumulative net realized P/L from its running peak', '  Close by close, starting from $0.']),
      null, p.max_drawdown ? 'neg' : ''));

    const exits = $('spreads-exits');
    clear(exits);
    exits.appendChild(el('span', { class: 'spreads-rec-sub' }, 'How closed spreads exited (judged with the current settings):'));
    for (const row of p.exit_counts) exits.appendChild(el('span', {}, `${row.label}: ${row.count}`));

    const host = $('spreads-closed');
    clear(host);
    const rows = data.closed_spreads;
    if (!rows.length) {
      host.appendChild(el('p', { class: 'hint' }, 'No credit spreads closed in the selected date range.'));
      return;
    }
    const combined = rows.some((r) => r.account_label);
    const head = [
      'Spread', 'Opened / closed', 'Credit', 'Net collected', 'Breakeven', 'Closing debit', 'Net P/L', 'Exit rule',
    ];
    const body = rows.map((r) => [
      {
        text: `${r.ticker} ${strikes(r)}`,
        sub: [r.strategy_label, `${r.contracts} contract${r.contracts === 1 ? '' : 's'}`, ...(combined ? [r.account_label] : [])].join(', '),
        title: r.trades_tip,
      },
      {
        text: `${shortDate(r.open_date)} → ${shortDate(r.close_date)}`,
        sub: `${r.holding_days} day${r.holding_days === 1 ? '' : 's'} held`,
      },
      usd(r.credit),
      { text: usd(r.net_premium_collected), title: r.net_collected_tip },
      { text: usd(r.breakeven), title: r.breakeven_tip },
      usd(r.closing_debit),
      r.result_settled ? {
        text: usd(r.realized_net),
        sub: `Gross ${usd(r.realized_gross)}, fees ${usd(r.realized_fees)}`,
        class: signClass(r.realized_net),
        title: formula(['Net P/L = gross P/L - fees', `= ${usd(r.realized_gross)} - ${usd(r.realized_fees)}`, `= ${usd(r.realized_net)}`]),
      } : { text: 'Not settled', sub: r.result_note },
      r.exit_label,
    ]);
    host.appendChild(table(head, body));
  }

  /* ------------------------------------------------------------ page */

  function renderSpreads() {
    const data = spreads.data;
    if (!data) return;
    const status = $('spreads-status');
    clear(status);
    const line = el('p', { class: 'hint' });
    const equity = data.account_equity;
    line.textContent =
      `Sizing uses ${equity.value === null ? 'no account value' : money(equity.value)}: ${equity.source}. ` +
      `Calculated ${new Date(data.calculated_at).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' })}. ` +
      `Fees assumed: ${usd(data.fee_per_contract)} per contract per leg.`;
    status.appendChild(line);
    if (data.warnings.length) {
      const notice = el('div', { class: 'notice' });
      notice.appendChild(el('strong', {}, 'Some market data could not be loaded. '));
      notice.appendChild(document.createTextNode(data.warnings.join(' ')));
      status.appendChild(notice);
    }

    renderSettings(data);
    renderScan(data);
    renderCheckForm(data);
    renderCheckResult(data);
    renderOpen(data);
    renderRecommendations(data);
    renderPerformance(data);
    if (typeof applySearchFilter === 'function') applySearchFilter();
  }

  global.loadSpreads = loadSpreads;
  global.renderSpreads = renderSpreads;
  global.sanitizeSpreadSettings = sanitizeSpreadSettings;
})(window);
