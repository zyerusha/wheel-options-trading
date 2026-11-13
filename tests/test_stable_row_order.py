"""Regression tests: payload lists must not depend on Python's per-process
hash randomization (set iteration order).

The tickers table used to iterate a set of tickers and sort only by net
realized P&L, so tickers with equal P&L came out in a different order after a
server restart. Option-chain warnings followed set order the same way. Ties
now break on the ticker name. The primary sort keys are unchanged.

Each check runs in fresh interpreters with different PYTHONHASHSEED values,
so the old set-ordered code fails reliably rather than by chance.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from datetime import date, datetime, timezone
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from wheel import api  # noqa: E402
from wheel.accounts import _combine_tickers  # noqa: E402

# Share-only buys: every ticker has a net realized P&L of exactly zero, so
# the whole order comes from the tie-break.
_TICKER_ORDER_SCRIPT = """
import json, sys
from datetime import date
sys.path.insert(0, {root!r})
from tests.test_engine import tx
from wheel.engine import build_cycles
from wheel.metrics import ticker_summary
tickers = {tickers!r}
cycles, _ = build_cycles([tx("2025-01-06", "BUY_STOCK", t, 100, 10.0, -1000.0, row_id=i + 1) for i, t in enumerate(tickers)])
rows = ticker_summary(cycles, date(2025, 6, 30))
print(json.dumps([(r["underlying"], r["net_realized_pl"]) for r in rows]))
"""

_CHAIN_ORDER_SCRIPT = """
import json, sys
from datetime import date
from unittest import mock
sys.path.insert(0, {root!r})
from wheel import api, marketdata
pairs = {{(t, date(2030, 1, 18)) for t in {tickers!r}}}
def failing_fetch(ticker, expiry, timeout=10.0):
    raise marketdata.MarketDataError("offline")
with mock.patch.object(marketdata, "fetch_yahoo_option_chain", failing_fetch):
    dashboard = api.Dashboard.__new__(api.Dashboard)
    chains = dashboard._option_chains(pairs)
print(json.dumps([warning for _, warning in chains.values()]))
"""

TICKERS = ["ZM", "AAPL", "MU", "KO", "BRKB", "QQQ", "F", "T", "XOM", "GE"]
SEEDS = ("1", "2", "3", "4")


def _run(script: str, seed: str) -> list:
    env = {**os.environ, "PYTHONHASHSEED": seed}
    completed = subprocess.run(
        [sys.executable, "-c", script.format(root=ROOT, tickers=TICKERS)],
        capture_output=True, text=True, env=env, cwd=ROOT, timeout=120, check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr)
    return json.loads(completed.stdout.strip().splitlines()[-1])


class StableRowOrderTest(unittest.TestCase):
    def test_ticker_rows_with_equal_pl_sort_by_ticker_under_any_hash_seed(self):
        runs = {seed: _run(_TICKER_ORDER_SCRIPT, seed) for seed in SEEDS}
        for seed, rows in runs.items():
            self.assertEqual([t for t, _ in rows], sorted(TICKERS), f"PYTHONHASHSEED={seed}")
            self.assertTrue(all(pl == 0 for _, pl in rows))

    def test_option_chain_warnings_follow_sorted_pairs_under_any_hash_seed(self):
        expected = [
            f"could not fetch option chain for {t} 2030-01-18: offline" for t in sorted(TICKERS)
        ]
        for seed in SEEDS:
            self.assertEqual(_run(_CHAIN_ORDER_SCRIPT, seed), expected, f"PYTHONHASHSEED={seed}")

    def test_combined_ticker_rows_keep_pl_order_and_break_ties_by_ticker_then_account(self):
        payloads = {
            "ira": {"tickers": [{"underlying": "ZM", "net_realized_pl": 0.0}, {"underlying": "MU", "net_realized_pl": 50.0}]},
            "joint": {"tickers": [{"underlying": "AAPL", "net_realized_pl": 0.0}, {"underlying": "ZM", "net_realized_pl": 0.0}]},
            "roth": {"tickers": [{"underlying": "KO", "net_realized_pl": -5.0}, {"underlying": "AAPL", "net_realized_pl": 0.0}]},
        }
        order = [(row["underlying"], row["account_id"]) for row in _combine_tickers(payloads)]
        self.assertEqual(
            order,
            [("MU", "ira"), ("AAPL", "joint"), ("AAPL", "roth"), ("ZM", "ira"), ("ZM", "joint"), ("KO", "roth")],
        )


class OptionChainKeepsAppTickerTest(unittest.TestCase):
    def test_brkb_chain_snapshot_carries_brkb_not_yahoo_spelling(self):
        expiry = date(2026, 12, 18)
        epoch = int(datetime(2026, 12, 18, tzinfo=timezone.utc).timestamp())
        body = json.dumps(
            {
                "optionChain": {
                    "result": [
                        {
                            "underlyingSymbol": "BRK-B",
                            "quote": {"regularMarketPrice": 570.89},
                            "options": [{"expirationDate": epoch, "calls": [], "puts": []}],
                        }
                    ]
                }
            }
        )
        with mock.patch.object(api.marketdata, "fetch_yahoo_option_chain", lambda ticker, expiry: body):
            pair, snapshot, warning = api._fetch_option_chain(("BRKB", expiry))
        self.assertIsNone(warning)
        self.assertEqual(pair, ("BRKB", expiry))
        self.assertEqual(snapshot.ticker, "BRKB")
        self.assertEqual(snapshot.underlying_price, 570.89)


if __name__ == "__main__":
    unittest.main()
