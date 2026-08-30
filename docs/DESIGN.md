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
and closes when that ticker is completely flat — no open contracts, no shares.
Rolls, scaled entries, assignment and the covered calls that follow all land inside
one cycle without contract-to-contract chaining, which matters because real rolls do
not preserve size (this book closes 4 contracts and opens 2 on 2025-10-20).

**A flat gap does not end the wheel; a completed rotation or the year turning does.**
Selling puts, closing them, and selling more later — with a flat stretch in between —
is one ongoing wheel, not a string of tiny ones. So when a ticker goes flat and is
then re-entered *with an option (STO/BTO)*, the engine reopens the most recent cycle
(`WheelEngine._resumable_cycle`) rather than starting a new one, **unless** either:

- that cycle already **completed a full rotation** — a covered call was assigned and
  its stock called away (a `DISPOSE` assignment). That is a finished wheel; the next
  entry is a new one. A cycle that only ever sold puts (assigned or not) has not
  completed and stays resumable.
- the re-entry falls in a **later calendar year**. The year is a deliberate cut point
  (it also resets the `-<n>` sequence), so a position carried across New Year's still
  starts a fresh `<ticker>-<year>-1`.
- the flat cycle **is not a wheel** (`Cycle.is_wheel` is `False` — a lone directional
  option punt, see below). A later put must not fold that unrelated trade's premium
  into a wheel's cost basis and metrics, so it opens a fresh cycle instead.

A bare stock purchase after a flat gap also starts its own cycle. A resumed cycle
keeps its original id and simply spans the flat days; committed capital reads $0
across them.

Status is `ACTIVE` while anything is open. A flat cycle is `NO_ACTIVITY` while it is
still resumable — it is an actual wheel whose `end_date` falls in the same calendar
year as the latest trade in the book and whose stock was not called away, so another
option on the ticker would reopen it. Otherwise it is `CLOSED` (terminal): the year
has turned since the last trade, a covered call was assigned and the stock disposed,
or the cycle was never a wheel to begin with (a directional option trade is done when
its option closes). The frontend renders `NO_ACTIVITY` as "NO ACTIVITY" and tints only
`CLOSED` wheels salmon.

### Wheel vs directional cycles

`Cycle.is_wheel` is `False` for a cycle that only ever *bought* options — a lone
directional call or put, an unpaired protective leg — and never sold a cash-secured
put or covered call, never held shares, never took assignment. Such a cycle ties up
nothing but its own premium and closes in days, so annualizing its result produces a
meaningless five-figure "ROC" (a $400 premium lost over two days ≈ −18,000%). Its
realized P&L is still real and still counts toward every P&L total — `net_realized_pl`,
`option_realized_pl`, the account rollups. Only the **wheel-framed ratios** are
withheld: `annualized_wheel_roc_pct`, `roi_on_avg_wheel_pct`, `net_option_yield_pct`,
`profit_per_day`, `win_rate_pct` all come back `None` from `cycle_metrics`, and
`portfolio_metrics` / `ticker_summary` compute those ratios from wheel cycles only
(the XIRR ledger in `wheel_cash_flow_events` / `wheel_terminal_value` skips them too).
The Trade Log tags the cycle "Directional (non-wheel)", the Dashboard cycle table adds
a `directional` badge, and the timeline marks the row with a `◇`.

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
- a short and a long leg paired into a `Spread` (see "Credit spreads" below) →
  `|short strike − long strike| × 100 × paired contracts`, in place of the short
  leg's own full CSP/covered-call figure for the paired portion

That last un-paired case covers eight tickers here: calls written against stock
bought before this export starts. Without the proxy those tickers report an
undefined return on zero capital. They are marked `~` in the UI and
`capital_estimated` in the payload.

### Credit spreads

A short and a long leg of the same underlying, right and expiry, opened on the
*same day*, pair into a `Spread` (`wheel/engine.py`, `WheelEngine._detect_spreads`)
-- the only pairing rule the engine applies, deliberately narrow: it mirrors the
existing same-day `_detect_rolls` grouping rather than trying to match legs across
different days, where the correct pairing is genuinely ambiguous (a later long
could be a new hedge, an adjustment, or unrelated). Quantities pair down to
whichever side is smaller; the excess on the larger side stays a naked leg, priced
by its own ordinary formula above.

**More than one short or long candidate in the same (underlying, right, expiry,
day) group pairs nothing.** One short put alongside two same-day long puts at
different strikes is real data, not a hypothetical (MU, 2025-11-17): pairing
against either one would be a guess, so none of that group pairs, all three legs
stay naked, and a cycle warning names the ambiguous group -- the same
"ambiguous → leave unmatched, warn" rule already used for ticker-rename matching
(`_lots_under_former_ticker`).

**A spread's netting only holds while both legs remain open.** If one side closes
early, the other reverts to its own naked collateral formula for whatever it has
left -- `paired_contracts` is fixed at pairing time, but each day's actual netted
amount is `min(paired_contracts, short leg's remaining, long leg's remaining)`,
computed fresh in `capital_timeline`.

Pairing never touches P/L: `Spread` is a costing overlay only, layered on top of
the untouched `OptionLeg.realized_pl`/`gross_premium`/`closes` machinery. A credit
spread's net P/L was already correct before this feature existed (both legs'
`realized_pl` simply summed into `option_realized_pl`); what changed is only how
much *collateral* the paired portion reports while open.

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

**Spread collateral gets the same treatment, for a different reason.** Unlike
long-option debit it is not always negligible, but giving it a fourth band would
reopen exactly the weak-color-pair problem the three existing bands were
deliberately validated against. It is counted in the total and shown in the
tooltip, the table and its own legend note, never banded.

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

`roi_pct` and `roi_on_peak_pct` are quoted against the initial and peak collateral
using `net_realized_pl` (premium plus realized stock P/L) -- they exist to show how
sensitive ROI is to denominator choice on a position that resizes, not to gauge the
wheel's own option income.

The headline metric, **Wheel ROC** (`roi_on_avg_wheel_pct`, annualized as
`annualized_wheel_roc_pct`), answers a narrower question: how much did the wheel's
*option activity* return on the capital it tied up, over time. Its numerator is
`option_realized_pl` only -- credits received minus debits paid to close -- and never
`net_realized_pl`: stock P/L, realized or not, gain or loss, does not enter it. A put
assigned at $100 that later trades at $80 does not make the wheel's ROC negative --
the stock is capital the wheel has tied up, not a loss the wheel's option leg took.
Conflating the two would credit (or blame) the option side for a move that was really
the stock's. `stock_realized_pl` and `net_realized_pl` remain available as their own
figures for anyone who wants the stock's own P/L or the full investment picture --
this tracker's ROC just doesn't fold them in.

`option_realized_pl` sums every leg in the cycle -- it always has, since nothing
here filters by `WHEEL_STRATEGIES` -- so protective puts and both legs of a
credit-spread hedge are already in it, each leg's own `realized_pl` already
netting its opening cash against every closing cash flow (a put bought for
$1,000 and sold for $700 contributes -$300, not -$1,000 and not +$700 booked
separately). `wheel_core_realized_pl` (CSP + covered-call legs) and
`hedge_realized_pl` (everything else -- `LONG_PUT`/`LONG_CALL`) are exposed as
the two addends purely so a caller can show that split; the model has no
concept of "this leg hedges that one", so every non-core leg counts as a hedge.
Capital for a long leg is the actual debit paid, decaying to $0 on close, never
notional -- and a short leg paired into a same-day `Spread` (see "Credit spreads"
above) reports the netted `|short strike − long strike| × 100` collateral for its
paired portion instead of the full CSP/covered-call figure. An unpaired short leg
-- no same-day long partner, or an ambiguous multi-candidate group -- is completely
unaffected and still gets full collateral, exactly as before this feature existed.

Both `roi_on_avg_wheel_pct` and the net-based ROI variants use the time-weighted
average collateral as their denominator when annualizing, the only one that credits
a position for releasing capital early; idle days at zero committed capital are
excluded so a gap between legs does not dilute the result. The portfolio-level
annualized figure is computed against the portfolio's own time-weighted average
capital rather than by averaging per-cycle percentages, which would weight a one-day
$1,400 trade the same as a two-month $60,000 one.

### Cost basis: tax basis vs. net adjusted cost basis

`ShareLot.basis_per_share` is the raw tax-lot basis -- the bare assignment or
purchase price, exactly what a 1099-B would show -- and nothing in this feature
touches it. `net_adjusted_cost_basis()` (`wheel/metrics.py`) is a second, separate
number: the wheel's own economic break-even, `strike − net option cash flow/share`,
accumulated over the *whole cycle's* option activity (every roll, every covered
call sold after assignment), not just the leg that produced the lot. When a cycle
holds more than one concurrent lot at different strikes, the cycle's net cash flow
is allocated pro-rata by share count, against every share the cycle's lots ever
held -- so the allocation stays stable as shares are later sold off, rather than
each lot claiming the whole cycle's premium independently.

The formula's `+ fees/share` term looks like it duplicates the fee that's already
netted into every cash figure here (`Transaction.amount` is fee-net -- see
"Verification" below) -- and it would, if "net premiums received" meant that same
fee-net figure. It doesn't: reconstructing gross premium (adding each leg's fees
back, the same trick `wheel/cashflow.py`'s `_split_option_row` already uses) and
then subtracting fees back out via the formula's own term is algebraically
identical to just using the already fee-net total directly. That identity is
exactly what the implementation does -- no separate fee term, because there's
nothing left for it to do once the gross reconstruction is skipped.

### Dual-track returns: Net Option Yield and Total Position ROI

Two more figures sit alongside the existing `roi_pct` / `roi_on_avg_wheel_pct` /
`annualized_wheel_roc_pct` trio, side by side rather than replacing them, quoted
against a different denominator: **initial** collateral (the position's day-one
capital), not the time-weighted average the headline Wheel ROC uses.

**Net Option Yield %** (`net_option_yield_pct`, annualized as
`annualized_net_option_yield_pct`) is `option_realized_pl ÷ initial_collateral` --
the same numerator as Wheel ROC, a different denominator. It answers "how much did
the option side return on the capital committed at entry," which a resizing
position (more contracts added mid-cycle, or capital freed early) can report
differently from the time-weighted-average-based Wheel ROC.

**Total Position ROI %** (`total_position_roi_pct`, annualized as
`annualized_total_position_roi_pct`) is the full investment picture:
`(option_realized_pl + stock_realized_pl + stock_unrealized_pl + dividends_received)
÷ initial_collateral`. Two things it deliberately does *not* include:

- **Long-option (open protective put / long call) unrealized P/L.** There is no
  options-quote feed anywhere in this stdlib-only project to mark an open hedge to
  market, so `long_leg_unrealized_pl` is always `None` -- a labelled placeholder,
  not a fake zero. Only a hedge leg's *realized* P/L (already inside
  `option_realized_pl` via `hedge_realized_pl`) counts until it actually closes.
- **Fees on top of the option/stock figures.** Every cash figure feeding this
  formula is already fee-net (the broker's own `Amount`), so nothing here
  re-subtracts fees a second time -- the same principle the module docstring
  states for `option_realized_pl` itself.

`stock_unrealized_pl` marks every share lot still held to the underlying's latest
close, fetched and cached the same way the SPY benchmark price always has been --
see "Market data cache" below, now generalized to any ticker. It is `None`, not
`0.0`, when no shares are held or no price is available, so it reads as "unknown"
rather than "no gain," matching this codebase's existing `None`-means-unknown
convention (`_safe_pct`, `win_rate_pct`).

`dividends_received` comes from `wheel/cashflow.py`'s existing dividend classifier
(`dividend_transactions`), attributed to whichever cycle was open on the
underlying's own ticker when the dividend posted -- `wheel.metrics.dividends_by_cycle`
matches each dividend's `event_date` against `[cycle.start_date, cycle.end_date or
still-open]`. On the rare day one cycle closes and a new one for the same ticker
opens, the earlier (closing) cycle claims it, since cycles are walked in
chronological order and the engine never actually produces two cycles that open
and close on the exact same day for the same ticker (a new cycle can't open until
the prior one goes flat, which this replay processes within the same day).

Portfolio- and ticker-level rollups follow the same "recompute from summed
absolutes, never average per-cycle percentages" principle as every other combined
figure in this codebase: `total_initial_collateral` is the sum of every cycle's own
`initial_collateral`, and both dual-track percentages are recomputed against that
sum, not averaged from each cycle's or account's own percentage.

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
verbatim fails on three axes that vary between exports of the same account:

- spacing: `BRIGHTHOUSE FINL INC NOV 21 25` vs `BRIGHTHOUSE FINL INCNOV 21 25`;
- the as-of date format: `as of Sep-17-2025` vs `as of 2025-09-17`;
- the cash columns: one PLTR buy-back downloaded twice shows `Fees 0.02 / Amount
  −261.32` in the newer file and `0.03 / −261.33` in the older — Fidelity re-rounds
  commission, fees and the net amount between downloads.

So the key strips all whitespace, replaces the whole as-of phrase (the parsed
`as_of_date` already carries that), and is built from the fields that actually define
a fill — date, symbol, action, direction, size, quoted price — **not** `commission`,
`fees` or `amount`. A genuinely repeated fill within one file still survives, because
the merged count is the max seen in any one file, not a set membership test.

**Row order has to be normalized first.** Some exports are oldest-first, others
newest-first. Since `row_id` breaks ties between events on the same day, a
newest-first file would process each day's fills backwards, and two exports of the
same account would disagree with each other. Descending files get their index
reversed at parse time, then merged rows are renumbered chronologically.

One assumption worth stating: identity is content-based, so two *different accounts*
making an identical trade on the same day would merge into one. The exports carry no
account column, so this cannot be detected — combine exports from one account unless
a portfolio-wide view is what you want.

## Position snapshots

A Positions export (`wheel/positions.py`) is a different shape from everything
above: one row per current holding at a single moment, not one row per historical
fill. It is what makes true net worth possible — the transaction-history engine
only ever sees option-wheel-related legs, so a buy-and-hold ETF an option has
never touched (IVV, QQQ, GLD) is invisible to it, and so is whole-account cash.

**The header alone tells the two formats apart.** Transaction history starts
`Run Date`; Positions starts `Account number`. Discovery sniffs on that first
column, the same way `looks_like_export` already does, so both kinds of file can
sit in the same folder without either misparsing the other.

**A short option's symbol carries a leading space before the dash** —
`" -CROX260821C150"` as one CSV field. `parse_occ_symbol` already strips the
dash; the parser strips the whitespace first, and reuses that function rather
than duplicating OCC-symbol parsing a second time.

**A row with no quantity and no price is cash**, regardless of what its Symbol
column says — the money-market sweep (`SPAXX**`) and the unlabeled
`Pending activity` line both take this path. Every other row is classified from
its symbol: a resolvable OCC symbol is an option, a bare symbol is equity, and
what remains is `UNKNOWN` — kept, not dropped, with one warning per occurrence,
matching the norm the parser already sets for anything ambiguous.

**`Type` is Fidelity's Cash/Margin/Financing activity tag, not a separate
brokerage account.** The same symbol legitimately appears on two rows with two
different `Type` values (a real Fidelity account splits a position across
sub-types), and both are kept as distinct rows rather than merged — merging
them would silently drop one lot's cost basis.

**The authoritative as-of moment is the footer, not the filename.** Every export
ends with a line like `"Date downloaded Aug-03-2026 5:45 p.m ET"`; that instant
is what every net-worth and benchmark figure is dated to. The filename is only a
fallback for a file whose footer is missing or unparseable, since it carries a
coarser, date-only granularity and is trivially user-editable.

## Account folders and combined aggregation

Transaction-history exports carry no account column — already a known limitation
for merging (see "Combining exports" above), and it means a true *per-account*
view was previously impossible even in principle. Since one Positions row genuinely
does carry `Account number`, the fix is structural rather than another parsing
heuristic: put each account's own files in their own folder under `data/`, and let
the folder be the account boundary. Loose files directly in `data/` (the layout
every export before this feature used) become one more, implicit account, so
nothing already on disk has to be reorganized.

`wheel/accounts.py`'s `AccountRegistry` builds one `Dashboard` per discovered
folder — each is exactly the existing single-account pipeline, unmodified — and
answers a `"combined"` query by aggregating the already-built payloads one level
up, never by pooling the accounts' transactions into one parse. That distinction
matters concretely: two accounts independently running a wheel on the same ticker
would merge into one cycle if their transactions were combined the way two
exports of the *same* account are (`merge_transactions` has no account field to
key on) — so combined `cycles` and `tickers` are the **concatenation** of every
account's own rows, each tagged with the account it came from, never re-merged.

**`cycle_id` is `"<ticker>-<start year>-<n>"`, and `n` restarts at 1 each calendar
year** (`WheelEngine._cycle_counter` is keyed on `(underlying, year)`) — the first MU
campaign of 2026 is `MU-2026-1` no matter how many MU campaigns ran in 2025, since the
year already separates them. It is also regenerated per account, so both accounts'
first 2026 MU cycle would be `MU-2026-1`; the dashboard uses `cycle_id` as a set key
for expand/collapse state, so the combined view prefixes it with the account id.
Without that, two unrelated cycles sharing a generated id would expand and collapse
together.

**Combined return figures are recomputed from the combined absolutes, never
averaged from each account's own percentage** — the same principle
`portfolio_metrics` already applies going from cycles to the portfolio (see
"Returns" below), one level further up. Concretely: a $15,000 six-day trade and a
$20,000 nine-day trade combine to an $18,000 time-weighted average capital
(`(15000×6 + 20000×9) / (6+9)`), not the $17,500 a naive average of the two
positions' own sizes would give — and the two accounts' own ROI percentages
average to a number further still from the combined figure, since averaging
percentages discards how large or how long each position actually was. Capital
series and P/L series are summed **day by day** across accounts' already-built
series for the same reason a lump average would misrepresent them.

**The reverse case — one Positions file naming more than one real account —
can't be resolved the same way, since there's nothing to group by two folders
reporting the same number.** Fidelity's "all accounts" download lists every
linked account's positions in a single CSV — often more accounts than there
are folders under `data/`, since not every account needs (or has) its own
transaction-history folder. So `AccountRegistry.refresh()` doesn't stop at one
Dashboard per folder: after resolving each folder's own account number
(config or the "whichever snapshot was seen most recently" heuristic
`Dashboard._build_net_worth` falls back to), it scans every Positions file
found anywhere under `data/` for account numbers no folder claimed and gives
each of *those* its own positions-only Dashboard too — Net Worth and holdings
only, since there's no transaction-history column to attribute it by.

An optional `data/accounts.json` covers what auto-discovery can't decide on
its own; see `wheel.accounts.load_account_config` and `AccountConfig`.
`"folders"` — `{"<folder>": "<account number>", ...}` — names a folder's
account explicitly instead of leaving it to the heuristic, which can pick the
wrong one when a folder's own Positions file (or the shared "all accounts"
download) lists several; a configured folder's Dashboard also widens its
search to every discovered Positions file, not just its own folder's, since
its data may only ever appear in someone else's shared download.
`Dashboard.__init__`'s `account_number` argument does the actual filtering —
for a configured folder and an auto-discovered account alike — dropping (with
a warning) any other account numbers the same file(s) also contain, never
silently shown or blended in. `"ignore"` — `["<account number or name>", ...]`
— drops an account entirely, everywhere, by number or by `Account name`.
`"default_account"` — an account id — is which account tab the frontend opens
to instead of Combined, surfaced through `AccountRegistry.default_account_id`
and `/api/accounts`. Transaction history still can't be split by account this
way — no column to split it on — so it stays wholly attributed to whichever
folder it's found in, same as the "Account folders" rule above.

## External cash-flow classification

The benchmark comparison (below) needs to know which non-trade ledger rows are
money actually entering or leaving the account — a wire, a check, a rollover —
versus money that only moved *within* it: a dividend, a fee, interest, or a
corporate-action rename. Counting the latter as a contribution would make an
account look like it needed less of its own performance to reach its ending
value than it actually did.

`wheel/benchmark.py` classifies every non-trade (`OTHER`-action) row with an
ordered, most-specific-first pattern list — the same idiom as
`wheel.parser._ACTION_PATTERNS` — for the same reason: `"DISTRIBUTION
NAME/SYMBOL CHANGE"` (a corporate-action rename, see "Corporate actions" above)
must be matched *before* any looser `"distribution"` rule, or it would be
mistaken for a cash distribution and inflate the account's apparent contributions.

Anything that matches no pattern is `UNCLASSIFIED`: excluded from the
money-weighted calculation and surfaced as one deduped warning per distinct
unmatched action text, rather than guessed at either way. This mirrors the
`UNKNOWN` treatment for an unrecognized Positions row, and the same "detect from
data, never hard-code, surface uncertainty" norm the whole parser is built on.

## Money-weighted benchmark comparison

The question "did the wheel beat just holding stock" is not a point-to-point
value comparison: if the user deposited or withdrew money over time, comparing
the ending values of two accounts that received cash on different dates isn't
comparing the same amount of investing.

**XIRR is the standard fix** — `wheel/benchmark.py`'s `xirr` solves for the
single annualized rate that makes the present value of every dated cash flow net
to zero, using Newton's method with a bisection fallback (stdlib `math` only, no
`numpy`/`scipy`). But XIRR alone only tells you the account's own return; it
says nothing about whether an index fund would have done as well or better with
the *same* money.

**The benchmark has to see the same cash-flow timing as the account did.**
`simulate_benchmark_series` replays every external contribution and withdrawal —
identical dates, identical dollar amounts — into a synthetic SPY position: a
contribution buys shares at that day's close, a withdrawal sells them, and the
result is marked to market at each requested date. Feeding the real account's
own timing into the benchmark, rather than comparing a lump-sum SPY return to
the account's XIRR, is what isolates "did the strategy beat buy-and-hold SPY"
from "the user happened to add money before a rally" — the latter would bias a
naive comparison in either direction, and it is exactly the same reasoning that
makes a fund's own money-weighted return differ from its time-weighted one.

**The tracked transaction history rarely reaches back to when the account was
first funded.** Without accounting for that, XIRR would see only cash moved
*after* the earliest recorded date and ignore whatever balance was already in
place — understating invested capital, or with zero external transfers on
record at all, making the return uncomputable outright (a single flow has no
rate to solve for). So `wheel/api.py`'s `Dashboard._build_benchmark` treats the
account's total value at its *earliest available* Positions snapshot as a
synthetic opening contribution dated that day, and only external transfers
*after* that date are added on top — avoiding a double count on the day the
snapshot and a same-day transfer coincide.

**At least two snapshots, on different dates, are required.** With only one,
the opening-balance flow and the terminal valuation flow fall on the same date
and exactly cancel for *any* rate — Newton's method would converge to its own
initial guess and report it as if it meant something. The dashboard checks for
this explicitly and reports the benchmark section as unavailable with a plain
explanation, rather than a spurious number.

**The combined view pools every account's cash-flow events into one stream**
before computing a single portfolio-wide XIRR, rather than averaging each
account's own rate — the same "recompute from absolutes" principle as combined
ROI above.

## Market data cache

Daily closes for any ticker come from Yahoo Finance's free, no-key chart JSON
endpoint (`https://query1.finance.yahoo.com/v8/finance/chart/<TICKER>?period1=0&period2=<now>&interval=1d`)
over stdlib `urllib.request` — the only network access anywhere in this
project, and the only reason "standard library only" carries a footnote. (An
earlier version used Stooq's CSV endpoint; Stooq now fronts it with a
JavaScript bot challenge a stdlib-only fetch can't solve.) `get_price_series(ticker)`
defaults to `"SPY"`, the original use (the benchmark comparison); Stock
Unrealized P&L calls it for every ticker the dashboard currently holds shares
in. Each ticker's response is cached to its own file, decoupled from Yahoo's
response shape so a change there can't silently corrupt the cache: SPY keeps
the original `data/spy_daily_closes.csv` path so an existing cache on disk
keeps working unchanged, every other ticker gets `data/prices/<TICKER>.csv`.

`get_price_series` never raises: a missing or stale (>1 day old) cache triggers
a refresh attempt, but a failed fetch falls back to whatever cache already
exists — stale is better than unavailable — and only when there is genuinely
neither a working fetch nor any cache does it return an empty series, with a
warning the benchmark section (or, for a non-SPY ticker, `meta.market_data_warnings`)
surfaces rather than a crash. One ticker's fetch failing never touches another's
already-cached series. `price_on_or_before` resolves an arbitrary calendar date (a
weekend, a holiday, the day a Positions snapshot happened to be taken) to the
nearest prior trading-day close, the same way a broker values a non-trading day.

`Dashboard._current_prices()` fetches every held ticker's price once per
`Dashboard` instance, not once per `build()` call: the frontend calls `build()` on
every filter change, and `get_price_series` still does disk I/O and a
cache-freshness check even when it skips the network fetch, so re-running it on
every filter change would multiply that cost by however many times a user narrows
the date range or ticker list.

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
