# Periodic P/L chart

What the Dashboard's *Periodic P/L* card shows, exactly how each number is
calculated, and why it's a useful view alongside the *Cumulative Wheel P/L*
chart directly above it.

Source: `wheel.metrics.periodic_pl_series()` (`wheel/metrics.py`), rendered by
`drawPeriodPl()` in `wheel/static/app.js`.

## Division of responsibility with Cumulative Wheel P/L

The card right above this one, *Cumulative Wheel P/L*, answers "how has the
wheel performed over time?" — running totals of option P/L, stock P/L, full
wheel P/L, and the current mark-to-market value of what's still open.

*Periodic P/L* deliberately does not repeat any of that. It answers a
narrower, complementary question: **"what did I actually realize this
particular week or month, and where did it come from?"** No cumulative line,
no open-position value, no return-on-capital — those already live one card up
or elsewhere on the dashboard. This card's only job is the period-by-period
breakdown and volatility that a running total smooths away.

## What it shows

One bar per week or calendar month (toggle in the card header), split into
two stacked segments:

| Segment | What it is |
|---|---|
| **Option P/L** | Realized profit/loss from options that closed in that period (bought back, expired, or assigned/exercised) |
| **Stock P/L** | Realized profit/loss from shares that were sold or called away in that period |

**Total Realized P/L** (Option P/L + Stock P/L) is not drawn as a third
segment — it's the bar's own net position relative to zero, and it's always
in the tooltip and the table.

A bar sits above the zero line for a gain, below it for a loss. A quiet
period (nothing closed) is a real `$0`, not a missing data point — the chart
zero-fills every period from the first realized trade through today, and
draws a flat tick on the baseline so a $0 period still reads as "nothing
happened here," not "no data here."

**Every number is dated to when a position actually closed**, not to when it
was opened or when cash moved. A put sold in March and expiring worthless in
June shows its (small) gain in June's Option P/L, not March's — see
[Not the same as cash flow](#not-the-same-as-cash-flow) below.

### Reading a bar: how stacking handles mixed signs

Option P/L and Stock P/L stack from a shared zero baseline, not from each
other's ends, so one segment can never visually eat into the other:

- **Same sign** (the common case): both stack contiguously on the same side
  of zero — Option P/L touching the baseline, Stock P/L extending beyond it.
  The bar's full height is simply their sum.
- **Opposite signs** (e.g. a loss on the shares assigned away more than
  offset by the premium collected): each segment extends to its *own* side
  of zero instead of one subtracting visually from the other. You read the
  green and red pieces independently and the net from the tooltip — nothing
  is hidden by two colors overlapping.

This is the standard diverging-stacked-bar convention: it is the only way to
show two signed quantities and their sum without letting a gain and a loss
cancel each other out on screen.

## The math

### Option P/L (per option leg, per close event)

Each time a contract closes — in full or in part, via buy-to-close,
expiration, or assignment/exercise — it realizes:

```
leg_cash_per_contract = leg.open_cash / leg.contracts

realized_pl(event) = leg_cash_per_contract × contracts_closed_this_event
                      + cash_of_this_close_event
```

- `open_cash` is the total cash the leg brought in when opened: positive for
  a credit (sell-to-open premium), negative for a debit (a long hedge leg).
  Dividing by the leg's total contract count gives the per-contract share, so
  a leg closed across several events (e.g. 2 of 5 contracts bought back early,
  3 assigned later) allocates its opening premium proportionally.
- `cash_of_this_close_event` is whatever cash changed hands to close that
  slice: negative for a buy-to-close debit, zero for expiration or
  assignment (the option itself generates no further cash — the share side
  is captured separately, in Stock P/L).

**Option P/L for a period** = sum of `realized_pl(event)` over every close
event whose date falls in that week/month.

*Worked example:* a cash-secured put sold for a $500 credit is later bought
back for $400. `leg_cash_per_contract = 500` (1 contract). At the buy-to-close
event, `cash_of_this_close_event = -400`. Realized P/L for that event =
`500 × 1 + (-400) = $100`, booked in the period the buy-to-close happened.
This is also why the card says **Option P/L**, not "premium": the $500
collected is not what lands here — the $100 actually kept is.

### Stock P/L (per share lot, per disposal)

Each time shares are sold or called away, against a specific tax lot:

```
realized(disposal) = (sale_price - lot.basis_per_share) × shares_sold
```

Lots are matched FIFO (oldest first), with one exception: if a disposal's
share count matches one open lot's remaining shares exactly, that whole lot
is used, so "100 shares called away" reads as one lot closing, not a sliver
off several.

**Stock P/L for a period** = sum of `realized(disposal)` over every disposal
whose date falls in that week/month, for lots whose cost basis is known. (A
lot bought before the earliest loaded transaction history has an unknown
basis; its disposals are flagged separately on the dashboard and left out of
this figure rather than guessed at — same handling as everywhere else on the
dashboard.)

### Total Realized P/L

```
Total Realized P/L = Option P/L + Stock P/L
```

Deliberately realized-only — no mark-to-market, no unrealized gain on shares
still held, no dividends beyond whatever the dashboard's realized wheel P/L
already includes, no cash flow, no deposits/withdrawals. This is the exact
same definition as *Net Realized P/L* everywhere else on the dashboard (the
Cycles table, Realized P/L by ticker), so the numbers tie out instead of each
chart inventing its own blend.

### Bucketing

- **Weekly**: ISO weeks, Monday-anchored. **Monthly**: calendar months.
- Buckets run from the first period with a realized flow through today,
  zero-filled in between — a bucket is never fabricated before trading
  started or after the current date.
- Both granularities are computed together server-side; the Weekly/Monthly
  toggle just swaps which already-fetched series is drawn, no refetch.

### Period summary

The compact line above the chart (Total realized / Option P/L / Stock P/L)
sums exactly the bars on display — the full trading history by default, or
the cropped date range if the dashboard's date filter is set.

## Not the same as cash flow

This chart is **not** cash flow. A put sold for a $500 credit shows $0 here
on the day it was sold — it only shows up once it closes (bought back,
expired, or assigned), and only the $100 actually kept, not the $500
collected. The *Monthly cash flow* card elsewhere on the dashboard dates
everything to settlement instead (when premium was collected, when a
buy-to-close debit was paid) and can show a big "cash in" month from
premium that hasn't been realized yet. The *Cash flow vs. wheel P/L gap*
card quantifies that difference directly.

## Why it's useful

- **It isolates the two distinct sources of wheel profit.** Premium income
  (collecting and managing options) and stock price movement (gain or loss
  on shares you end up holding or get called away from) are economically
  different bets. Blending them into one number hides which one is actually
  driving results — a period could show a solid gain entirely from a lucky
  stock bounce while the options themselves lost money, or vice versa.
- **It's dated to when P/L is actually realized, not when cash moved** — see
  [Not the same as cash flow](#not-the-same-as-cash-flow).
- **It matches the dashboard's other realized-P/L totals by construction.**
  Total Realized P/L uses the exact same definition as Net Realized P/L
  elsewhere, so there's nothing to reconcile — the periodic breakdown always
  sums to the same totals shown in the Cycles table and the ticker-level
  breakdown.
- **A flat or negative stretch is diagnostic, not just descriptive.** Since
  gaps are real zeros and nothing is smoothed or interpolated, a visibly flat
  run in Option P/L says "nothing closed this period" (normal if positions
  are mid-cycle), while a negative run in Stock P/L specifically says shares
  were sold at a loss — a different, more actionable signal than a vague dip
  in a single blended total.
- **It's a two-second read.** One glance at a bar's side of zero and its
  color split answers "profitable or not, and from options or stock" without
  having to parse a trend line across periods that aren't actually connected
  to each other.
