"""Fidelity's combined, multi-account transaction-history export (an
"Accounts_History.csv"-style download) is not supported yet -- see
docs/DESIGN.md, "Account folders". These tests guard the interim protection:
such a file must never be silently imported (its column names don't match
this parser's expected header, so every dollar amount would come out zero),
and its presence must be surfaced rather than just vanish from discovery with
no explanation.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wheel.accounts import AccountRegistry  # noqa: E402
from wheel.api import (  # noqa: E402
    discover_exports,
    discover_multi_account_exports,
    looks_like_export,
    looks_like_multi_account_export,
)

SINGLE_ACCOUNT_HEADER = (
    "Run Date,Action,Symbol,Description,Type,Quantity,Price ($),Commission ($),"
    "Fees ($),Accrued Interest ($),Amount ($),Cash Balance ($),Settlement Date"
)
MULTI_ACCOUNT_HEADER = (
    "Run Date,Account,Account Number,Action,Symbol,Description,Type,Exchange Quantity,"
    "Exchange Currency,Currency,Price,Quantity,Exchange Rate,Commission,Fees,"
    "Accrued Interest,Amount,Settlement Date"
)
MULTI_ACCOUNT_ROW = (
    '08/05/2026,"Zafrir\'s IRA",488896608,DIVIDEND RECEIVED,JEPQ,'
    "J P MORGAN EXCHANGE TRADED FD,Cash,0,\"\",USD,\"\",0,0,\"\",\"\",\"\",352.49,\"\""
)
SINGLE_ACCOUNT_ROW = (
    "08/05/2026,YOU SOLD OPENING TRANSACTION PUT (MU) ...,-MU251003P157.5,"
    'PUT (MU) ...,Cash,-1,3.35,0,0,,335.00,10000.00,08/06/2026'
)


def _write_csv(header: str, rows: list[str], directory: str, name: str) -> str:
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        handle.write(header + "\n")
        handle.write("\n".join(rows) + "\n")
    return path


class TestMultiAccountDetection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_multi_account_header_is_detected(self):
        path = _write_csv(MULTI_ACCOUNT_HEADER, [MULTI_ACCOUNT_ROW], self.tmp.name, "Accounts_History.csv")
        self.assertTrue(looks_like_multi_account_export(path))

    def test_multi_account_export_is_excluded_from_looks_like_export(self):
        path = _write_csv(MULTI_ACCOUNT_HEADER, [MULTI_ACCOUNT_ROW], self.tmp.name, "Accounts_History.csv")
        self.assertFalse(looks_like_export(path))

    def test_single_account_export_is_unaffected(self):
        path = _write_csv(SINGLE_ACCOUNT_HEADER, [SINGLE_ACCOUNT_ROW], self.tmp.name, "History_for_Account.csv")
        self.assertTrue(looks_like_export(path))
        self.assertFalse(looks_like_multi_account_export(path))

    def test_discover_exports_skips_it_discover_multi_account_exports_finds_it(self):
        _write_csv(MULTI_ACCOUNT_HEADER, [MULTI_ACCOUNT_ROW], self.tmp.name, "Accounts_History.csv")
        good = _write_csv(SINGLE_ACCOUNT_HEADER, [SINGLE_ACCOUNT_ROW], self.tmp.name, "History_for_Account.csv")
        self.assertEqual(discover_exports([self.tmp.name]), [good])
        multi = discover_multi_account_exports([self.tmp.name])
        self.assertEqual(len(multi), 1)
        self.assertTrue(multi[0].endswith("Accounts_History.csv"))


class TestRegistrySurfacesExclusion(unittest.TestCase):
    """The registry must not silently import the file, and must say why."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = os.path.join(self.tmp.name, "data")
        os.makedirs(self.data_dir)
        _write_csv(MULTI_ACCOUNT_HEADER, [MULTI_ACCOUNT_ROW], self.data_dir, "Accounts_History.csv")

    def test_not_counted_as_a_transaction(self):
        registry = AccountRegistry(base_dir=self.data_dir, extra_dirs=())
        # The only file present is the unsupported multi-account export -- no
        # account should have picked up a transaction from it.
        for row in registry.list_accounts():
            self.assertEqual(row["transactions"], 0)

    def test_exclusion_is_reported_as_a_warning(self):
        registry = AccountRegistry(base_dir=self.data_dir, extra_dirs=())
        self.assertTrue(
            any("Accounts_History.csv" in w and "not supported" in w for w in registry._build_warnings)
        )

    def test_a_sibling_single_account_export_still_imports_normally(self):
        _write_csv(SINGLE_ACCOUNT_HEADER, [SINGLE_ACCOUNT_ROW], self.data_dir, "History_for_Account.csv")
        registry = AccountRegistry(base_dir=self.data_dir, extra_dirs=())
        payload = registry.build("default")
        self.assertEqual(payload["portfolio"]["cycles"], 1)  # one opening leg -> one active cycle
        self.assertEqual(payload["meta"]["transactions_total"], 1)


if __name__ == "__main__":
    unittest.main()
