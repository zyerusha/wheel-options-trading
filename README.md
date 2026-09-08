# Options Wheel Tracker

Local dashboard for analyzing options wheel campaigns from broker transaction exports.

Tracks **premium, realized P/L, capital deployed, annualized return on capital, rolls, assignments, and performance by ticker and wheel cycle** — plus, from a Portfolio Positions export, **true net worth and a money-weighted return comparison against the S&P 500**.

* Python 3.9+
* Standard library only (one optional network call, to fetch SPY price history — see [Net Worth & Benchmark](#net-worth--benchmark))
* Runs entirely locally
* Supports multiple overlapping broker exports, and multiple accounts
* JSON API included

## Quick Start

```bash
python -m wheel.serve
```

Finds broker CSVs in the project folder and `data/`, starts the dashboard at http://127.0.0.1:8765, and opens the browser.

```text
source        1 export
transactions  195
cycles        23 across 19 tickers
cash check    BALANCED (delta 0.0)
```

`BALANCED` means the imported transactions reconcile. `MISMATCH` indicates a data problem.

```bash
python -m wheel.serve --csv export.csv
python -m wheel.serve --csv export1.csv export2.csv
python -m wheel.serve --port 9000 --no-browser
```

## Docker

The dashboard ships as a small, standard-library-only image. Your trade data
never goes into the image; you bind-mount a local folder at run time.

### Pull

```bash
docker pull ghcr.io/zyerusha/wheel-options-trading:latest
```

### Run (bind-mount your data folder)

```powershell
# Windows PowerShell
docker run --rm -p 8765:8765 -v "${PWD}\data:/app/data" ghcr.io/zyerusha/wheel-options-trading:latest
```

```bash
# macOS / Linux
docker run --rm -p 8765:8765 -v "$PWD/data:/app/data" ghcr.io/zyerusha/wheel-options-trading:latest
```

Then open http://localhost:8765. The mounted folder must already contain at
least one broker CSV export (or a Portfolio Positions file), otherwise the
container prints a message and exits.

On Linux, if your user is not uid 1000, match it so uploads stay writable:

```bash
docker run --rm -p 8765:8765 --user "$(id -u):$(id -g)" -v "$PWD/data:/app/data" ghcr.io/zyerusha/wheel-options-trading:latest
```

### Build locally / Compose

```bash
docker build -t wheel-trading:local .

cp .env.example .env      # edit WHEEL_DATA_DIR / WHEEL_PORT
docker compose up --build
```

### Cloud (`$PORT` honored, binds `0.0.0.0`, `WHEEL_DATA_DIR` relocatable)

```bash
docker run --rm -e PORT=8080 -e WHEEL_DATA_DIR=/data -p 8080:8080 -v /some/data:/data ghcr.io/zyerusha/wheel-options-trading:latest
```

### Push to the registry

```bash
docker tag wheel-trading:local ghcr.io/zyerusha/wheel-options-trading:latest
docker push ghcr.io/zyerusha/wheel-options-trading:latest
```

Tagging a release (`git tag v0.1.0 && git push origin v0.1.0`) runs
`.github/workflows/docker-publish.yml`, which builds `linux/amd64` + `linux/arm64`
and pushes `:0.1.0`, `:0.1`, `:sha-…`, and `:latest` to GHCR.

Running locally without Docker is unchanged: `python -m wheel.serve` still binds
`127.0.0.1:8765` and opens a browser. The new `--host 0.0.0.0` flag is what the
container uses.

## Data

### Fidelity

**Accounts & Trade → Portfolio → Activity & Orders → Download**

Drop the CSV into the project folder or `data/`.

The parser handles Fidelity export variations including:

* Transposed `Quantity` / `Price ($)` columns
* Different column and row ordering
* BOMs, header offsets, and disclaimer footers
* Multiple date formats
* `"Processing"` balances
* Assignment equity rows
* Corporate-action ticker renames
* Non-trade ledger rows

### Multiple Exports

Fidelity limits export history, so multiple overlapping files may be needed.

Exports are merged and deduplicated without double-counting trades. Repeated fills within a single export are preserved, and loading the same file twice is a no-op.

For best results, combine exports from the **same account**.

See [docs/DESIGN.md](docs/DESIGN.md) for the merge and deduplication rules.

### Portfolio Positions

Drop a Fidelity **Positions** export (Accounts & Trade → Portfolio → Positions → Download) into `data/` alongside your transaction history to unlock the **Net Worth & Benchmark** section: total account value, cash, and true capital deployed — not just the option-wheel slice — plus a return comparison against the S&P 500.

Positions exports are point-in-time account snapshots (a single moment's holdings), a different shape from the transaction-history exports above. The dashboard tells the two apart automatically from their headers, so both kinds of file can live in the same folder. Add another Positions export later, on a different date, and it becomes a second point on the net worth and benchmark charts — more snapshots over time make both more accurate.

### Closed lots (realized gains)

Drop a Fidelity **Closed Positions / Realized Gain & Loss** export (`Portfolio_Closed_Lots_*.csv`) into `data/` to fill the **Realized Gains** tab: Fidelity's own per-lot table with the short-/long-term split and a `⬇ CSV` re-export, plus a rough per-ticker cross-check against the wheel engine's option P/L over the same period.

### Earnings dates

The Planner and the covered-call candidates table show each holding's next earnings date. These are fetched from Yahoo and cached; to override one (or supply a missing one), add `data/earnings.json`:

```json
{ "MU": "2026-09-30", "WFC": "2026-10-13" }
```

### Multiple Accounts

Organize `data/` into one subfolder per account:

```text
data/
  ira/
    History_for_Account_2025.csv
    Portfolio_Positions_Aug-03-2026.csv
  taxable/
    History_for_Account_2025.csv
    Portfolio_Positions_Aug-03-2026.csv
```

Each folder's files are combined the way a single account's files always have been — but never with another folder's, since transaction-history exports carry no account column and two accounts wheeling the same ticker must not be merged into one cycle.

Files placed directly in `data/` (today's default layout) are treated as one more, implicit account, so nothing has to be reorganized to keep working.

An account switcher appears at the top of the dashboard whenever more than one account is found. **Combined** aggregates every account — cycles and positions are concatenated and tagged by account rather than merged, and return figures are recomputed from the combined totals rather than averaged from each account's own percentage.

#### One Positions file listing several accounts

Fidelity's "all accounts" Positions download lists every linked account in a single file — often more accounts than there are folders under `data/`, since not every account needs its own transaction-history folder. Any account number the dashboard finds that no folder claims gets its own tab automatically (Net Worth and holdings only — there's no transaction history to show for it). No config needed for that part.

An optional `data/accounts.json` covers four things that auto-discovery can't:

```json
{
  "folders": {
    "ira": "Z12345678",
    "taxable": "Z98765432"
  },
  "ignore": ["Fidelity Go account"],
  "default_account": "ira",
  "default_range": "ytd"
}
```

* **`folders`** — names a folder's account explicitly instead of leaving it to the "whichever snapshot was seen most recently" heuristic, which can pick the wrong one when a folder's own Positions file (or the shared "all accounts" download) lists several. Once named, that folder is filtered to just that account's rows — wherever they're found, including a shared download that physically lives in a different folder — and any other account in the same file is dropped with a warning rather than shown or blended in.
* **`ignore`** — hides an account everywhere (its own tab and Combined), by account number or by its `Account name` exactly as Fidelity reports it (case-insensitive).
* **`default_account`** — which account tab the dashboard opens to, instead of Combined. Use an id from `/api/accounts` (a folder name, or an auto-discovered slug like `roth-ira`).
* **`default_range`** — which date-range preset the dashboard opens to, instead of All. One of `all`, `ytd`, `1y`, `3y`, `5y`, a specific calendar year (`year:2025`), or a bare day count — the same presets the filter row's dropdown offers.

Transaction history is never split by account this way — no column to split it on — so it stays wholly attributed to whichever folder it's found in.

## Wheel Model

A **cycle** starts when a position is opened and ends when the ticker becomes completely flat — no contracts and no shares.

Rolls, scaled entries, assignments, and subsequent covered calls remain part of the same cycle.

Short options are treated as covered:

* Short put → cash-secured put
* Short call → covered call

Assignment equity legs are taken from the broker export when available. Otherwise, share legs are synthesized at the option strike and flagged as estimated.

If shares supporting a covered call predate the available export history, their cost basis is unknown. The tracker reports stock P/L as zero, flags the cycle, and estimates capital at `strike × 100`.

See [docs/DESIGN.md](docs/DESIGN.md) for the accounting and cycle rules.

## Dashboard

### Metrics

| Metric                                | Description                                                    |
| -------------------------------------- | --------------------------------------------------------------- |
| **Net realized P/L**                   | Closed option premium P/L plus realized stock P/L               |
| **Premium collected (net)**            | Option credits minus debits paid to close — excludes stock P/L  |
| **Annualized Wheel ROC**               | Option P/L only ÷ time-weighted average capital, scaled to a year — never includes stock P/L |
| **Capital deployed**                   | Current, average, and peak capital                              |
| **Win rate**                           | Winning legs ÷ (winning + losing legs), among closed legs only   |
| **Cash reconciliation**                | Import accounting check                                         |

Stock price movement (gains/losses on assigned or called-away shares) is never
mixed into the premium or Wheel ROC figures above — it is its own line,
`stock_realized_pl`, shown separately everywhere premium appears. The Wheel ROC
measures what the option strategy itself earned; it treats assigned shares as
capital tied up, not as a gain or loss the wheel took. Your broker already
reports total stock P/L if you want the full investment picture.

Protective puts and credit-spread legs used to hedge the wheel count too — every
option leg in a cycle contributes its own realized P/L (open cash and every
closing cash flow, netted), regardless of whether it's a CSP, a covered call, a
long put bought for protection, or one side of a spread. A put bought for
$1,000 and later sold for $700 is a $300 hedge cost that reduces Wheel ROC, not
a $1,000 loss and not a $0 wash. `wheel_core_realized_pl` (CSP + covered calls)
and `hedge_realized_pl` (everything else) are exposed as the two addends of
`option_realized_pl` so the calculation tooltip can show the split.

Win rate is a secondary, diagnostic figure — how often a closed leg was
profitable — not a return metric. It excludes open/unresolved legs entirely
(they're neither a win nor a loss yet) and excludes exact break-even legs from
its denominator, so `wins + losses` can be smaller than the total leg count;
the dashboard shows both counts side by side rather than implying they match.
Annualized Wheel ROC remains the primary performance number.

### Charts

* **Capital deployed** — daily shares, put collateral, and uncovered-share call capital
* **Cumulative premium P/L, stock P/L, and full wheel P/L**
* **Realized P/L by ticker**
* **Annualized Wheel ROC** — option P/L only, per ticker
* **Wheel timelines** — legs, rolls, and assignments

Every chart has a **Table** view.

`~` marks estimated capital.

### Filters

Date range, status, and ticker filters rebuild the dashboard from the filtered transaction set, keeping metrics, charts, and tables consistent.

A start date crops the view, not a position's history: capital already committed before the window still shows correctly on day one instead of appearing to start from zero.

## Returns and Capital

Capital is tracked as a daily timeline:

* Cash-secured put → `strike × 100 × contracts`
* Assigned shares → actual cost basis
* Covered call → no additional capital when shares are already tracked
* Covered call with untracked shares → `strike × 100` estimated capital
* Long option (a protective put, or the long leg of a credit-spread hedge) →
  the actual debit paid, never the option's notional, and it drops to $0 the
  day the leg closes (its cost has moved into realized P/L by then)

A short leg still gets full cash-secured-put/covered-call collateral even when
it's economically one side of a defined-risk spread — nothing here pairs legs
into spreads, so it can't net a spread down to its true (smaller) margin. That
overstates capital, and understates ROC, for a hedged position versus what a
broker would actually require; getting it right needs matching legs by
underlying/right/expiry/side/timing, which is ambiguous enough (concurrent
spreads, rolls, partial fills) that a wrong pairing would be worse than this
conservative overstatement.

The Annualized Wheel ROC uses **time-weighted average capital** and excludes days with zero
committed capital. Its numerator is option P/L — CSPs, covered calls, protective puts, and
credit-spread legs alike — never stock price movement on assigned or called-away shares,
whether realized, unrealized, a gain, or a loss.

Portfolio ROC is calculated from the portfolio's own capital timeline rather than averaging
individual cycle returns.

## Net Worth & Benchmark

Requires at least one Portfolio Positions export (see [Portfolio Positions](#portfolio-positions) above); the return comparison needs at least two, taken on different dates.

This section sits above the filter row — it always reflects the full account, not the filtered slice — and shows:

* **Total value, cash, and true capital deployed** — the whole account, including buy-and-hold positions an option has never touched, not just the wheel-tracked slice.
* **A money-weighted (XIRR) return** on the actual account, compared against the same dollars invested in SPY on the same dates. Deposits and withdrawals found in the transaction history are replayed into both sides identically, so the comparison isolates whether the strategy beat a buy-and-hold benchmark from whether money happened to arrive before a rally.
* **Value added** — the dollar difference between the actual account and the SPY simulation, as of the most recent snapshot.

SPY price history is fetched once from a free, no-key public source and cached under `data/`; after the first fetch it works offline. See [docs/DESIGN.md](docs/DESIGN.md) for the cash-flow classification and benchmark-replay methodology.

## Reconciliation

The engine uses the broker's `Amount ($)` as the authoritative cash value rather than recalculating cash from price and quantity.

Equity fills allow a small tolerance because brokers may display rounded average prices. Non-trade ledger rows such as dividends and collateral marks are excluded from trade reconciliation.

A `BALANCED` result means the imported transaction cash reconciles to the broker data.

## JSON API

| Route                | Description                                  |
| -------------------- | --------------------------------------------- |
| `GET /api/dashboard` | Dashboard data, including `net_worth`, `benchmark` / `benchmarks`, `assignment_risk`, `expiration_calendar`, `workflow`, `earnings_in_view`, `realized_gains` |
| `GET /api/health`    | Health and reconciliation status              |
| `GET /api/datasets`  | Available/active exports (default account)    |
| `GET /api/accounts`  | Every discovered account                      |
| `GET /api/export/{cycles,tickers,trade-log,closed-lots}.csv` | Flat CSV of that payload list, with the same filters applied |
| `POST /api/upload`   | Upload CSVs (default account)                 |
| `POST /api/select`   | Select active exports (default account)       |

Load `#present` in the URL (or the **Present** button) for a chrome-free, large-tile view for screenshots.

`/api/dashboard` also accepts `account=<id>` (an id from `/api/accounts`, or `combined` — the default) to scope the whole payload, filters included, to one account or every account aggregated.

Dashboard filters:

```text
tickers=MU,QQQ
start=2025-10-01
end=2025-10-31
status=ACTIVE,ASSIGNED
```

Example:

```bash
curl "http://127.0.0.1:8765/api/dashboard?tickers=MU&status=ACTIVE"
```

## Python API

```python
from wheel.parser import parse_exports
from wheel.engine import build_cycles
from wheel.metrics import portfolio_metrics

transactions, reports, merge = parse_exports(["export.csv"])
cycles, engine = build_cycles(transactions)
through = max(t.event_date for t in transactions)

portfolio = portfolio_metrics(cycles, through)
print(f"{portfolio.net_realized_pl:,.2f} net")
```

Or use the dashboard directly:

```python
Dashboard(paths).build(Filters(...))
```

## Tests

```bash
python -m unittest discover -s .
```

Integration tests independently verify the engine against the raw CSV exports, including shared invariants across different export formats.

## Troubleshooting

**No export found**

```bash
python -m wheel.serve --csv path/to/file.csv
```

**Port in use**

```bash
python -m wheel.serve --port 8766
```

**`cash check MISMATCH`**

Inspect `reconciliation.row_failures` in the API response. Usually indicates an incomplete, modified, or non-standard export.

**Closing rows have no opening leg**

The position may predate the export window. Load an earlier export.

**Net worth section is empty**

No Portfolio Positions export was found for that account. Drop one into `data/` (or the account's subfolder) and refresh.

**Benchmark unavailable**

Either fewer than two Portfolio Positions snapshots exist yet for that account, or the SPY price fetch failed with no local cache — check `benchmark.warnings` in the API response. The rest of the dashboard is unaffected either way.

**Ticker shows `~`**

Capital is estimated because the supporting shares predate the available export history.

## Project Layout

| Path                   | Purpose                                       |
| ---------------------- | ---------------------------------------------- |
| `wheel/parser.py`      | CSV → normalized transactions                  |
| `wheel/engine.py`      | Positions, rolls, assignments, cycles          |
| `wheel/metrics.py`     | P/L, ROC, capital                              |
| `wheel/positions.py`   | Portfolio Positions snapshot parser            |
| `wheel/closed_lots.py` | Fidelity closed-lots (realized gains) parser   |
| `wheel/marketdata.py`  | SPY/QQQ price history and fundamentals, cached |
| `wheel/benchmark.py`   | Cash-flow classification, XIRR, benchmark replay |
| `wheel/assignment.py` `wheel/expiration.py` `wheel/workflow.py` `wheel/taxes.py` | Planner and Realized-Gains logic |
| `wheel/exporter.py`    | CSV export of payload lists                    |
| `wheel/accounts.py`    | Account discovery and the Combined view        |
| `wheel/api.py`         | JSON payload assembly                          |
| `wheel/serve.py`       | HTTP server                                    |
| `wheel/paths.py`       | Resolves the data directory (`WHEEL_DATA_DIR`) |
| `wheel/static/`        | Dashboard (Dashboard / Planner / Realized Gains / Trade Log tabs) |
| `tests/`               | Tests                                          |
| `Dockerfile` `docker-compose.yml` `docker-entrypoint.sh` | Container packaging (see [Docker](#docker)) |
| `docs/DESIGN.md`       | Detailed design and accounting rules           |

For the detailed implementation decisions, edge cases, validation results, and accounting model, see [docs/DESIGN.md](docs/DESIGN.md).
