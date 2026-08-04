# Options Wheel Tracker

Local dashboard for analyzing options wheel campaigns from broker transaction exports.

Tracks **premium, realized P/L, capital deployed, annualized return on capital, rolls, assignments, and performance by ticker and wheel cycle**.

* Python 3.9+
* Standard library only
* Runs entirely locally
* Supports multiple overlapping broker exports
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
| **Annualized ROC — premium only**      | Premium P/L ÷ time-weighted average capital, excludes stock P/L |
| **Annualized ROC — full wheel**        | Net P/L (premium + stock) ÷ time-weighted average capital       |
| **Capital deployed**                   | Current, average, and peak capital                              |
| **Win rate**                           | Percentage of profitable closed legs                            |
| **Cash reconciliation**                | Import accounting check                                         |

Stock price movement (gains/losses on assigned or called-away shares) is never
mixed into the premium figures above — it is its own line, `stock_realized_pl`,
shown separately everywhere premium appears.

### Charts

* **Capital deployed** — daily shares, put collateral, and uncovered-share call capital
* **Cumulative premium P/L, stock P/L, and full wheel P/L**
* **Realized P/L by ticker**
* **Annualized return on capital** — premium only and full wheel, per ticker
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

Annualized ROC uses **time-weighted average capital** and excludes days with zero committed capital.

Portfolio ROC is calculated from the portfolio's own capital timeline rather than averaging individual cycle returns.

## Reconciliation

The engine uses the broker's `Amount ($)` as the authoritative cash value rather than recalculating cash from price and quantity.

Equity fills allow a small tolerance because brokers may display rounded average prices. Non-trade ledger rows such as dividends and collateral marks are excluded from trade reconciliation.

A `BALANCED` result means the imported transaction cash reconciles to the broker data.

## JSON API

| Route                | Description                      |
| -------------------- | -------------------------------- |
| `GET /api/dashboard` | Dashboard data                   |
| `GET /api/health`    | Health and reconciliation status |
| `GET /api/datasets`  | Available/active exports         |
| `POST /api/upload`   | Upload CSVs                      |
| `POST /api/select`   | Select active exports            |

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

**Ticker shows `~`**

Capital is estimated because the supporting shares predate the available export history.

## Project Layout

| Path               | Purpose                               |
| ------------------ | ------------------------------------- |
| `wheel/parser.py`  | CSV → normalized transactions         |
| `wheel/engine.py`  | Positions, rolls, assignments, cycles |
| `wheel/metrics.py` | P/L, ROC, capital                     |
| `wheel/api.py`     | JSON API                              |
| `wheel/serve.py`   | HTTP server                           |
| `wheel/static/`    | Dashboard                             |
| `tests/`           | Tests                                 |
| `docs/DESIGN.md`   | Detailed design and accounting rules  |

For the detailed implementation decisions, edge cases, validation results, and accounting model, see [docs/DESIGN.md](docs/DESIGN.md).
