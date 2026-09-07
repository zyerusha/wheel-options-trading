"""CSV export: wheel/exporter.py."""

from __future__ import annotations

import csv
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wheel.exporter import (  # noqa: E402
    CYCLE_COLUMNS,
    TRADE_LOG_COLUMNS,
    flatten_trade_log,
    rows_to_csv,
)

COLS = [("a", "Alpha"), ("b", "Beta"), ("c", "Gamma")]


def _parse(text):
    return list(csv.reader(io.StringIO(text)))


class TestRowsToCsv(unittest.TestCase):
    def test_header_row_matches_columns(self):
        rows = _parse(rows_to_csv([], COLS))
        self.assertEqual(rows, [["Alpha", "Beta", "Gamma"]])

    def test_row_count_matches_input(self):
        data = [{"a": 1, "b": 2, "c": 3}, {"a": 4, "b": 5, "c": 6}]
        rows = _parse(rows_to_csv(data, COLS))
        self.assertEqual(len(rows), 3)  # header + 2

    def test_numbers_go_out_unformatted(self):
        rows = _parse(rows_to_csv([{"a": 1234.5, "b": -0.25, "c": 1000000}], COLS))
        self.assertEqual(rows[1], ["1234.5", "-0.25", "1000000"])

    def test_none_becomes_empty_cell_missing_key_too(self):
        rows = _parse(rows_to_csv([{"a": None}], COLS))
        self.assertEqual(rows[1], ["", "", ""])

    def test_bools_become_words(self):
        rows = _parse(rows_to_csv([{"a": True, "b": False, "c": None}], COLS))
        self.assertEqual(rows[1][:2], ["true", "false"])

    def test_empty_input_is_header_only(self):
        self.assertEqual(rows_to_csv([], CYCLE_COLUMNS).strip().count("\n"), 0)


class TestFlattenTradeLog(unittest.TestCase):
    WHEELS = [
        {"cycle_id": "MU-1", "underlying": "MU", "transactions": [
            {"type": "STO", "date": "2026-01-02", "net_cash_flow": 150.0},
            {"type": "BTC", "date": "2026-01-20", "net_cash_flow": -40.0},
        ]},
        {"cycle_id": "WFC-1", "underlying": "WFC", "transactions": [
            {"type": "STO", "date": "2026-02-01", "net_cash_flow": 90.0},
        ]},
    ]

    def test_one_row_per_ledger_line_with_wheel_context(self):
        out = flatten_trade_log(self.WHEELS)
        self.assertEqual(len(out), 3)
        self.assertEqual(out[0]["wheel_id"], "MU-1")
        self.assertEqual(out[0]["underlying"], "MU")
        self.assertEqual(out[2]["wheel_id"], "WFC-1")

    def test_only_wheel_filter(self):
        out = flatten_trade_log(self.WHEELS, only_wheel="WFC-1")
        self.assertEqual([r["wheel_id"] for r in out], ["WFC-1"])

    def test_csv_of_flattened_log_has_expected_header(self):
        text = rows_to_csv(flatten_trade_log(self.WHEELS), TRADE_LOG_COLUMNS)
        header = _parse(text)[0]
        self.assertEqual(header[0], "Wheel")
        self.assertIn("Net cash flow", header)


if __name__ == "__main__":
    unittest.main()
