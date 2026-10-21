/* Tooltip layout: the one renderer for every text tooltip in the dashboard.
 *
 * Tips are authored as plain text (a `title` attribute, `setFormula`, or a
 * chart readout's `formula` argument) in one line convention, used across
 * app.js and the backend's `*_explanation` strings:
 *
 *   Name = formula with named inputs     a calculation starts
 *     ÷ continued formula                indented, starts with an operator
 *     Short qualifier.                   indented: a note on that calculation
 *   = $1,000 ÷ $2,000                    substitution with the real values
 *   = 50.0%                              the last "=" value is the result
 *                                        a blank line starts a new section
 *   Plain sentence.                      description (first section) or note
 *   Label: $1,234                        a labeled value row
 *   Heading:                             followed by indented "x = $y" lines:
 *     50% × $14.60 = $7.30               a list of calculation rows
 *   · item                               a bullet list
 *
 * `parse` turns that text into a structured model and `render` lays the
 * model out; `renderSpec` takes a model built directly by code, for tips the
 * text convention can't express (a table, a meter). Both share one set of
 * classes (`.tip*` in styles.css), so every tooltip has the same hierarchy:
 * title, calculation, result, then notes.
 *
 * Presentation only: every label and figure is shown exactly as authored.
 * The only rewrite is sign tidying in substitution lines ("+ -$5" reads
 * "- $5"), which is the same arithmetic. No number is parsed or recomputed.
 */
(function (global) {
  'use strict';

  const RELATIONS = [' = ', ' ≈ '];
  const SUM_OPERATORS = [' + ', ' - '];
  // A single displayed figure: $1,234.56, -$5, 12.3%, 286, $103.70/day,
  // 3 contracts, N/A, or the dash used for a missing value.
  const VALUE_RE =
    /^(?:N\/A|—|-?\$?-?(?:\d[\d,]*(?:\.\d+)?|\.\d+)(?:%|[kM])?)(?:\/day|\/share|\/y)?(?: (?:days?|contracts?|shares?))?$/;
  // A calculation's name stays short; longer text before " = " is a sentence.
  const MAX_NAME_LENGTH = 60;
  const MAX_NAME_WORDS = 8;
  const MAX_ROW_LABEL_LENGTH = 40;

  const isValue = (text) => VALUE_RE.test(text.trim());
  const isIndented = (raw) => /^\s/.test(raw);
  const isBullet = (text) => /^[·•] /.test(text);
  const startsLowercase = (text) => /^[a-z]/.test(text);
  const endsSentence = (text) => /[.!?:]$/.test(text);
  const continuesFormula = (text) => /^[+\-×÷*/]\s/.test(text);
  const leavesFormulaOpen = (text) => /[+\-×÷*/(,=]$/.test(text) || openParens(text) > 0;

  function openParens(text) {
    let depth = 0;
    for (const ch of text) {
      if (ch === '(') depth += 1;
      else if (ch === ')') depth -= 1;
    }
    return depth;
  }

  /** Split `text` on any of `separators` outside parentheses:
   * [{sep, text}], `sep` being the trimmed separator before each piece. */
  function splitTopLevel(text, separators) {
    const parts = [];
    let depth = 0;
    let start = 0;
    let sep = '';
    for (let i = 0; i < text.length; i += 1) {
      const ch = text[i];
      if (ch === '(' || ch === '[') depth += 1;
      else if ((ch === ')' || ch === ']') && depth > 0) depth -= 1;
      else if (depth === 0) {
        const found = separators.find((s) => text.startsWith(s, i));
        if (found) {
          parts.push({ sep, text: text.slice(start, i).trim() });
          sep = found.trim();
          start = i + found.length;
          i = start - 1;
        }
      }
    }
    parts.push({ sep, text: text.slice(start).trim() });
    return parts;
  }

  /** "a + -$5" and "a - -$5" written out by string templates read as
   * "a - $5" and "a + $5": the same arithmetic, without the double sign. */
  function tidySigns(text) {
    return text.replace(/ \+ -(?=[$\d])/g, ' - ').replace(/ - -(?=[$\d])/g, ' + ');
  }

  function isName(text) {
    return (
      text.length > 0 &&
      text.length <= MAX_NAME_LENGTH &&
      !/[:;]/.test(text) &&
      text.split(/\s+/).length <= MAX_NAME_WORDS
    );
  }

  /** `Name = rest` (or `Name ≈ rest`, or `Name =` continued below), else null. */
  function parseDefinition(raw) {
    if (isIndented(raw)) return null;
    const line = raw.trim();
    if (/^[=≈]/.test(line)) return null;
    const open = /^(.+?) ([=≈])$/.exec(line);
    if (open) return isName(open[1]) ? { name: open[1], rel: open[2], segments: [] } : null;
    const parts = splitTopLevel(line, RELATIONS);
    if (parts.length < 2 || !isName(parts[0].text)) return null;
    const segments = parts.slice(1).map((p) => p.text);
    // "x = A, y = B, area = C" is a legend sentence, not a calculation: a
    // real chain of "=" ends in, or passes through, a figure.
    if (segments.length > 1 && !segments.some((s) => /\d/.test(s))) return null;
    return { name: parts[0].text, rel: parts[1].sep, segments };
  }

  /** A substitution line: "= ..." or "≈ ...". */
  function parseStepLine(raw) {
    if (isIndented(raw)) return null;
    const match = /^([=≈])\s?(.*)$/.exec(raw.trim());
    return match ? { rel: match[1], text: match[2] } : null;
  }

  function newCalc(name, rel) {
    return { type: 'calc', name, rel, formula: [], steps: [], notes: [], terms: null, result: null };
  }

  const calcHasResult = (calc) => calc.steps.length > 0 && !calc.steps[calc.steps.length - 1].name &&
    isValue(calc.steps[calc.steps.length - 1].text);

  /** Push the pieces of an "=" chain as steps, splitting "a = b" on one line. */
  function addSteps(calc, rel, text) {
    for (const part of splitTopLevel(text, RELATIONS)) {
      calc.steps.push({ rel: part.sep || rel, text: part.text, name: null });
    }
  }

  function addDefinitionSegments(calc, segments) {
    const [formula, ...rest] = segments;
    if (formula !== undefined) calc.formula.push(formula);
    for (const segment of rest) {
      // "status = ACTIVE = 27": a wordy middle piece still belongs to the formula.
      if (!/\d/.test(segment) && !calc.steps.length) {
        calc.formula[calc.formula.length - 1] += ` ${calc.rel} ${segment}`;
      } else {
        calc.steps.push({ rel: calc.rel, text: segment, name: null });
      }
    }
  }

  /** An inner definition inside a calculation that is still being worked
   * ("Floor = max($44.90, $43.23) = $44.90" under "Profit Target = ..."),
   * as opposed to the start of a new calculation. */
  function isInnerDefinition(calc, def) {
    if (!calc || calcHasResult(calc)) return false;
    if (def.name === calc.name) return true;
    const last = def.segments[def.segments.length - 1];
    return calc.formula.length > 0 && def.segments.length > 1 && last !== undefined && isValue(last);
  }

  /** A one-piece worded definition written as a full sentence. */
  const isQualifyingSentence = (def) =>
    def.segments.length === 1 && /\.$/.test(def.segments[0]) && !/\d/.test(def.segments[0]);

  /* Text runs: descriptions and notes. */

  /** Join hard-wrapped lines back into sentences: a line that starts in
   * lowercase, or an indented line after one that didn't end a sentence. */
  function reflow(rawLines) {
    const lines = [];
    for (const raw of rawLines) {
      const text = raw.trim();
      if (!text) continue;
      const prev = lines[lines.length - 1];
      const joins =
        prev !== undefined &&
        !isBullet(text) &&
        !isBullet(prev) &&
        (startsLowercase(text) || ((isIndented(raw) || text.startsWith('(')) && !endsSentence(prev)));
      if (joins) lines[lines.length - 1] = `${prev} ${text}`;
      else lines.push(text);
    }
    return lines;
  }

  function parseRowLine(text) {
    const match = /^([^:]{1,40}): (.+)$/.exec(text);
    if (!match || match[1].length > MAX_ROW_LABEL_LENGTH || !isValue(match[2])) return null;
    return { label: match[1], value: match[2] };
  }

  /** A "Heading:" list of indented "expression = $value" lines. */
  function parseCalcRows(heading, rawItems) {
    const rows = [];
    for (const raw of rawItems) {
      const parts = splitTopLevel(raw.trim(), RELATIONS);
      const value = parts[parts.length - 1].text;
      if (parts.length < 2 || !isValue(value)) return null;
      rows.push({ label: tidySigns(parts.slice(0, -1).map((p) => p.text).join(' = ')), value, sub: null });
    }
    return { type: 'rows', heading: heading.replace(/:$/, ''), rows };
  }

  /** Blocks for a run of non-calculation lines, in `tone` 'text' (primary)
   * or 'note' (secondary qualifiers). */
  function textBlocks(rawLines, tone) {
    const blocks = [];
    let pending = [];
    const flush = () => {
      for (const text of reflow(pending)) {
        const row = parseRowLine(text);
        const last = blocks[blocks.length - 1];
        if (row && last && last.type === 'rows' && !last.heading) last.rows.push({ ...row, sub: null });
        else if (row) blocks.push({ type: 'rows', heading: null, rows: [{ ...row, sub: null }] });
        else blocks.push({ type: tone, text });
      }
      pending = [];
    };
    for (let i = 0; i < rawLines.length; i += 1) {
      const raw = rawLines[i];
      const text = raw.trim();
      if (!text) continue;
      if (isBullet(text)) {
        const heading = pending.length && endsSentence(pending[pending.length - 1].trim())
          ? pending.pop().trim()
          : null;
        flush();
        const items = [];
        while (i < rawLines.length && isBullet(rawLines[i].trim())) {
          items.push(rawLines[i].trim().slice(2));
          i += 1;
        }
        i -= 1;
        blocks.push({ type: 'list', heading, items, tone });
        continue;
      }
      if (/:$/.test(text) && !isIndented(raw) && i + 1 < rawLines.length && isIndented(rawLines[i + 1])) {
        const items = [];
        let j = i + 1;
        while (j < rawLines.length && isIndented(rawLines[j]) && rawLines[j].trim()) {
          items.push(rawLines[j]);
          j += 1;
        }
        const list = parseCalcRows(text, items);
        if (list) {
          flush();
          blocks.push(list);
          i = j - 1;
          continue;
        }
      }
      pending.push(raw);
    }
    flush();
    return blocks;
  }

  /* Calculations. */

  /** Capitalize a formula term used as a row label ("cost basis" -> "Cost basis"). */
  const asLabel = (text) => text.charAt(0).toUpperCase() + text.slice(1);

  /** Pair a plain sum's named terms with its substituted values, so
   * "A + B - C" over "= $1 + $2 - $3" lays out as labeled rows. Only when the
   * shapes match exactly: same count, same operators, every value a figure. */
  function pairTerms(expression, substitution) {
    const names = splitTopLevel(expression, SUM_OPERATORS);
    const values = splitTopLevel(substitution, SUM_OPERATORS);
    if (names.length < 2 || names.length !== values.length) return null;
    if (names.some((n, i) => n.sep !== values[i].sep || !n.text)) return null;
    if (!values.every((v) => isValue(v.text))) return null;
    return names.map((n, i) => {
      const sub = /^(.*\S)\s*\(([^()]*)\)$/.exec(n.text);
      return {
        label: asLabel(sub ? sub[1] : n.text),
        sub: sub ? sub[2] : null,
        less: n.sep === '-',
        value: values[i].text,
      };
    });
  }

  function finishCalc(calc) {
    if (calcHasResult(calc)) calc.result = calc.steps.pop().text;
    if (calc.result !== null && calc.steps.length === 1 && !calc.steps[0].name) {
      const substitution = calc.steps[0].text;
      const terms = pairTerms(calc.formula.join(' '), substitution);
      if (terms) {
        calc.terms = terms;
        calc.formulaIsTerms = true;
        calc.steps = [];
      } else {
        // The term list may sit on its own indented line under a worded
        // formula ("Initial cap = day one's total ..." then "A + B + C").
        const index = calc.notes.findIndex((raw) => pairTerms(raw.trim(), substitution));
        if (index >= 0) {
          calc.terms = pairTerms(calc.notes[index].trim(), substitution);
          calc.notes.splice(index, 1);
          calc.steps = [];
        }
      }
    }
    for (const step of calc.steps) step.text = tidySigns(step.text);
    calc.notes = textBlocks(calc.notes, 'note');
    return calc;
  }

  /** Lines of one blank-separated section -> its blocks. */
  function parseSection(rawLines, tone) {
    const blocks = [];
    let lead = [];
    let calc = null;
    const flushLead = () => {
      if (lead.length) blocks.push(...textBlocks(lead, tone));
      lead = [];
    };
    const close = () => {
      if (calc) blocks.push(finishCalc(calc));
      calc = null;
    };
    for (const raw of rawLines) {
      const step = parseStepLine(raw);
      if (step) {
        if (!calc) {
          flushLead();
          calc = newCalc(null, step.rel);
        }
        addSteps(calc, step.rel, step.text);
        continue;
      }
      const def = parseDefinition(raw);
      if (def && isInnerDefinition(calc, def)) {
        if (def.name === calc.name) addSteps(calc, def.rel, def.segments.join(` ${def.rel} `));
        else calc.steps.push({ rel: def.rel, name: def.name, text: tidySigns(def.segments.join(` ${def.rel} `)) });
        continue;
      }
      if (def && calc && isQualifyingSentence(def)) {
        // "Income = all cash collected, incl. ..." under a calculation
        // qualifies it; it isn't a second calculation.
        calc.notes.push(raw);
        continue;
      }
      if (def) {
        close();
        flushLead();
        calc = newCalc(def.name, def.rel);
        addDefinitionSegments(calc, def.segments);
        continue;
      }
      const text = raw.trim();
      if (!calc) {
        lead.push(raw);
      } else if (isIndented(raw) && !calc.steps.length && (
        !calc.formula.length ||
        leavesFormulaOpen(calc.formula[calc.formula.length - 1]) ||
        continuesFormula(text)
      )) {
        calc.formula.push(text);
      } else if (isIndented(raw) && !calc.steps.length && startsLowercase(text) && calc.formula.length) {
        // A worded formula wrapped mid-sentence.
        calc.formula[calc.formula.length - 1] += ` ${text}`;
      } else {
        calc.notes.push(raw);
      }
    }
    close();
    flushLead();
    return blocks;
  }

  /** The text's blank-separated sections, as arrays of raw lines. A section
   * that opens with "=" continues the one before it (some tips put a blank
   * line between a formula and its substitution) unless that one already
   * ended on a result. */
  function sectionsOf(text) {
    const groups = [];
    let current = [];
    for (const raw of String(text || '').replace(/\r/g, '').split('\n')) {
      if (raw.trim()) current.push(raw);
      else if (current.length) {
        groups.push(current);
        current = [];
      }
    }
    if (current.length) groups.push(current);
    const merged = [];
    for (const group of groups) {
      const previous = merged[merged.length - 1];
      const lastStep = previous && parseStepLine(previous[previous.length - 1]);
      const endedOnResult = lastStep && isValue(splitTopLevel(lastStep.text, RELATIONS).pop().text);
      if (previous && parseStepLine(group[0]) && !endedOnResult) previous.push(...group);
      else merged.push(group);
    }
    return merged;
  }

  /** Text tip -> { title, sections: [[block, ...], ...] }. The first section
   * is the description; text-only sections after it are notes. */
  function parse(text) {
    const sections = sectionsOf(text)
      .map((lines, index) => parseSection(lines, index === 0 ? 'text' : 'note'))
      .filter((blocks) => blocks.length);
    return { title: null, sections };
  }

  /* Rendering. */

  function node(tag, className, text) {
    const n = document.createElement(tag);
    if (className) n.className = className;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }

  function renderRows(block) {
    const wrap = node('div', 'tip-rows-block');
    if (block.heading) wrap.appendChild(node('div', 'tip-heading', block.heading));
    const grid = node('div', 'tip-grid');
    for (const row of block.rows) {
      const label = node('span', 'tip-label', row.label);
      if (row.sub) label.appendChild(node('span', 'tip-sub', row.sub));
      grid.appendChild(label);
      grid.appendChild(node('span', 'tip-value', row.value));
    }
    wrap.appendChild(grid);
    return wrap;
  }

  function renderList(block) {
    const wrap = node('div', block.tone === 'note' ? 'tip-list-block tip-note' : 'tip-list-block');
    if (block.heading) wrap.appendChild(node('div', 'tip-text', block.heading));
    const list = node('ul', 'tip-list');
    for (const item of block.items) list.appendChild(node('li', null, item));
    wrap.appendChild(list);
    return wrap;
  }

  function renderTable(block) {
    const wrap = node('div', 'tip-table-block');
    if (block.heading) wrap.appendChild(node('div', 'tip-heading', block.heading));
    const table = node('table', 'tip-table');
    if (block.columns.some((c) => c.label)) {
      const head = node('tr');
      for (const column of block.columns) head.appendChild(node('th', column.num ? 'num' : null, column.label));
      const thead = node('thead');
      thead.appendChild(head);
      table.appendChild(thead);
    }
    const body = node('tbody');
    for (const cells of block.rows) {
      const tr = node('tr');
      cells.forEach((cell, i) => {
        const td = node('td', block.columns[i] && block.columns[i].num ? 'num' : null);
        if (cell && typeof cell === 'object' && 'meter' in cell) {
          // The figure beside it carries the score; the bar only pictures it.
          const meter = node('span', 'tip-meter');
          meter.setAttribute('aria-hidden', 'true');
          const fill = node('span');
          fill.style.width = `${Math.max(0, Math.min(1, cell.meter)) * 100}%`;
          meter.appendChild(fill);
          td.appendChild(meter);
          if (cell.text) td.appendChild(node('span', 'tip-meter-text', cell.text));
        } else {
          td.textContent = cell === null || cell === undefined ? '' : String(cell);
        }
        tr.appendChild(td);
      });
      body.appendChild(tr);
    }
    table.appendChild(body);
    wrap.appendChild(table);
    return wrap;
  }

  function renderStep(step) {
    const line = node('div', 'tip-step');
    if (step.name) {
      line.appendChild(node('span', 'tip-step-name', step.name));
      line.appendChild(document.createTextNode(' '));
    }
    line.appendChild(node('span', 'tip-rel', step.rel));
    line.appendChild(document.createTextNode(` ${step.text}`));
    return line;
  }

  function renderCalc(calc) {
    const wrap = node('div', 'tip-calc');
    const compact = calc.result !== null && !calc.steps.length && !calc.terms && calc.formula.length <= 1;
    if (compact && calc.name) {
      // A one-line definition with its figure: one labeled row.
      const grid = node('div', 'tip-grid');
      const label = node('span', 'tip-label tip-calc-name', calc.name);
      if (calc.formula.length) label.appendChild(node('span', 'tip-sub', calc.formula[0]));
      grid.appendChild(label);
      grid.appendChild(node('span', 'tip-value tip-strong', calc.result));
      wrap.appendChild(grid);
    } else {
      if (calc.name) wrap.appendChild(node('div', 'tip-calc-name', calc.name));
      if (!calc.formulaIsTerms) {
        calc.formula.forEach((line, i) => {
          wrap.appendChild(node('div', i ? 'tip-formula tip-formula-cont' : 'tip-formula', line));
        });
      }
      for (const step of calc.steps) wrap.appendChild(renderStep(step));
      if (calc.terms) {
        const grid = node('div', 'tip-grid tip-terms');
        for (const term of calc.terms) {
          const label = node('span', 'tip-label');
          if (term.less) label.appendChild(node('span', 'tip-less', 'less '));
          label.appendChild(document.createTextNode(term.label));
          if (term.sub) label.appendChild(node('span', 'tip-qualifier', ` (${term.sub})`));
          grid.appendChild(label);
          grid.appendChild(node('span', 'tip-value', term.value));
        }
        grid.appendChild(node('span', 'tip-rule'));
        grid.appendChild(node('span', 'tip-label tip-total', calc.name || 'Total'));
        grid.appendChild(node('span', 'tip-value tip-total tip-strong', calc.result));
        wrap.appendChild(grid);
      } else if (calc.result !== null) {
        const row = node('div', 'tip-result');
        row.appendChild(node('span', 'tip-label', calc.name || 'Result'));
        row.appendChild(node('span', 'tip-value tip-strong', calc.result));
        wrap.appendChild(row);
      }
    }
    for (const note of calc.notes) wrap.appendChild(renderBlock(note));
    return wrap;
  }

  function renderBlock(block) {
    switch (block.type) {
      case 'calc':
        return renderCalc(block);
      case 'rows':
        return renderRows(block);
      case 'list':
        return renderList(block);
      case 'table':
        return renderTable(block);
      case 'note':
        return node('p', 'tip-note', block.text);
      default:
        return node('p', 'tip-text', block.text);
    }
  }

  /** Model -> DOM, the `.tip` container every text tooltip uses. */
  function renderSpec(spec) {
    const root = node('div', 'tip');
    if (spec.title) root.appendChild(node('div', 'tip-title', spec.title));
    for (const section of spec.sections) {
      const box = node('div', 'tip-section');
      for (const block of section) box.appendChild(renderBlock(block));
      root.appendChild(box);
    }
    return root;
  }

  global.TipLayout = {
    parse,
    renderSpec,
    render: (text) => renderSpec(parse(text)),
  };
})(window);
