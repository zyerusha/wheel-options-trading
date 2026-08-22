"""Dashboard server-state tests: upload validation and the shared
write/rollback path between the default-bucket and named-account uploaders.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wheel.serve as serve  # noqa: E402
from wheel.serve import DashboardState, DatasetError  # noqa: E402

HISTORY_HEADER = (
    "Run Date,Action,Symbol,Description,Type,Quantity,Price ($),Commission ($),"
    "Fees ($),Accrued Interest ($),Amount ($),Cash Balance ($),Settlement Date"
)
POSITIONS_HEADER = (
    "Account number,Account name,Symbol,Description,Quantity,Last price,Last price change,"
    "Current value,Today's gain/loss dollar,Today's gain/loss percent,Total gain/loss dollar,"
    "Total gain/loss percent,Percent of account,Cost basis total,Average cost basis,Type"
)

ONE_TRADE_ROW = (
    '09/19/2025,"YOU SOLD OPENING TRANSACTION PUT (MU) ...",-MU250926P150,'
    '"PUT ...",Cash,-1,3.35,0,0,,335.00,10000.00,09/19/2025'
)


def _write_history_csv(path: str, rows: list[str] = ()) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        handle.write(HISTORY_HEADER + "\n")
        handle.write("\n".join(rows) + ("\n" if rows else ""))


def _write_positions_csv(path: str) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        handle.write(POSITIONS_HEADER + "\n")
        handle.write(
            '111111111,"Some Account",MU,MU DESCRIPTION,10,$100.00,,'
            "$1000.00,,,,,10.00%,$1000.00,$100.00,Cash,\n"
        )
        handle.write("\n")
        handle.write('"Date downloaded Sep-25-2025 5:00 p.m ET"\n')


class TestValidateRejectsEmptyTransactionUpload(unittest.TestCase):
    """Regression guard: _validate() must reject a non-empty `paths` that
    parses to zero transactions, even when unrelated Positions snapshots
    exist elsewhere in the project -- Dashboard(paths) auto-discovers every
    Positions file on disk, so `dashboard.snapshots` is not a fact about
    `paths` at all.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_empty_history_file_is_rejected_even_with_unrelated_positions_on_disk(self):
        empty_history = os.path.join(self.tmp.name, "History_for_Account.csv")
        _write_history_csv(empty_history, rows=[])
        positions = os.path.join(self.tmp.name, "Portfolio_Positions.csv")
        _write_positions_csv(positions)

        with patch.object(serve, "PROJECT_ROOT", self.tmp.name), patch.object(
            serve, "UPLOAD_DIR", self.tmp.name
        ):
            with self.assertRaises(DatasetError):
                DashboardState._validate([empty_history])

    def test_positions_only_upload_with_empty_paths_is_accepted(self):
        positions = os.path.join(self.tmp.name, "Portfolio_Positions.csv")
        _write_positions_csv(positions)

        with patch.object(serve, "PROJECT_ROOT", self.tmp.name), patch.object(
            serve, "UPLOAD_DIR", self.tmp.name
        ):
            dashboard = DashboardState._validate([])
            self.assertEqual(len(dashboard.transactions), 0)
            self.assertTrue(dashboard.snapshots)

    def test_real_history_file_still_accepted(self):
        history = os.path.join(self.tmp.name, "History_for_Account.csv")
        _write_history_csv(history, rows=[ONE_TRADE_ROW])

        with patch.object(serve, "PROJECT_ROOT", self.tmp.name), patch.object(
            serve, "UPLOAD_DIR", self.tmp.name
        ):
            dashboard = DashboardState._validate([history])
            self.assertEqual(len(dashboard.transactions), 1)


class TestWriteUploadsRollback(unittest.TestCase):
    """_write_uploads/_rollback_uploads: the shared write-then-validate path
    both DashboardState.accept_uploads and Handler._accept_account_upload
    use -- an unrecognized file must be rejected and leave nothing behind.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_unrecognized_file_is_rejected_and_rolled_back(self):
        target_dir = os.path.join(self.tmp.name, "some-account")
        written: list[str] = []
        with patch.object(serve, "PROJECT_ROOT", self.tmp.name), patch.object(
            serve, "UPLOAD_DIR", self.tmp.name
        ):
            with self.assertRaises(DatasetError):
                serve._write_uploads(target_dir, [("junk.csv", b"not,a,real,export\n1,2,3,4\n")], written)
            serve._rollback_uploads(written)

        self.assertTrue(written)  # something was written before the rejection...
        for path in written:
            self.assertFalse(os.path.exists(path))  # ...but rollback removed it

    def test_recognized_history_file_is_written_and_returned(self):
        target_dir = os.path.join(self.tmp.name, "some-account")
        written: list[str] = []
        body = (HISTORY_HEADER + "\n" + ONE_TRADE_ROW + "\n").encode("utf-8-sig")
        with patch.object(serve, "PROJECT_ROOT", self.tmp.name), patch.object(
            serve, "UPLOAD_DIR", self.tmp.name
        ):
            resolved = serve._write_uploads(target_dir, [("History_for_Account.csv", body)], written)

        self.assertEqual(len(resolved), 1)
        self.assertTrue(os.path.isfile(resolved[0]))
        self.assertEqual(written, resolved)


if __name__ == "__main__":
    unittest.main()
