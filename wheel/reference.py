"""Best-effort static reference data the broker exports don't carry.

Neither Fidelity's transaction history nor its Positions snapshot names a
holding's sector, and there is no earnings feed anywhere in this project. Both
are useful for deciding *which* ticker to wheel next (the covered-call / CSP
candidate tables), so they live here:

* ``SECTOR`` -- a hand-maintained ticker -> sector map. Extend it as new
  tickers show up; an unmapped ticker simply renders as ``-`` and is treated
  as "unknown" for diversification, never guessed.

* ``load_earnings`` -- reads an optional ``earnings.json`` (``{"TICKER":
  "YYYY-MM-DD", ...}``) from the project root or ``data/``. Absent file =>
  empty map => the Earnings column is blank and nothing is flagged. Keep it
  current by hand; a date in the past just reads as stale.
"""

from __future__ import annotations

import json
import os
from datetime import date
from typing import Sequence

# --------------------------------------------------------------------------
# Sectors -- deliberately coarse, one bucket per ticker. ETFs and funds get a
# descriptive category rather than a GICS sector.
# --------------------------------------------------------------------------

SECTOR: dict[str, str] = {
    # Technology
    "AMD": "Technology",
    "FIG": "Technology",
    "FLEX": "Technology",
    "HPE": "Technology",
    "HPQ": "Technology",
    "KEYS": "Technology",
    "MSFT": "Technology",
    "MU": "Technology",
    "NVDA": "Technology",
    "PLTR": "Technology",
    "SMCI": "Technology",
    # Financials
    "ABR": "Financials",
    "BFH": "Financials",
    "BHF": "Financials",
    "BRKB": "Financials",
    "HOOD": "Financials",
    "JXN": "Financials",
    "KEY": "Financials",
    "KSPI": "Financials",
    "LNC": "Financials",
    "PAGS": "Financials",
    "SOFI": "Financials",
    "TIGR": "Financials",
    "WFC": "Financials",
    # Healthcare
    "GILD": "Healthcare",
    "TPST": "Healthcare",
    # Consumer
    "AMZN": "Consumer Discretionary",
    "CROX": "Consumer Discretionary",
    "RIVN": "Consumer Discretionary",
    "TGT": "Consumer Staples",
    "UNFI": "Consumer Staples",
    "CRESY": "Consumer Staples",
    # Energy
    "NBR": "Energy",
    "PBR": "Energy",
    # Industrials
    "QUAD": "Industrials",
    "TPC": "Industrials",
    # Communication Services
    "VEON": "Communication Services",
    # Broad-market / index ETFs
    "IVV": "Index ETF",
    "IWM": "Index ETF",
    "QQQ": "Index ETF",
    "SPY": "Index ETF",
    "VOO": "Index ETF",
    "TQQQ": "Leveraged ETF",
    "JEPQ": "Income ETF",
    # Sector / thematic ETFs
    "XLV": "Healthcare ETF",
    "IBIT": "Crypto ETF",
    "GLD": "Gold ETF",
    "GLDM": "Gold ETF",
    # Closed-end / income funds
    "IGR": "Real Estate Fund",
    "NRO": "Real Estate Fund",
    "RQI": "Real Estate Fund",
    "NVG": "Municipal Bond Fund",
    "PDI": "Bond Fund",
    "PDO": "Bond Fund",
    # Mutual funds
    "FBGRX": "Mutual Fund",
    "FBMPX": "Mutual Fund",
}


def sector_of(ticker: str) -> str | None:
    return SECTOR.get(ticker.upper())


# --------------------------------------------------------------------------
# Earnings dates
# --------------------------------------------------------------------------

EARNINGS_DIRS = (".", "data")


def load_earnings(directories: Sequence[str] = EARNINGS_DIRS) -> dict[str, date]:
    """``{TICKER: date}`` from the first ``earnings.json`` found. Malformed
    entries are skipped, never raised on -- a stale or partly-broken file
    should degrade the column, not the dashboard.
    """
    for directory in directories:
        path = os.path.join(directory, "earnings.json")
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError):
            return {}
        out: dict[str, date] = {}
        for ticker, value in (raw or {}).items():
            try:
                out[str(ticker).upper()] = date.fromisoformat(str(value))
            except (TypeError, ValueError):
                continue
        return out
    return {}
