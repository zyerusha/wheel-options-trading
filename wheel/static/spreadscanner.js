/* Credit Spreads tab: Top Opportunities.
 *
 * Display only: every score, rating, reason and number comes from
 * /api/spreads/opportunities (wheel/spreadscanner.py). A filter change asks the
 * server to re-rank the last scan, with no market download; only "Run scan now"
 * and "Refresh top opportunities" download anything.
 *
 * Loaded before app.js and uses its shared helpers at call time. spreads.js
 * calls loadSpreadScanner() whenever the tab loads.
 *
 * Settings: `state.creditSpreadScanner` (saved in data/config.json) holds only
 * what the reader changed; the backend owns every default.
 */
(function (global) {
  'use strict';

  // Must match SCANNER_API_VERSION in wheel/spreadscanner.py.
  const SCANNER_API_VERSION = 1;
  const POLL_WHILE_RUNNING_MS = 1500;
  // Re-ranking is local, but open positions rarely change between price refreshes.
  const BACKGROUND_REFRESH_MS = 30000;
  // The exit rules are the tab's Strategy settings, so both sections agree.
  const MANAGEMENT_KEYS = ['profit_target_pct', 'loss_multiple', 'time_exit_dte'];

  const FILTER_GROUPS = [
    {
      title: 'What to show',
      hint: 'Applied right away to the last scan. A spread that fails one of these is rejected.',
      fields: [
        { key: 'strategy', label: 'Spread type', options: [['BOTH', 'Both'], ['BULL_PUT', 'Bull put'], ['BEAR_CALL', 'Bear call']] },
        { key: 'asset_class', label: 'Stocks or ETFs', options: [['BOTH', 'Both'], ['ETF', 'ETFs only'], ['STOCK', 'Stocks only']] },
        { key: 'min_pop', label: 'Min chance of profit %' },
        { key: 'min_ror', label: 'Min return on risk %' },
        { key: 'min_ann_ror', label: 'Min annualized return %' },
        { key: 'delta_min', label: 'Lowest short delta', hint: 'In delta points: 10 = 0.10.' },
        { key: 'delta_max', label: 'Highest short delta', hint: 'In delta points: 25 = 0.25.' },
        { key: 'dte_min', label: 'DTE from', scan: true },
        { key: 'dte_max', label: 'DTE to', scan: true },
        { key: 'min_credit', label: 'Min credit $', step: 0.05 },
        { key: 'max_width', label: 'Max width $' },
        { key: 'min_liquidity', label: 'Min liquidity (0-100)' },
        { key: 'min_oi', label: 'Min open interest per leg' },
        { key: 'min_score', label: 'Min score' },
        { key: 'earnings_policy', label: 'Earnings before expiration', options: [['penalize', 'Allow, but lower the score'], ['exclude', 'Reject the trade']] },
      ],
    },
    {
      title: 'How to rank',
      hint: 'These order the list; they never reject a trade.',
      fields: [
        { key: 'top_n', label: 'Opportunities shown' },
        { key: 'max_per_ticker', label: 'Most per ticker' },
        { key: 'max_per_sector', label: 'Most per sector' },
        { key: 'dte_pref_min', label: 'Preferred DTE from', scan: true },
        { key: 'dte_pref_max', label: 'Preferred DTE to', scan: true },
      ],
    },
    {
      title: 'Your risk limits',
      hint: '% of account value, measured by max loss. Going over warns you and lowers the score; it never hides the trade. Buying power is not the same as acceptable risk.',
      fields: [
        { key: 'max_trade_pct', label: 'Max per trade %', step: 0.25 },
        { key: 'max_total_pct', label: 'Max all spreads %' },
        { key: 'max_ticker_pct', label: 'Max per ticker %' },
        { key: 'max_sector_pct', label: 'Max per sector %' },
      ],
    },
    {
      title: 'What to scan',
      hint: 'These decide what gets downloaded, so they apply on the next scan.',
      fields: [
        { key: 'min_price', label: 'Stock price from $', scan: true },
        { key: 'max_price', label: 'Stock price to $', scan: true },
        { key: 'min_cap_b', label: 'Min market cap $B', scan: true },
        { key: 'min_vol_m', label: 'Min avg volume (M shares)', scan: true },
        { key: 'max_stocks', label: 'Stocks to scan', scan: true, hint: 'The most actively traded that pass the screen. More takes longer.' },
        { key: 'max_expirations', label: 'Expirations per ticker', scan: true },
        { key: 'etfs', label: 'ETFs', list: true, scan: true, hint: 'Ticker symbols, comma separated.' },
      ],
    },
  ];
  const ALL_FIELDS = FILTER_GROUPS.flatMap((g) => g.fields);
  const FIELD_KEYS = new Set(ALL_FIELDS.map((f) => f.key));

  const scan = {
    data: null,
    generation: 0,
    lastFetchAt: 0,
    lastAccount: null,
    expanded: new Set(),
    formBuilt: false,
    pollTimer: null,
    autoStarted: false,
    message: null,
  };

  /* ------------------------------------------------------------ settings */

  /** Known keys with plain string values only: config.json isn't hand-edit proof. */
  function sanitizeScannerSettings(raw) {
    const out = {};
    if (!raw || typeof raw !== 'object') return out;
    for (const [key, value] of Object.entries(raw)) {
      if (FIELD_KEYS.has(key) && typeof value === 'string' && value.length <= 300) out[key] = value;
    }
    return out;
  }

  function scannerQuery() {
    const params = new URLSearchParams();
    const account = dashboardQuery().get('account');
    if (account) params.set('account', account);
    for (const [key, value] of Object.entries(state.creditSpreadScanner || {})) params.set(key, value);
    for (const key of MANAGEMENT_KEYS) {
      const value = (state.creditSpreads || {})[key];
      if (value) params.set(key, value);
    }
    return params;
  }

  function buildFilterForm() {
    const host = $('scanner-filter-groups');
    clear(host);
    for (const group of FILTER_GROUPS) {
      const section = el('div', { class: 'scanner-filter-group' });
      section.appendChild(el('h4', {}, group.title));
      section.appendChild(el('p', { class: 'hint' }, group.hint));
      const grid = el('div', { class: 'spreads-settings' });
      for (const field of group.fields) {
        const wrap = el('div', { class: 'field' });
        const id = `scanner-setting-${field.key}`;
        wrap.appendChild(el('label', { for: id }, field.label));
        let input;
        if (field.options) {
          input = el('select', { id });
          for (const [value, text] of field.options) input.appendChild(el('option', { value }, text));
        } else if (field.list) {
          input = el('input', { id, type: 'text' });
        } else {
          input = el('input', { id, type: 'number', step: field.step || 1, inputmode: 'decimal' });
        }
        if (field.hint) input.title = field.hint;
        input.addEventListener('change', () => {
          const typed = input.value.trim();
          const overrides = { ...(state.creditSpreadScanner || {}) };
          if (typed === '') delete overrides[field.key];
          else overrides[field.key] = typed;
          state.creditSpreadScanner = overrides;
          queueConfigSave();
          loadSpreadScanner();
        });
        wrap.appendChild(input);
        grid.appendChild(wrap);
      }
      section.appendChild(grid);
      host.appendChild(section);
    }
    const reset = el('button', { class: 'toggle', type: 'button' }, 'Reset to defaults');
    reset.addEventListener('click', () => {
      state.creditSpreadScanner = {};
      queueConfigSave();
      loadSpreadScanner();
    });
    host.appendChild(reset);
    scan.formBuilt = true;
  }

  function renderFilters(data) {
    if (!scan.formBuilt) buildFilterForm();
    for (const field of ALL_FIELDS) {
      const input = $(`scanner-setting-${field.key}`);
      const value = data.settings[field.key];
      if (input && document.activeElement !== input && value !== undefined) {
        input.value = field.list ? value.join(', ') : String(value);
      }
    }
    const notes = $('scanner-settings-warnings');
    clear(notes);
    if (data.settings_warnings.length) {
      const notice = el('div', { class: 'notice' });
      notice.appendChild(el('strong', {}, 'Some settings were not used. '));
      notice.appendChild(document.createTextNode(data.settings_warnings.join(' ')));
      notes.appendChild(notice);
      $('scanner-filters').open = true;
    }
  }

  /* ------------------------------------------------------------ loading */

  async function loadSpreadScanner({ background = false } = {}) {
    const account = dashboardQuery().get('account');
    const sameAccount = account === scan.lastAccount;
    if (background && sameAccount && Date.now() - scan.lastFetchAt < BACKGROUND_REFRESH_MS) return;
    const generation = ++scan.generation;
    scan.lastFetchAt = Date.now();
    scan.lastAccount = account;
    try {
      const response = await fetch('/api/spreads/opportunities?' + scannerQuery().toString());
      if (response.status === 404) throw new Error('old server');
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'HTTP ' + response.status);
      if (data.api_version !== SCANNER_API_VERSION) throw new Error('old server');
      if (generation !== scan.generation) return;
      scan.data = data;
      renderScanner();
      schedulePoll(data);
      // The first look at the tab starts the research; after that only a click does.
      if (!data.has_results && !data.scan.running && !data.scan.error && !scan.autoStarted) {
        scan.autoStarted = true;
        startScan('full');
      }
    } catch (error) {
      if (generation !== scan.generation) return;
      const host = $('scanner-status');
      clear(host);
      const notice = el('div', { class: 'notice err' });
      notice.appendChild(el('strong', {}, 'Could not load opportunities. '));
      notice.appendChild(
        document.createTextNode(
          error.message === 'old server'
            ? 'The dashboard server is running older code than this page. Stop it and run python -m wheel.serve again, then reload.'
            : error.message
        )
      );
      host.appendChild(notice);
    }
  }

  function schedulePoll(data) {
    if (scan.pollTimer) clearTimeout(scan.pollTimer);
    scan.pollTimer = null;
    if (data.scan.running && !$('tab-spreads').hidden) {
      scan.pollTimer = setTimeout(() => loadSpreadScanner(), POLL_WHILE_RUNNING_MS);
    }
  }

  async function startScan(mode) {
    const params = scannerQuery();
    params.set('mode', mode);
    try {
      const response = await fetch('/api/spreads/scan?' + params.toString(), { method: 'POST' });
      const reply = await response.json();
      if (!response.ok) throw new Error(reply.error || 'HTTP ' + response.status);
      scan.message = reply.started ? null : reply.message;
    } catch (error) {
      scan.message = 'Could not start the scan: ' + error.message;
    }
    await loadSpreadScanner();
  }

  /* ------------------------------------------------------------ helpers */

  const usd = (value) => money(value, { cents: true });
  const usdWhole = (value) => money(value);
  const strikeText = (row) => `${row.short_strike}/${row.long_strike}`;
  const clockTime = (iso) => new Date(iso).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
  const RATING_KIND = { Excellent: 'ok', 'Very Good': 'ok', Good: 'unknown', Watch: 'review', Rejected: 'fail' };

  function shortDay(iso) {
    return iso && iso.slice(0, 4) === String(new Date().getFullYear()) ? dayLabel(iso) : longDate(iso);
  }

  function ratingBadge(rating) {
    return el('span', { class: `badge spread-status ${RATING_KIND[rating] || 'unknown'}` }, rating);
  }

  function factsList(rows) {
    const list = el('dl', { class: 'scanner-facts' });
    for (const [name, value, tip] of rows) {
      const term = el('dt', {}, name);
      const detail = el('dd', {}, value === null || value === undefined ? '—' : String(value));
      if (tip) setFormula(detail, tip);
      list.appendChild(term);
      list.appendChild(detail);
    }
    return list;
  }

  function bullets(items) {
    const list = el('ul', { class: 'hint-list' });
    items.forEach((text) => list.appendChild(el('li', {}, text)));
    return list;
  }

  function detailSection(title, ...children) {
    const section = el('section', { class: 'scanner-detail-section' });
    section.appendChild(el('h4', {}, title));
    children.filter(Boolean).forEach((child) => section.appendChild(child));
    return section;
  }

  /** A section with a table or chart that needs the panel's full width. */
  function wide(section) {
    section.classList.add('wide');
    return section;
  }

  /* ------------------------------------------------------------ status */

  function renderStatus(data) {
    const host = $('scanner-status');
    clear(host);
    const s = data.scan;
    const parts = [];
    if (s.last_scan_at) {
      parts.push(`Last scan ${clockTime(s.last_scan_at)}, took ${Math.round(s.duration_seconds)} sec.`);
      if (s.verified_at) parts.push(`Top quotes checked ${clockTime(s.verified_at)}.`);
    } else if (!s.running) {
      parts.push('No scan yet.');
    }
    if (s.running) {
      const count = s.total ? ` (${s.done.toLocaleString()} of ${s.total.toLocaleString()})` : '';
      const progress = el('div', { class: 'scanner-progress' });
      progress.appendChild(el('strong', {}, `${s.stage}${count}`));
      if (s.total) {
        const bar = el('progress', { max: String(s.total), value: String(s.done) });
        bar.setAttribute('aria-label', 'Scan progress');
        progress.appendChild(bar);
      }
      host.appendChild(progress);
    }
    if (parts.length) host.appendChild(el('p', { class: 'hint' }, parts.join(' ')));
    if (s.last_scan_at) {
      const counts = el('div', { class: 'spreads-exits' });
      [
        ['Tickers looked at', s.universe_size],
        ['Passed the stock screen', s.stocks_passed],
        ['Stocks and ETFs scanned', s.underlyings_scanned],
        ['Option chains read', s.chains_fetched],
        ['Spreads checked', data.evaluated],
        ['Qualified', data.qualified],
      ].forEach(([name, value]) => {
        counts.appendChild(el('span', {}, `${name}: ${value === undefined || value === null ? '—' : value.toLocaleString()}`));
      });
      host.appendChild(counts);
      host.appendChild(el('p', { class: 'hint' }, `Data: ${s.data_source}. ${s.verify_note || ''}`));
    }
    const notices = [];
    if (s.error) notices.push(['err', s.error]);
    if (scan.message) notices.push(['', scan.message]);
    if (data.rescan_needed) notices.push(['', 'You changed what to scan. These results still use the old settings until you run a new scan.']);
    const counted = data.quote_counts || {};
    if ((counted.STALE || 0) + (counted.UNAVAILABLE || 0) > 0) {
      notices.push(['', "Some quotes are old, usually because the market is closed. Those trades are held at Watch until quotes are current: run a scan during market hours before acting."]);
    } else if (counted.CACHED) {
      notices.push(['', 'These quotes are from earlier in this scan. Use Refresh top opportunities for current prices.']);
    }
    if ((data.warnings || []).length) notices.push(['', 'Some data could not be loaded: ' + data.warnings.join(' ')]);
    for (const [kind, text] of notices) host.appendChild(el('div', { class: `notice ${kind}` }, text));

    const running = Boolean(s.running);
    $('scanner-run').disabled = running;
    $('scanner-refresh').disabled = running || !data.has_results;
  }

  /* ------------------------------------------------------------ top list */

  function renderTop(data) {
    const host = $('scanner-top');
    clear(host);
    if (!data.has_results) {
      if (!data.scan.running) host.appendChild(el('p', { class: 'hint' }, 'Run a scan to see the best trades right now.'));
      return;
    }
    const rows = data.opportunities;
    if (!rows.length) {
      host.appendChild(
        el('p', { class: 'hint' }, 'No spread passes your rules right now. That is a real answer: look under "Why trades were rejected" to see what stopped them, or loosen a filter.')
      );
      return;
    }
    const table = el('table', { class: 'spreads-table scanner-table' });
    const thead = el('thead');
    const headRow = el('tr');
    ['#', 'Ticker', 'Type', 'Price', 'Strikes', 'Expires', 'Credit', 'Max profit / loss', 'Chance of profit (est.)', 'Return on risk / annualized', 'Short delta', 'Score'].forEach(
      (label, index) => headRow.appendChild(el('th', { class: index < 2 ? 'left' : '' }, label))
    );
    thead.appendChild(headRow);
    table.appendChild(thead);
    const tbody = el('tbody');
    for (const row of rows) {
      const open = scan.expanded.has(row.id);
      const tr = el('tr', { class: 'scanner-row has-detail', tabindex: '0', 'aria-expanded': String(open) });
      const toggle = () => {
        if (scan.expanded.has(row.id)) scan.expanded.delete(row.id);
        else scan.expanded.add(row.id);
        renderTop(scan.data);
      };
      tr.addEventListener('click', (event) => {
        if (!event.target.closest('a,button')) toggle();
      });
      tr.addEventListener('keydown', (event) => {
        if (event.target === tr && (event.key === 'Enter' || event.key === ' ')) {
          event.preventDefault();
          toggle();
        }
      });
      const cell = (primary, sub, { left = false, tip = null } = {}) => {
        const td = el('td', { class: left ? 'left' : 'num' });
        if (primary instanceof Node) td.appendChild(primary);
        else td.appendChild(el('div', {}, primary));
        if (sub) td.appendChild(el('div', { class: 'sub' }, sub));
        if (tip) setFormula(td, tip);
        tr.appendChild(td);
      };
      cell(String(row.rank), null, { left: true });
      cell(tickerLink(row.ticker), row.is_etf ? 'ETF' : row.sector || 'Sector unknown', { left: true, tip: row.name });
      cell(row.strategy_label);
      cell(usd(row.spot), null, { tip: row.quote_label });
      cell(strikeText(row), `Width ${usd(row.width)}`);
      cell(shortDay(row.expiry), `${row.dte} DTE`);
      cell(usd(row.credit), `Mid ${usd(row.credit_mid)}`, { tip: formula([
        'Credit = short bid - long ask',
        `= ${usd(row.short_leg.bid)} - ${usd(row.long_leg.ask)}`,
        `= ${usd(row.credit)}`,
        '',
        'The midpoint is an estimate of a better fill.',
      ]) });
      cell(usd(row.max_profit), usd(row.max_loss), { tip: formula([
        `Max profit: ${usd(row.max_profit)}`,
        `Max loss: ${usd(row.max_loss)}`,
        '  Per spread, before fees.',
      ]) });
      cell(pct(row.pop_pct, 0), null, { tip: row.pop_model_note });
      cell(pct(row.ror_pct), `${pct(row.annualized_ror_pct, 0)} a year`, { tip: formula([
        'Return on risk = credit ÷ (width - credit)',
        `= ${usd(row.credit)} ÷ (${usd(row.width)} - ${usd(row.credit)})`,
        `= ${pct(row.ror_pct)}`,
        '',
        'Annualized = return on risk × 365 ÷ DTE',
        `= ${pct(row.ror_pct)} × 365 ÷ ${row.dte}`,
        `= ${pct(row.annualized_ror_pct, 0)}`,
      ]) });
      cell(row.short_delta.toFixed(1));
      const score = el('div', { class: 'scanner-score' });
      score.appendChild(el('strong', {}, row.score.toFixed(0)));
      score.appendChild(ratingBadge(row.rating));
      cell(score, null, { tip: 'Open the row to see exactly how this score was built.' });
      tbody.appendChild(tr);

      const why = el('tr', { class: 'spread-detail' });
      why.appendChild(el('td', { class: 'left', colspan: '12' }, row.explanation));
      tbody.appendChild(why);
      if (open) {
        const detailRow = el('tr', { class: 'scanner-detail-row' });
        const td = el('td', { class: 'left', colspan: '12' });
        detailRow.appendChild(td);
        tbody.appendChild(detailRow);
        td.appendChild(detailPanel(row));
      }
    }
    table.appendChild(tbody);
    host.appendChild(table);
    // Charts measure their container, so they draw once the table is in the page.
    for (const row of rows) {
      if (scan.expanded.has(row.id)) drawPriceChart(row);
    }
  }

  /* ------------------------------------------------------------ expanded analysis */

  function detailPanel(row) {
    const panel = el('div', { class: 'scanner-detail' });
    const m = row.management;
    const chartHost = el('div', { class: 'scanner-chart' });
    const svg = svgEl('svg', { id: chartId(row), role: 'img', 'aria-label': `${row.ticker} daily closing prices with this spread's strikes` });
    chartHost.appendChild(svg);
    panel.appendChild(wide(detailSection(`${row.ticker} price, last ${row.chart.length} sessions`, chartHost, row.history_note ? el('p', { class: 'hint' }, row.history_note) : null)));

    panel.appendChild(detailSection('Trade', factsList([
      ['Price', `${usd(row.spot)}, ${row.distance_pct}% from the short strike`],
      ['Breakeven at expiration', usd(row.breakeven)],
      ['Credit, conservative', usd(row.credit)],
      ['Credit at midpoint (estimate)', usd(row.credit_mid)],
      ['Max profit', usd(row.max_profit)],
      ['Max loss', usd(row.max_loss)],
      ['Return on risk', pct(row.ror_pct)],
      ['Annualized return', pct(row.annualized_ror_pct, 0)],
      ['Fees to open, close', `${usd(row.open_fees)}, ${usd(row.round_trip_fees - row.open_fees)}`],
      ['Suggested contracts', row.suggested_contracts],
    ])));

    panel.appendChild(detailSection('Odds and value', factsList([
      ['Chance of profit (model)', row.pop_pct === null ? 'Unavailable' : pct(row.pop_pct, 0), row.pop_model_note],
      ['Rough check: 1 - delta', pct(row.pop_delta_pct, 0), 'Looks at the short strike only, not the breakeven, so it is just a cross-check.'],
      ['Broker probability', 'Not available from Yahoo'],
      ['Chance of touching the short strike', row.touch_pct === null ? 'Unavailable' : `About ${pct(row.touch_pct, 0)}`, 'Rough rule: about twice the chance of finishing past the short strike.'],
      ['Expected value per spread', row.expected_value === null ? 'Unavailable' : usd(row.expected_value), formula([
        'EV = chance of profit × max profit - chance of loss × max loss - fees',
        `Fees: ${usd(row.round_trip_fees)} to open and close`,
      ])],
    ]), el('p', { class: 'hint' }, row.expected_value_note)));

    const zoneText = (zones, name) => zones.length
      ? zones.map((z) => `${z.text} (strength ${z.score}, ${z.touches} touch${z.touches === 1 ? '' : 'es'})`).join('; ')
      : `No clear ${name} level found`;
    const ma = row.moving_averages;
    panel.appendChild(detailSection('Technicals', el('p', {}, row.technical_text), factsList([
      ['Support', zoneText(row.support, 'support')],
      ['Resistance', zoneText(row.resistance, 'resistance')],
      ['20-day average', ma ? usd(ma.ma20) : null],
      ['50-day average', ma ? usd(ma.ma50) : null],
      ['200-day average', ma ? usd(ma.ma200) : null],
    ])));

    panel.appendChild(detailSection('Volatility and events', factsList([
      ['Implied volatility', pct(row.iv_pct)],
      ['30-day realized volatility', pct(row.realized_vol_pct)],
      ['IV rank', 'Unavailable', row.iv_rank_note],
      ['Next earnings', row.is_etf ? 'None (ETF)' : row.earnings_date ? `${longDate(row.earnings_date)}${row.earnings_in_window ? ', before expiration' : ', after expiration'}` : 'Unknown'],
    ]), el('p', { class: 'hint' }, 'Only earnings are tracked: Yahoo has no feed for other scheduled events.')));

    panel.appendChild(detailSection('When to close', factsList([
      [`Take profit at ${m.profit_target_pct}%`, `Close for ${usd(m.target_debit)} or less, keeping ${usd(m.target_profit)} before fees`],
      ['Risk alert', `Cost to close reaches ${usd(m.loss_trigger)} (${m.loss_multiple}× credit), a loss of ${usd(m.loss_at_trigger)} before fees`],
      ['Review date', `${longDate(m.review_date)} (${m.review_dte} DTE)`],
    ]), el('p', { class: 'hint' }, m.strike_warning)));

    const legs = el('table', { class: 'spreads-table' });
    const legHead = el('tr');
    ['Leg', 'Strike', 'Bid', 'Ask', 'Mid', 'IV', 'Open interest', "Today's volume"].forEach((h, i) => legHead.appendChild(el('th', { class: i ? '' : 'left' }, h)));
    const legHeadWrap = el('thead');
    legHeadWrap.appendChild(legHead);
    legs.appendChild(legHeadWrap);
    const legBody = el('tbody');
    for (const [name, leg] of [['Sell', row.short_leg], ['Buy', row.long_leg]]) {
      const tr = el('tr');
      [name, leg.strike, usd(leg.bid), usd(leg.ask), usd(leg.mid), pct(leg.iv_pct),
        leg.open_interest === null ? '—' : leg.open_interest.toLocaleString(),
        leg.volume === null ? '—' : leg.volume.toLocaleString(),
      ].forEach((value, i) => tr.appendChild(el('td', { class: i ? 'num' : 'left' }, String(value))));
      legBody.appendChild(tr);
    }
    legs.appendChild(legBody);
    panel.appendChild(wide(detailSection('Option quotes', legs, el('p', { class: 'hint' }, row.quote_label), bullets(row.liquidity_notes))));

    const scoreTable = el('table', { class: 'spreads-table' });
    const scoreHead = el('thead');
    const scoreHeadRow = el('tr');
    ['Part of the score', 'Points', 'Why'].forEach((h, i) => scoreHeadRow.appendChild(el('th', { class: i === 1 ? '' : 'left' }, h)));
    scoreHead.appendChild(scoreHeadRow);
    scoreTable.appendChild(scoreHead);
    const scoreBody = el('tbody');
    for (const c of row.components) {
      const tr = el('tr');
      const label = el('td', { class: 'left' }, c.label);
      setFormula(label, c.how);
      tr.appendChild(label);
      tr.appendChild(el('td', { class: 'num' }, `${c.points.toFixed(1)} of ${c.weight}`));
      tr.appendChild(el('td', { class: 'left spread-why' }, c.detail));
      scoreBody.appendChild(tr);
    }
    for (const p of row.penalties) {
      const tr = el('tr');
      tr.appendChild(el('td', { class: 'left' }, p.label));
      tr.appendChild(el('td', { class: 'num neg' }, `less ${p.points.toFixed(1)}`));
      tr.appendChild(el('td', { class: 'left spread-why' }, p.detail));
      scoreBody.appendChild(tr);
    }
    const total = el('tr', { class: 'scanner-score-total' });
    total.appendChild(el('td', { class: 'left' }, 'Score'));
    total.appendChild(el('td', { class: 'num' }, row.score.toFixed(1)));
    total.appendChild(el('td', { class: 'left spread-why' }, `Rating: ${row.rating}.`));
    scoreBody.appendChild(total);
    scoreTable.appendChild(scoreBody);
    panel.appendChild(wide(detailSection('How the score was built', scoreTable)));

    panel.appendChild(detailSection('Why it qualifies', row.reasons.length ? bullets(row.reasons) : el('p', { class: 'hint' }, 'It clears your rules, but no part of it stands out.')));
    panel.appendChild(detailSection('Risks and warnings', bullets(row.risks)));

    const actions = el('div', { class: 'scanner-actions' });
    const send = el('button', { class: 'toggle', type: 'button' }, 'Send to Check a spread');
    send.addEventListener('click', () => {
      global.checkSpreadFromScanner({
        ticker: row.ticker,
        expiry: row.expiry,
        strategy: row.strategy,
        short: String(row.short_strike),
        long: String(row.long_strike),
      });
    });
    actions.appendChild(send);
    actions.appendChild(el('span', { class: 'hint' }, 'Tests it against your Strategy settings, which may be stricter. This never places an order.'));
    panel.appendChild(actions);
    return panel;
  }

  function chartId(row) {
    return 'scanner-chart-' + row.id.replace(/[^A-Za-z0-9]/g, '-');
  }

  /** One series, so no legend: the section title names it and the reference
   *  lines carry their own labels. Colours follow the app's chart tokens. */
  function drawPriceChart(row) {
    const svg = $(chartId(row));
    if (!svg || !row.chart.length) return;
    clear(svg);
    const points = row.chart;
    const closes = points.map((p) => p.close);
    const zone = (row.strategy === 'BULL_PUT' ? row.support : row.resistance)[0] || null;
    const levels = [
      { value: row.short_strike, label: `Short ${row.short_strike}` },
      { value: row.long_strike, label: `Long ${row.long_strike}` },
    ];
    const all = [...closes, ...levels.map((l) => l.value), ...(zone ? [zone.low, zone.high] : [])];
    const span = Math.max(...all) - Math.min(...all) || 1;
    const margin = { top: 12, right: 72, bottom: 26, left: 58 };
    const width = chartWidth(svg, 280);
    const height = 220;
    const { group, plotWidth, plotHeight, y } = frame(svg, {
      width, height, margin, yMin: Math.min(...all) - span * 0.05, yMax: Math.max(...all) + span * 0.05,
    });
    const x = (index) => margin.left + (points.length > 1 ? (index / (points.length - 1)) * plotWidth : plotWidth / 2);
    if (zone) {
      group.appendChild(svgEl('rect', {
        x: margin.left, width: plotWidth, y: y(zone.high), height: Math.max(1, y(zone.low) - y(zone.high)),
        fill: cssVar('--accent-soft'),
      }));
      group.appendChild(svgEl('text', { x: margin.left + 6, y: y(zone.high) + 12, class: 'tick-label' }, row.strategy === 'BULL_PUT' ? 'Support' : 'Resistance'));
    }
    // Keep the two strike labels from landing on each other when strikes are close.
    const labelY = levels.map((l) => y(l.value) + 4);
    if (Math.abs(labelY[0] - labelY[1]) < 13) {
      const shortIsHigher = labelY[0] < labelY[1];
      const middle = (labelY[0] + labelY[1]) / 2;
      labelY[0] = middle + (shortIsHigher ? -7 : 7);
      labelY[1] = middle + (shortIsHigher ? 7 : -7);
    }
    levels.forEach((level, index) => {
      group.appendChild(svgEl('line', {
        x1: margin.left, x2: margin.left + plotWidth, y1: y(level.value), y2: y(level.value),
        stroke: cssVar('--text-muted'), 'stroke-width': 1, 'stroke-dasharray': index ? '2 3' : '5 3',
      }));
      group.appendChild(svgEl('text', { x: margin.left + plotWidth + 6, y: labelY[index], class: 'tick-label' }, level.label));
    });
    const path = points.map((p, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(p.close).toFixed(1)}`).join(' ');
    group.appendChild(svgEl('path', {
      d: path, fill: 'none', stroke: cssVar('--series-1'), 'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round',
    }));
    const yBase = margin.top + plotHeight + 16;
    group.appendChild(svgEl('text', { class: 'tick-label', x: margin.left, y: yBase }, dayLabel(points[0].day)));
    group.appendChild(svgEl('text', { class: 'tick-label', x: margin.left + plotWidth, y: yBase, 'text-anchor': 'end' }, dayLabel(points[points.length - 1].day)));
    const dot = svgEl('circle', { r: 4, fill: cssVar('--series-1'), opacity: 0 });
    group.appendChild(dot);
    const indexed = points.map((p, i) => ({ ...p, i }));
    crosshairLayer(svg, group, { margin, plotWidth, plotHeight, width }, indexed, (p) => x(p.i), {
      label: `${row.ticker} closing price by day. Arrow keys step through dates.`,
      onIndex: (index, at) => {
        const point = indexed[index];
        dot.setAttribute('cx', x(point.i));
        dot.setAttribute('cy', y(point.close));
        dot.setAttribute('opacity', 1);
        showTooltip(at, longDate(point.day), [{ label: 'Close', value: usd(point.close) }]);
      },
      onLeave: () => dot.setAttribute('opacity', 0),
    });
  }

  /* ------------------------------------------------------------ supporting sections */

  function renderAlso(data) {
    const host = $('scanner-also');
    clear(host);
    const rows = data.also_considered || [];
    $('scanner-also-card').hidden = !rows.length;
    if (!rows.length) return;
    const table = el('table', { class: 'spreads-table' });
    const head = el('thead');
    const headRow = el('tr');
    ['Trade', 'Expires', 'Credit', 'Chance of profit (est.)', 'Return on risk', 'Score', 'Why it is not listed'].forEach((h, i) => headRow.appendChild(el('th', { class: i === 0 || i === 6 ? 'left' : '' }, h)));
    head.appendChild(headRow);
    table.appendChild(head);
    const body = el('tbody');
    for (const row of rows) {
      const tr = el('tr');
      tr.appendChild(el('td', { class: 'left' }, `${row.ticker} ${row.strategy_label.toLowerCase()} ${strikeText(row)}`));
      tr.appendChild(el('td', { class: 'num' }, `${shortDay(row.expiry)}, ${row.dte} DTE`));
      tr.appendChild(el('td', { class: 'num' }, usd(row.credit)));
      tr.appendChild(el('td', { class: 'num' }, pct(row.pop_pct, 0)));
      tr.appendChild(el('td', { class: 'num' }, pct(row.ror_pct)));
      tr.appendChild(el('td', { class: 'num' }, `${row.score.toFixed(0)}, ${row.rating}`));
      tr.appendChild(el('td', { class: 'left spread-why' }, row.reason));
      body.appendChild(tr);
    }
    table.appendChild(body);
    host.appendChild(table);
  }

  function reasonList(title, rows) {
    if (!rows.length) return null;
    const wrap = el('div', {});
    wrap.appendChild(el('h4', {}, title));
    wrap.appendChild(bullets(rows.map((r) => `${r.reason}: ${r.count.toLocaleString()}`)));
    return wrap;
  }

  function renderRejected(data) {
    const host = $('scanner-rejected');
    clear(host);
    const r = data.rejected;
    $('scanner-rejected-card').hidden = !r;
    if (!r) return;
    [
      reasonList('Companies and ETFs not scanned', r.stock_reasons),
      reasonList('Spreads with unusable quotes', r.quote_reasons),
      reasonList('Spreads that failed your rules', r.spread_reasons),
    ].filter(Boolean).forEach((node) => host.appendChild(node));
    if (r.spread_examples.length) {
      host.appendChild(el('h4', {}, 'Examples'));
      host.appendChild(bullets(r.spread_examples.map((e) => `${e.ticker} ${e.strategy_label.toLowerCase()} ${e.short_strike}/${e.long_strike}, ${shortDay(e.expiry)}: ${e.reason}`)));
    }
    if (r.stock_examples.length) {
      host.appendChild(el('h4', {}, 'Tickers not scanned, examples'));
      host.appendChild(bullets(r.stock_examples.map((e) => `${e.ticker}: ${e.reason}`)));
    }
  }

  function renderContext(data) {
    const host = $('scanner-context');
    clear(host);
    const exposure = data.exposure;
    const usage = (row) => `${usdWhole(row.used)}${row.limit === null ? '' : ` of ${usdWhole(row.limit)} limit${row.over ? ', OVER LIMIT' : ''}`}`;
    if (exposure) {
      const rows = [
        ['Account value', exposure.account_value === null ? 'Not known' : usdWhole(exposure.account_value), exposure.account_source],
        ['Max loss, all open spreads', usage(exposure.total)],
        ...exposure.tickers.map((row) => [`In ${row.name}`, usage(row)]),
        ...exposure.sectors.map((row) => [`In ${row.name}`, usage(row)]),
      ];
      host.appendChild(detailSection('Your open spread risk', factsList(rows)));
    }
    if (data.market) {
      host.appendChild(detailSection('Market right now', factsList([
        ['SPY', data.market.spy],
        ['QQQ', data.market.qqq],
        ['VIX', data.market.vix],
        ['Sectors in an uptrend', data.market.sectors_up.join(', ') || 'None'],
        ['Sectors in a downtrend', data.market.sectors_down.join(', ') || 'None'],
      ]), el('p', { class: 'hint' }, data.market.iv_rank_note)));
    }
    const track = data.track_record;
    if (track) {
      const rows = [
        ['Trades recommended', track.recommended],
        ['Later traded', track.traded],
        ['Closed with a known result', track.closed],
        ['Actual win rate', pct(track.actual_win_rate_pct)],
        ['Average estimated chance of profit', pct(track.avg_estimated_pop_pct)],
      ];
      host.appendChild(detailSection('Scanner track record', factsList(rows), el('p', { class: 'hint' }, track.note)));
    }
    if (data.universe_sources) {
      host.appendChild(detailSection('Where the companies come from', bullets(
        data.universe_sources.map((s) => `${s.label}: ${s.count.toLocaleString()}${s.note ? `. ${s.note}` : ''}`)
      )));
    }
    const how = el('ul', { class: 'hint-list' });
    for (const c of data.score_categories) {
      const li = el('li');
      li.appendChild(el('strong', {}, `${c.label} (${c.weight})`));
      li.appendChild(document.createTextNode(`: ${c.how}`));
      how.appendChild(li);
    }
    const ratings = data.ratings.map((r) => `${r.rating} from ${r.min_score}`).join(', ');
    host.appendChild(detailSection('How scoring works', how, el('p', { class: 'hint' },
      `${ratings}; below that is rejected. Penalties for earnings, poor liquidity and what you already hold come off the total, and some problems cap the rating however high the score. The score ranks trades by fixed rules; it has not been shown to predict results.`
    )));
  }

  /* ------------------------------------------------------------ page */

  function renderScanner() {
    const data = scan.data;
    if (!data) return;
    renderFilters(data);
    renderStatus(data);
    renderTop(data);
    renderAlso(data);
    renderRejected(data);
    renderContext(data);
    if (typeof applySearchFilter === 'function') applySearchFilter();
  }

  function initScannerControls() {
    $('scanner-run').addEventListener('click', () => startScan('full'));
    $('scanner-refresh').addEventListener('click', () => startScan('refresh'));
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initScannerControls);
  else initScannerControls();

  global.loadSpreadScanner = loadSpreadScanner;
  global.renderSpreadScanner = renderScanner;
  global.sanitizeScannerSettings = sanitizeScannerSettings;
})(window);
