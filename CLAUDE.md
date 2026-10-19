# Working Standards

Default standards for all my software work. Project-level CLAUDE.md files add project-specific rules on top of these.

## Role and priorities

Work like a principal software engineer maintaining a large, long-lived production system, not like someone trying to make the requested change work as fast as possible.

Priority order: correctness, architectural quality, maintainability, reliability, testability, performance (where it materially matters), security and robustness, clarity, minimal complexity.

Standard: **Correct → Precise → Robust → Maintainable → Testable → Understandable.**

## Before changing existing code

Treat the codebase as a real production system.

- Understand the architecture, dependencies, interfaces, data flow, and conventions before significant changes.
- Reuse existing abstractions; don't duplicate functionality that already exists.
- Preserve working behavior and backward compatibility unless I ask for a change.
- Look for side effects and impact on other parts of the system.
- If the fix belongs somewhere other than the file I pointed to, put it there and tell me why.
- If the existing architecture is poor, name the problem and recommend the fix. Don't make it worse because it's easier.
- Follow the project's established patterns (framework, naming, state management, testing, style). Only deviate for a real engineering reason, and say so.

## Code quality

Prefer: separation of concerns, high cohesion, low coupling, small focused functions, explicit interfaces, strong typing where appropriate, deterministic and testable components, consistent error handling, explicit validation, defensive handling of edge cases.

Avoid: clever code, premature abstraction, copy/paste, hidden or global state, magic numbers, silent failures, fragile assumptions, deep nesting, giant functions or classes, duplicated business logic, temporary hacks presented as permanent solutions.

Don't over-engineer. Use the simplest design that is correct, maintainable, and extensible.

**Naming:** names communicate intent (`current_collateral`, `realized_pnl`, `annualized_return`), never vague names like `value`, `data`, `amount`, `result`, `temp`.

**Comments:** explain *why*: business rules, mathematical definitions, assumptions, non-obvious decisions. Never restate obvious code. Don't over-comment.

```python
# Count the opening transaction only.
# Closing the same-day position must not create a second trading day.
count += 1
```

## Engineering judgment

- If my proposed approach is technically inferior, say so briefly, give the better approach, and implement it when appropriate. Don't agree just because I suggested it.
- When there are several valid approaches, compare them and recommend one.
- For significant changes, settle the architecture first: where responsibility lives, source of truth, inputs and outputs, invariants, what should stay independent, failure behavior, how it will read in six months.
- Don't solve architectural problems by adding more conditionals to an existing function.

## Never hide problems

Tell me about any bug, incorrect existing math, data-integrity issue, design flaw, race condition, performance or security problem, misleading UI, or architectural weakness you find, even if it's outside the task. Never silently work around a serious problem to make the task look done.

## Math and calculations

Precision is mandatory for every equation, metric, percentage, financial calculation, unit conversion, and algorithm.

- Verify formula, units, signs, assumptions, and boundary conditions.
- Check percentages are applied to the correct base.
- Check annualization or normalization is mathematically valid.
- Check nothing is double-counted; reconcile intermediate values.
- State assumptions explicitly. Never invent a formula because it looks reasonable.
- Code must match the exact mathematical definition.
- Sanity-check important results independently before presenting them.

## Data integrity

For financial, trading, analytics, metrics, and database work:

- Define every metric exactly and identify its source of truth.
- Don't mix fundamentally different quantities; preserve units and meaning.
- Reconcile derived values against source data.
- Handle missing, invalid, stale, and contradictory data explicitly.
- Never silently substitute an approximate value for an exact one.
- Make state transitions explicit.
- If two numbers look inconsistent, investigate. Don't assume one is right.

## Testing

Think about tests before implementing meaningful changes: unit, integration, regression, edge cases, invalid/empty/missing/duplicate data, boundary values, state transitions, failure modes.

Tests verify *intended* behavior, not whatever the implementation currently does. Point out the most important tests for preventing future regressions.

## Refactoring

- Preserve externally observable behavior unless asked otherwise.
- One logical improvement at a time; don't mix refactoring with feature work.
- Remove dead code when safe; consolidate duplicated business logic.
- Preserve or improve test coverage.
- Every refactor needs a reason beyond style preference.

## Requirements

- Don't assume requirements, or that something is intended because it's common in similar apps. Follow what I actually specify.
- If ambiguity materially affects the implementation, ask. If it's minor and the safe reading is obvious, proceed and state the assumption.
- Never silently change the meaning of a requirement.

## Complete changes

Deliver production-quality work, not just code that compiles. Check whether the change also needs: data-model changes, API changes, UI changes, validation, tests, docs, error handling, migrations, configuration, logging, backward compatibility.

Before delivering substantial code, review it against:

1. Meets the actual requirement
2. Appropriate architecture
3. Preserves required behavior
4. Calculations correct
5. Edge cases handled
6. Error handling appropriate
7. Testable
8. No unnecessary duplication
9. Understandable
10. No regressions elsewhere

## User-facing text

UI labels, tooltips, help text, dashboard descriptions, error messages, and docs should be understandable by a reasonably intelligent 21-year-old that has done basic options trading. Use plain English, keep it accurate, and cut jargon. Explain what something means and why the user should care, not how the code works.

- Good: "Money currently tied up in open option positions."
- Bad: "Aggregate deployed capital exposure associated with currently active option collateralization."

## Response style

- Answer first. Be direct and concise.
- Don't restate my request, add introductions, generic encouragement, or unnecessary disclaimers, or repeat conclusions.
- Use simple language unless technical detail is needed.
- Use tables for comparisons when they're clearer.

## Continuity

When I establish a business rule, metric definition, architectural decision, or convention, record it in the project's CLAUDE.md (ask first) so future sessions stay consistent. Apply it until I change it.

## Project conventions

**Options Greeks (delta, gamma, theta, vega) are model-derived, never a broker feed.** Yahoo's free quote endpoints carry no Greeks and no risk-free rate. `wheel/optionsmath.py` computes them with the standard Black-Scholes formulas (European exercise, no dividend yield) from: spot price (existing live-quote pipeline), strike/expiry (the transaction ledger), and implied volatility (a new live fetch of Yahoo's per-expiration option chain, `wheel/marketdata.py`'s `fetch_yahoo_option_chain`/`parse_yahoo_option_chain`). The risk-free rate is a fixed assumption, `DIRECTIONAL_RISK_FREE_RATE_ASSUMPTION = 0.045` in `wheel/api.py`, not fetched from anywhere. When a quote or its implied volatility is unavailable, every Greek for that leg is `None` with a `greeks_unavailable_reason` string - never a stale, zero, or invented value. Introduced for the directional (LC/LP) Trade Log card; reuse this module/approach rather than a second Greeks implementation if another feature needs them.

**CC TO EXIT has one implementation: `wheel/ccexit.py` `calculate_cc_to_exit`.** It runs once per wheel in `_trade_log_entry`; every table, card and tooltip copies the wheel's `cc_to_exit`, `cc_to_exit_status`, `cc_to_exit_label`, `cc_to_exit_card` and `cc_to_exit_detail` fields (`_cc_exit_fields` in `wheel/api.py`) and the frontend renders them with `ccToExitCell`. Never compute it a second way. Rules:
- Covered shares = 100 x open short covered-call contracts (expired, bought-back and assigned contracts don't count). Uncovered = shares held - covered. None uncovered: ALL SHARES COVERED. No shares: a dash. Fewer than 100 uncovered, no chain, no price, or no strike passing: NO QUALIFYING CC with the reason. Never fall back to the stock price.
- Covered/uncovered follows the engine's FIFO lot order (also Fidelity's default for stocks): open calls cover the oldest shares, pre-history shares count as oldest, so the next call covers the newest. Uses the cost of those shares only, never the blended cost of all shares and never the whole-position Wheel Breakeven.
- Preferred minimum exit = that cost x 1.02, rounded up to $0.50; strikes below it are rejected.
- Expiration: the listed date closest to 35 DTE within 30-45 DTE (closest to 35 overall if none is in the window).
- Premium = bid/ask mid less the account's own average fee per call contract sold (`Dashboard._cc_exit_fee_model`; Fidelity's $0.65 only if the account has never sold a call). Delta = Black-Scholes from the chain's implied volatility (see the Greeks rule above).
- Sale fee if assigned = strike x shares x the fee rate on the account's most recent stock sale (the SEC's per-dollar fee; `SALE_FEE_RATE_FALLBACK` = $20.60 per $1M only if the account has never sold stock).
- Hard constraint: P&L if assigned = (strike - cost) x shares + net premium - sale fee + option cash those same shares already earned (lot-level, below) must be > 0.
- Of the strikes left, the one with delta closest to 0.30 wins (lower strike on a tie); 0.25-0.35 is the preferred range, shown in the tooltip when the pick falls outside it.
- Every result carries the chain's quote time, its fetch time and one per-build calculation time.

**CSP TO ENTER has one implementation: `wheel/cspentry.py` `calculate_csp_to_enter`, one result per ticker.** `Dashboard._build_csp_to_enter` runs it once per price refresh and serves it as the payload's top-level `csp_to_enter` map (`{TICKER: ...}`, shaped by `_csp_to_enter_payload` in `wheel/api.py`). Rules:
- The frontend only displays it: every table, card and tooltip looks the ticker up with `cspToEnterFor` / `cspToEnterCell` in `app.js` and shows its `recommended_strike` (or `label`) and its tooltip `card` verbatim. No frontend support, delta, ROI or strike logic, no rounding, no substituting the stock price. Rows never carry their own copy; a view that merges accounts merges the map so one ticker can never show two values.
- Merging accounts picks whole entries, never mixes fields and never invents a strike. The Combined view (`accounts._combine_csp_to_enter`) keeps, per ticker, an entry whose `market_data_complete` is true over one that lacked a price, history, expirations or the chain, then the newest `calculated_at` (a full tie keeps the first). A single-account view always keeps its own entry and only adds other accounts' entries for tickers it has none for, because its own Gap to Target was computed against it.
- Expiration is CSP TO ENTER's own pick, `cspentry.pick_expiration` (`CSP_TARGET_DTE` 35 inside `CSP_DTE_WINDOW` 30-45, closest to 35 overall if none is listed there, flagged `expiry_in_window`), built on the shared `optionsmath.pick_expiration` that CC TO EXIT's `ccexit.pick_expiration` also uses. Never read CC TO EXIT's chosen date.
- The per-contract fee is the fixed $0.65 (`ccexit.OPTION_FEE_PER_CONTRACT`), not an account's measured fee, so the result stays one value per ticker. Don't change this without the user's explicit approval.
- The 30% minimum annualized return on strike collateral is one constant, `cspentry.MIN_ANNUALIZED_RETURN_PCT`, used wherever the wheel's minimum applies (the roll target `_ROLL_TARGET_ANNUALIZED_YIELD` reads it). Annualized return uses the open-position Ann. Yield definition: net premium / (strike x 100) x 365 / DTE.
- When no put qualifies (no relevant support, no chain, no price, nothing passing the hard filters) the result is NO QUALIFYING CSP with a reason. Never fall back to a fixed percentage of the price (the old 93% formula) or invent a strike.
- Support strength is a relative 0-100 score, never presented as a probability. The scoring weights and tunable thresholds are documented in `docs/DESIGN.md` (CSP TO ENTER) and live as named constants in `wheel/cspentry.py`; don't duplicate them here.

**Lot-level option cash (`wheel/metrics.py` `lot_option_credit`) is only what each lot's own shares earned.** A put roll chain that ended in assignment goes to the lot(s) it created, per share assigned, matched on the assignment day and that put's strike, so two puts assigned the same day each keep their own premium. A short call's net cash is split evenly over every share held when it opened, pre-history shares included; a call sold on the day of a put assignment does not count toward the newly assigned shares (they arrive after the close). Puts that expired or were bought back, and long hedges, stay wheel-level and go to no lot. This drives each lot's `net_adjusted_cost_basis` (the Breakeven column), the per-lot input to Profit Target, and CC TO EXIT's earlier-premium term. Whole-position figures (Wheel Breakeven `break_even_price`, wheel P&L) still count every dollar and are never inputs to CC TO EXIT.

**CSP candidate stars are absolute ratings.** `csp_star_score` in `wheel/api.py` gives each ticker `stars = round(raw_stars)` from its own past wheels, today's market inputs and its sector's weight in the book (this account's in a single-account view, the whole book's in Combined). The frontend shows that value as-is: never re-grade or stretch it across the shortlist (the old `spreadStars` curve is gone), so adding or removing one candidate can never change another's stars. Ordering within a star level uses `raw_stars` (`cspSignalSortKey`). The rating covers wheel history and option market conditions only; it is never presented as a chance of profit or a verdict on the company. Cross-account candidate rows are rebuilt from merged raw totals (`csp_candidate_history`, losing accounts included) by the same `csp_candidate_row` a single account uses.

**CSP TO ENTER and CC TO EXIT tooltips are one card component.** Each module builds its own structured card next to its calculation (`cspentry.tooltip_card`, `ccexit.tooltip_card`), in the shape `wheel/tooltipcard.py` defines, with every value already formatted; the frontend's one renderer `tipCardNode` (attached with `setTipCard`) and one CSS block (`.tip-card`) draw both. Don't add a plain-text tooltip, a second renderer or frontend math for either. Rules:
- Same order for both: header with a worded status badge (QUALIFIES / DOES NOT QUALIFY / UNVERIFIED when a market input was missing / ALL SHARES COVERED), the selected strike and delta, at most four key numbers, at most three "Why this strike?" reasons generated from the result, the "If assigned" risk block, then collapsible sections (strike table first), then the footer.
- Status never relies on colour alone; the strike table names each strike's result in words, with the reason on its own line.
- DTE in the header names the date it's counted from (the calculation date), and the annualized return uses that same DTE.
- Never call the distance to the strike protection or a maximum loss; the midpoint is always called an estimate.

**Tooltips open and close with the T key and never use the browser's native tooltip.** The browser cuts long `title` text off with "..." and hides it as soon as the mouse moves, so it is switched off everywhere: in `wheel/static/app.js`, a `MutationObserver` (`adoptTitles`) moves every `title` attribute and SVG `<title>` child into `data-tip` the moment it reaches the page, re-renders included. Code keeps setting `title` (or `setFormula`) as usual, and writing an empty `title` clears the tip. Rules:
- Hovering a value shows nothing. Pressing T (no Ctrl/Alt/Cmd, not while typing in a field) over it, or over the focused element, opens its tip in the page's one `#tooltip` (`pinTooltip`).
- Charts keep a hover readout that follows the mouse and hides when it leaves (`showTooltip`/`attachTip`/`crosshairLayer`). T over a chart freezes that readout instead.
- Only one tooltip can be open. While one is open (`tooltipPinned`), no hover or chart crosshair opens, moves or hides it. Pressing T again closes it (`dismissTooltip`). Esc does nothing to tooltips.
- An open tooltip has larger text (14px) than the chart readout (12px). Text is never cut off: it wraps, and a tooltip taller than the window scrolls inside itself. Never add `text-overflow: ellipsis`, a max line count or a fixed height that clips tooltip text.
