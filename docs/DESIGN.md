# Design notes

Why the engine works the way it does. For day-to-day usage see [../README.md](../README.md).

## Two exports, two shapes

The design is validated against real broker exports that differ in almost every way
that matters, which is what keeps it from being tuned to one of them:

| | Export A | Export B |
|---|---|---|
| Rows | 195 | 624 |
| Span | 2.5 months | 12 months |
| Column order | **transposed** | as labelled |
| Row order | oldest first | **newest first** |
| Equity rows | none | 145 |
| Assignment share legs | must be synthesized | 33 of 34 supplied by the broker |
| As-of date format | `Sep-17-2025` | `Sep-17-2025` and `02-20-26` |
| Corporate actions | none | a ticker rename mid-contract |

Later year-split exports added a third date format, ISO `as of 2025-09-17`, plus
non-trade ledger rows (dividends, collateral marks).

Nothing in the parser or engine is switched on filename or shape; every difference
above is detected from the data.

## What the exports actually contain

Three properties of export A shape the whole design:

**The `Quantity` and `Price ($)` columns are transposed.** `Quantity` holds the
per-share premium and `Price ($)` holds the signed contract count. The parser does
not hard-code this — `_detect_swapped_columns` scores both readings and picks the
winner, so a corrected export still parses. Note that the obvious test does *not*
work: `Amount = −contracts × price × 100 − fees` is symmetric in the two fields, so
it validates magnitude but can never reveal orientation. The signals that do
discriminate are that contract counts are whole numbers, quoted prices are strictly
positive, and a sale pairs a negative quantity with positive cash. On this file the
swapped reading wins 492 to 127.

**There are no equity rows.** All 195 rows are options. When a put is assigned,
Fidelity books the share purchase somewhere this export does not show, and the
`Cash Balance ($)` column is no help either — it has 27 discontinuities from wires
and T+1 settlement, plus six literal `"Processing"` strings. So share legs are
**synthesized at the strike** and flagged `synthetic` everywhere they surface.

The newer export *does* carry those share legs, which makes synthesis a fallback
rather than the rule — see [Assignment share legs](#assignment-share-legs).

**Assignments and expirations are dated twice.** They post on the next business day
but carry the real date inline as `as of Nov-20-2025`. `Transaction.event_date`
prefers the as-of date, which is what puts them in the right cycle.

## Domain model

### Cycles

A **cycle** is one campaign in one underlying: it opens on the first position taken
and closes only when that ticker is completely flat — no open contracts, no shares.
Rolls, scaled entries, assignment and the covered calls that follow all land inside
one cycle without contract-to-contract chaining, which matters because real rolls do
not preserve size (this book closes 4 contracts and opens 2 on 2025-10-20).

Status is `ACTIVE` while anything is open, otherwise `ASSIGNED` if the campaign went
through an assignment, otherwise `CLOSED`.

### Intra-day ordering

Events within one ticker-day run in three phases, because a single ordering cannot
serve every case:

1. closes of positions carried in from a previous day — so a covered call opened
   later that day sees the shares an assignment just delivered;
2. opens;
3. closes of symbols that were *also opened today* — a 0-DTE round trip must come
   after its own open, or it finds no lot to match.

A single "closes first" rule loses same-day round trips; "file order" loses the
assignment→covered-call link. Both cases are in this data. Getting this right took
unmatched closing rows from 19 to 0.

### Rolls

A roll is a same-day close-and-reopen on the same underlying and option right, where
the new expiry is no earlier than the one closed. Quantities are deliberately *not*
required to match. Re-entering the identical contract just closed is a round trip,
not a roll. Direction is reported as `OUT`, `UP`, `DOWN` or `OUT_AND_UP`/`OUT_AND_DOWN`.

### Assignment share legs

An export that includes equity rows marks the settling fill as
`YOU BOUGHT ASSIGNED PUTS AS OF 02-20-26 …`. Two traps here, both of which cost
real money if missed:

1. That string contains the word `ASSIGNED`, so a naive action classifier matches
   it as the *option* assignment event. Since the row carries no OCC symbol it is
   then dropped entirely, losing the shares **and** their cash. The rules for
   `YOU (BOUGHT|SOLD) (ASSIGNED|EXERCISED) (PUTS|CALLS)` therefore sit above the
   bare `ASSIGNED` rule.
2. Once the row *is* recognized, synthesizing a share leg for the same assignment
   books the shares twice. So `_settle_assignment` first looks for the broker's own
   fill — matched on ticker, direction and share count within a few days, since the
   two rows post together but the option leg is back-dated to its as-of date — and
   only synthesizes when there is genuinely nothing to use.

Each settlement row can be claimed once. That consumption is tracked in engine-local
state rather than on the `Transaction` objects, which are frozen and shared between
runs; marking them would make a second `build()` silently disagree with the first.

These two files also date their as-of phrase differently — `as of Nov-20-2025` on
option events, `AS OF 02-20-26` on equity legs — so both forms are parsed.

### Corporate actions

A corporate action can rename the underlying mid-contract: the newer export sells
`AXL260220P8` and is assigned `DCH260220P8` when American Axle becomes Dauch
Corporation. The option series — expiry, right, strike — is untouched, so when a
close finds no lot under its own symbol the engine looks for open lots matching all
three under exactly one other ticker. One match is treated as a rename and reported;
anything ambiguous is left unmatched rather than guessed at.

### Capital

Committed capital is a daily timeline, not a snapshot, because a wheel's capital
changes every time a put rolls to a different strike:

- cash-secured put → `strike × 100 × contracts`
- assigned shares → cost basis
- covered call → **nothing**; the capital is already in the shares
- short call with no tracked shares → `strike × 100`, as a labelled proxy

That last case covers eight tickers here: calls written against stock bought before
this export starts. Without the proxy those tickers report an undefined return on
zero capital. They are marked `~` in the UI and `capital_estimated` in the payload.

A day on which nothing was live is emitted explicitly at zero rather than left out
of the series. Anything plotting it draws a straight line between consecutive
points, so an absent stretch becomes a ramp asserting capital that was never
committed. The unfiltered portfolio has no holes — with 40-odd tickers something is
always live — but filtering to one ticker did: 159 fabricated days on TGT, 284 on
QUAD, 1,886 across all of them. Only the interior is filled; extending to `through`
would rewrite `capital_deployed_now` for a book that has closed. The fix is
provably display-only, since the time-weighted average already skipped zero days
and a peak is a maximum.

### Charting committed capital

Three bands, stacked largest-and-steadiest first: shares held, put collateral, then
short calls with no tracked shares. Color is bound to the series, never to stack
position, so reordering or filtering never repaints a survivor.

**Long-option debit is counted but not banded.** It peaks at 0.4% of committed
capital — about one pixel — so a legend swatch for it would point at nothing
findable. It stays in the tooltip, the table and a legend note. Dropping it also
leaves the stack on palette slots 1–3, the only subset validated all-pairs in both
modes; the slot-4 yellow it gave up sat next to slot-2 orange, the documented weak
pair.

**That makes the total line load-bearing, not decoration.** The gap between the top
band and the line is exactly the excluded debit, and on 26 days across two tickers
the capital committed is *entirely* long debit — every band is zero while the total
is not. Without the line those days read as "nothing deployed", which is false.

Two rendering details that are easy to get wrong, and were:

- **A stroke around a band is an outline, not a separator.** Stroking the closed
  polygon traces the baseline and both side edges too. The 2px surface gap belongs
  only on the *shared* boundary between touching bands.
- **Caps and separators must be clipped to the runs where the band has height.**
  Drawn full width, every band's cap lands on the same line wherever the bands above
  it are empty, and the last one painted wins — so the top edge reports the wrong
  series. Short calls are non-zero on only 76 of 568 days, so this was most of the
  chart claiming a green top edge over a period with no short calls at all.

Because one band routinely holds 80–95% of the total, a **share-of-total mode**
re-expresses the same stack against a fixed 0–100 axis. The denominator stays the
true total rather than being renormalized over the three drawn bands: renormalizing
would delete the long debit and silently inflate the rest, and would make the
tooltip's dollars disagree with the chart's percentages.

### Returns

ROI is quoted against three denominators — initial, peak, and time-weighted average
capital — because no single one is honest for a position that resizes. Annualized ROC
always uses the time-weighted average, the only denominator that credits a position
for releasing capital early. Idle days at zero committed capital are excluded so a
gap between legs does not dilute the result.

The portfolio-level annualized figure is computed against the portfolio's own
time-weighted average capital rather than by averaging per-cycle percentages, which
would weight a one-day $1,400 trade the same as a two-month $60,000 one.

Every ROI and annualized ROC number is quoted twice, at the cycle, ticker, and
portfolio level: once against `option_realized_pl` (premium only -- credits
received minus debits paid to close, never touched by the underlying's price)
and once against `net_realized_pl` (premium plus realized stock P/L -- the full
wheel result). A stock assignment or call-away can swing the full-wheel figure
well away from the premium-only one; conflating the two would credit premium
income for a move that was really the stock's, or vice versa.

## Verification

Every option cash figure is the broker's own `Amount ($)`, already net of commission
and fees, allocated pro-rata when one fill closes several lots. The engine never
re-derives cash from price × quantity, so it cannot drift.

One wrinkle the second export forced: option premiums are quoted exactly, but
equity rows print a **rounded average fill price** beside an exact total. A
6,000-share fill at a true 3.1265 displays as 3.13 and misses by $21. Equity rows
therefore get half a cent per share of tolerance; options keep the flat cent. Eight
rows that looked like failures were all this, and a genuine error is off by orders
of magnitude more.

Non-trade rows are excluded from the check entirely: a dividend or a collateral
mark puts unrelated figures in the quantity and price cells, so there is no trade
to verify and no option leg for its cash to belong to.

Against the exports, individually and combined:

| Check | Export A | Export B | all combined |
|---|---|---|---|
| Rows parsed | 195 / 195 | 624 / 624 | 2115 → 1296 kept |
| Priced rows reconciling to `Amount ($)` | 164 / 164 | 542 / 542 | 1323 / 1323 |
| Model cash vs file cash | delta **0.000000** | delta **0.000000** | delta **0.000000** |
| Closing rows with no matching lot | 0 | 0 | 0 |
| Positions surviving their own expiry | 0 | 0 | 0 |
| Per-ticker option cash vs CSV | 19 / 19 exact | — | — |

The combined column is the strongest result: 819 duplicate rows merged away, and
closes that one file alone could not match all resolve once an earlier file
covering the same position is loaded alongside it.

`tests/test_integration.py` re-derives these from the CSV with independent standard
library arithmetic and asserts the engine agrees, so a regression fails the suite.
Its `TestEveryExport` class runs the shared invariants over every export present,
so a fix tuned to one file that breaks the other cannot pass.

One honest gap: when a call is assigned against shares bought before this export
began, the cost basis is unknowable. Those shares get a `PRE_HISTORY` lot with
`basis_known=False`, their stock P/L is reported as zero rather than invented, and
the cycle carries a warning.

## Combining exports

Fidelity limits how much history one download covers, so a full picture needs
several files — and they overlap. Merging them is the difference between a leg that
resolves and one that appears to close out of nowhere.

**The count kept is the maximum, never the sum.** A trade appearing in two
overlapping exports is one trade; but the *same* fill appearing twice within one
file is two real fills (selling one contract twice at the same price on the same
day). Set-based de-duplication cannot tell these apart — one export in testing
contains five such repeated fills. Taking `max(count per file)` handles both, makes
loading a file twice a no-op, and makes the result independent of tick order. All
copies of a trade are taken from a single chosen file so their relative order — which
the intra-day sequencing depends on — stays intact.

**The identity test has to tolerate the broker's own inconsistency.** Comparing rows
verbatim fails on two axes that vary between exports of the same account:

- spacing: `BRIGHTHOUSE FINL INC NOV 21 25` vs `BRIGHTHOUSE FINL INCNOV 21 25`;
- the as-of date format: `as of Sep-17-2025` vs `as of 2025-09-17`.

So the key strips all whitespace and replaces the whole as-of phrase — the parsed
`as_of_date` already carries that information as a real field.

**Row order has to be normalized first.** Some exports are oldest-first, others
newest-first. Since `row_id` breaks ties between events on the same day, a
newest-first file would process each day's fills backwards, and two exports of the
same account would disagree with each other. Descending files get their index
reversed at parse time, then merged rows are renumbered chronologically.

One assumption worth stating: identity is content-based, so two *different accounts*
making an identical trade on the same day would merge into one. The exports carry no
account column, so this cannot be detected — combine exports from one account unless
a portfolio-wide view is what you want.

## Filtering

Ticker and date filters rebuild the cycles from the filtered transaction slice rather
than post-filtering finished cycles, so every stat, chart and table agrees. A date
filter can cut a position in half, leaving a buy-back whose sell-to-open is outside
the window; that cash is tracked as `unmatched_cash` and reported separately instead
of appearing as a shortfall.

Status is applied *after* cycles are built, since it only hides finished cycles from
the view. Reconciliation is therefore measured before the status filter — it is a
claim about ingest integrity, not about what is currently on screen.

**Capital is the one exception to "rebuild from the filtered slice."** P/L, premium,
wins and rolls are legitimately about "what happened in this window," so they stay
scoped to the start+end+ticker+status filtered transactions above. But committed
capital is a *state*, not an event count: a position opened before the window still
has real money tied up once the window begins. Rebuilding it the same way as P/L
would make that position appear to spring from nothing the moment some unrelated
in-window trade happens to touch it. Against a real account: one ticker assigned
shares months before a one-year window, then untouched until a covered call deep
into it, showed exactly $0 capital for the first eleven weeks of that window instead
of the $4,490 truly held. Portfolio-wide, where 40-odd tickers dilute any single
position's effect, the same bug still understated the account's average committed
capital across a one-year window by 8%.

So capital is reconstructed twice: `cycles` (start+end+ticker+status filtered) drives
every P&L figure as usual, while a second `capital_cycles` set — filtered by ticker,
status and `end`, but **not** `start` — drives the committed-capital figures (current,
average, peak, and the ROC denominator) and the capital chart itself.
`portfolio_capital_series(cycles, through, since=filters.start)` reconstructs full
history from `capital_cycles` and only crops the result to `since` for display, so a
position's true state on day one is whatever it actually was, not zero. `since=None`
is a no-op, so every other caller of `portfolio_metrics`/`ticker_summary`/
`portfolio_capital_series` is unaffected. `end` stays a real cutoff here, not just a
display crop — "state as of a past date" should not see trades that hadn't happened
yet, which is the same reasoning `through` already applies everywhere else.

One consequence: `ticker_summary` iterates the *union* of tickers appearing in either
cycle set, not just the P&L one. A wheel that is fully dormant during the window has
no P&L cycle once `cycles` is date-filtered, but it can still be holding real capital
the whole time — dropping it from the per-ticker table would make it vanish from the
P&L and ROC breakdown exactly when there's nothing else to explain where its capital
went. Such a row reports zero P&L and its true capital honestly, rather than not
existing.

## Relationship to the previous script

`wheel_options_tracker.py` is the earlier pandas implementation, left untouched. Two
defects in its `tag_wheels()` are worth noting, since they motivated the rewrite:

- the `is_long_call` / `is_long_put` masks run before any Wheel ID has been assigned,
  so **every** option row is stamped `LONG CALL` / `LONG PUT` before the main loop
  overwrites it;
- `df_wheel_candidates` is computed and then never used.

Its `rename_headers` mapping, however, already documented the transposed columns, and
that observation carried straight into the new parser.
