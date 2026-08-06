"""Positions parser tests: row classification, as-of resolution, multi-account/file handling."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wheel.api import looks_like_export  # noqa: E402
from wheel.positions import (  # noqa: E402
    CASH,
    EQUITY,
    OPTION,
    UNKNOWN,
    PositionsFormatError,
    latest_snapshot_per_account,
    load_snapshots,
    looks_like_position_snapshot,
    parse_position_snapshot,
)

HEADER = (
    "Account number,Account name,Symbol,Description,Quantity,Last price,Last price change,"
    "Current value,Today's gain/loss dollar,Today's gain/loss percent,Total gain/loss dollar,"
    "Total gain/loss percent,Percent of account,Cost basis total,Average cost basis,Type"
)


def write_positions_csv(
    rows: list[str],
    footer_date: str | None = "Date downloaded Aug-03-2026 5:45 p.m ET",
    bom: bool = True,
    filename: str | None = None,
) -> str:
    """Materialize a Fidelity Positions-shaped CSV and return its path."""
    suffix = f"_{filename}.csv" if filename else ".csv"
    handle = tempfile.NamedTemporaryFile(
        "w", suffix=suffix, delete=False, encoding="utf-8-sig" if bom else "utf-8"
    )
    handle.write(HEADER + "\n")
    handle.write("\n".join(rows) + "\n")
    handle.write("\n")
    handle.write('"Brokerage services are provided by Fidelity Brokerage Services LLC (FBS)."\n')
    handle.write("\n")
    if footer_date:
        handle.write(f'"{footer_date}"\n')
    handle.close()
    return handle.name


ROW_SPAXX = '488896608,"Zafrir\'s IRA",SPAXX**,HELD IN MONEY MARKET,,,,$125170.01,,,,,15.16%,,,Cash,'
ROW_PENDING = '488896608,"Zafrir\'s IRA",Pending activity,,,,,$28860.01,,,,,,,,,,'
ROW_CROX = (
    '488896608,"Zafrir\'s IRA",CROX,CROCS INC,290,$134.89,+$6.88,$39118.10,+$1995.20,+5.37%,'
    "+$11448.10,+41.37%,4.74%,$27670.00,$95.41,Cash,"
)
ROW_CROX_CALL = (
    '488896608,"Zafrir\'s IRA", -CROX260821C150,CROX AUG 21 2026 $150 CALL,-2,$1.30,+$0.60,-$260.00,'
    "-$120.00,-85.72%,+$588.65,+69.36%,-0.03%,$848.65,$4.24,Cash,"
)
ROW_TGT_UNKNOWN_BASIS = (
    '488896608,"Zafrir\'s IRA",TGT,TARGET CORP,50,$149.35,+$4.86,$7467.50,--,--,--,--,0.90%,--,--,Margin,'
)
ROW_GILD_MARGIN = (
    '488896608,"Zafrir\'s IRA",GILD,GILEAD SCIENCES INC COM,100,$131.15,+$0.94,$13115.00,+$94.00,'
    "+0.72%,+$130.00,+1.00%,1.59%,$12985.00,$129.85,Margin,"
)
ROW_GILD_CASH = (
    '488896608,"Zafrir\'s IRA",GILD,GILEAD SCIENCES INC COM,50,$131.15,+$0.94,$6557.50,+$47.00,'
    "+0.72%,+$672.50,+11.42%,0.79%,$5885.00,$117.70,Cash,"
)


class TestDiscovery(unittest.TestCase):
    def test_positions_header_is_recognized(self):
        path = write_positions_csv([ROW_SPAXX])
        self.addCleanup(os.unlink, path)
        self.assertTrue(looks_like_position_snapshot(path))

    def test_transaction_history_header_is_not_a_positions_file(self):
        handle = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8")
        handle.write("Run Date,Action,Symbol,Description,Type,Quantity,Price ($)\n")
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        self.assertFalse(looks_like_position_snapshot(handle.name))

    def test_positions_file_is_not_a_transaction_export(self):
        path = write_positions_csv([ROW_SPAXX])
        self.addCleanup(os.unlink, path)
        self.assertFalse(looks_like_export(path))

    def test_missing_header_raises(self):
        handle = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8")
        handle.write("not,a,positions,file\n")
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        with self.assertRaises(PositionsFormatError):
            parse_position_snapshot(handle.name)


class TestRowClassification(unittest.TestCase):
    def setUp(self):
        path = write_positions_csv([ROW_SPAXX, ROW_PENDING, ROW_CROX, ROW_CROX_CALL, ROW_TGT_UNKNOWN_BASIS])
        self.addCleanup(os.unlink, path)
        self.snapshots, self.warnings = parse_position_snapshot(path)
        self.assertEqual(len(self.snapshots), 1)
        self.snapshot = self.snapshots[0]
        self.rows = {row.symbol.strip(): row for row in self.snapshot.rows}

    def test_cash_sweep_row(self):
        row = self.rows["SPAXX**"]
        self.assertEqual(row.kind, CASH)
        self.assertEqual(row.current_value, 125170.01)

    def test_pending_activity_row_is_cash(self):
        row = self.rows["Pending activity"]
        self.assertEqual(row.kind, CASH)
        self.assertEqual(row.current_value, 28860.01)

    def test_equity_row(self):
        row = self.rows["CROX"]
        self.assertEqual(row.kind, EQUITY)
        self.assertEqual(row.underlying, "CROX")
        self.assertEqual(row.quantity, 290)
        self.assertEqual(row.cost_basis_total, 27670.0)

    def test_leading_space_option_symbol_is_classified_and_stripped(self):
        row = self.rows["-CROX260821C150"]
        self.assertEqual(row.kind, OPTION)
        self.assertEqual(row.underlying, "CROX")
        self.assertEqual(row.right, "C")
        self.assertEqual(row.strike, 150.0)
        self.assertEqual(row.quantity, -2)
        self.assertEqual(row.occ_symbol, "CROX260821C150")

    def test_dollar_and_percent_and_dashdash_parsing(self):
        row = self.rows["CROX"]
        self.assertEqual(row.total_gain_dollar, 11448.10)
        self.assertAlmostEqual(row.percent_of_account, 4.74)
        tgt = self.rows["TGT"]
        self.assertIsNone(tgt.cost_basis_total)
        self.assertIsNone(tgt.today_gain_dollar)

    def test_unknown_cost_basis_row_counted_in_unknown_rows(self):
        self.assertEqual(self.snapshot.cost_basis_unknown_rows, 1)

    def test_option_value_is_negative_mark_to_market(self):
        self.assertEqual(self.snapshot.option_value, -260.0)

    def test_no_warnings_for_recognized_rows(self):
        self.assertEqual(self.warnings, [])


class TestDuplicateSymbolDifferentType(unittest.TestCase):
    def test_two_rows_both_counted(self):
        path = write_positions_csv([ROW_GILD_MARGIN, ROW_GILD_CASH])
        self.addCleanup(os.unlink, path)
        snapshots, _ = parse_position_snapshot(path)
        rows = snapshots[0].rows
        self.assertEqual(len(rows), 2)
        self.assertEqual({row.account_type for row in rows}, {"Margin", "Cash"})
        self.assertAlmostEqual(snapshots[0].equity_value, 13115.00 + 6557.50)


class TestUnknownRow(unittest.TestCase):
    def test_unclassifiable_row_is_kept_and_warned(self):
        # A blank symbol with a real quantity/price can't be swept into CASH
        # (something is clearly held) nor classified as EQUITY/OPTION (no symbol
        # to key off), so it must fall through to UNKNOWN rather than be dropped.
        weird = '488896608,"Zafrir\'s IRA",,ODD ROW NO SYMBOL,5,$10.00,+$0.10,$50.00,,,,,1.00%,$40.00,$8.00,Cash,'
        path = write_positions_csv([weird])
        self.addCleanup(os.unlink, path)
        snapshots, warnings = parse_position_snapshot(path)
        self.assertEqual(snapshots[0].rows[0].kind, UNKNOWN)
        self.assertEqual(len(warnings), 1)
        self.assertIn("could not classify", warnings[0])


class TestAsOfResolution(unittest.TestCase):
    def test_footer_as_of_wins(self):
        path = write_positions_csv([ROW_SPAXX], footer_date="Date downloaded Aug-03-2026 5:45 p.m ET")
        self.addCleanup(os.unlink, path)
        snapshots, _ = parse_position_snapshot(path)
        self.assertEqual(snapshots[0].as_of, datetime(2026, 8, 3, 17, 45))
        self.assertEqual(snapshots[0].as_of_source, "footer")

    def test_footer_am_time(self):
        path = write_positions_csv([ROW_SPAXX], footer_date="Date downloaded Jan-05-2026 9:05 a.m ET")
        self.addCleanup(os.unlink, path)
        snapshots, _ = parse_position_snapshot(path)
        self.assertEqual(snapshots[0].as_of, datetime(2026, 1, 5, 9, 5))

    def test_filename_fallback_when_footer_missing(self):
        path = write_positions_csv([ROW_SPAXX], footer_date=None, filename="Portfolio_Positions_Sep-01-2026")
        self.addCleanup(os.unlink, path)
        snapshots, _ = parse_position_snapshot(path)
        self.assertEqual(snapshots[0].as_of_source, "filename")
        self.assertEqual(snapshots[0].as_of.date().isoformat(), "2026-09-01")

    def test_no_as_of_anywhere_raises(self):
        path = write_positions_csv([ROW_SPAXX], footer_date=None, filename=None)
        # tempfile names don't carry a recognizable date, so this should fail.
        self.addCleanup(os.unlink, path)
        with self.assertRaises(PositionsFormatError):
            parse_position_snapshot(path)


class TestMultiAccountFile(unittest.TestCase):
    def test_two_accounts_in_one_file(self):
        other_account = ROW_CROX.replace("488896608", "999888777").replace("Zafrir's IRA", "Other Person")
        path = write_positions_csv([ROW_SPAXX, other_account])
        self.addCleanup(os.unlink, path)
        snapshots, _ = parse_position_snapshot(path)
        self.assertEqual(len(snapshots), 2)
        numbers = {snapshot.account_number for snapshot in snapshots}
        self.assertEqual(numbers, {"488896608", "999888777"})

    def test_letter_prefixed_account_numbers_are_not_dropped(self):
        # Fidelity IRAs, Joint accounts, Fidelity Go, and custodial/"Youth"
        # accounts commonly carry a one-letter prefix (e.g. Z05826863,
        # X43128422) rather than a pure digit string -- these must parse the
        # same as any other account, not be mistaken for footer boilerplate.
        ira = ROW_CROX.replace("488896608", "Z05826863").replace("Zafrir's IRA", "Fidelity Go account")
        joint = ROW_SPAXX.replace("488896608", "X43128422").replace("Zafrir's IRA", "S&Z Joint account")
        path = write_positions_csv([joint, ira])
        self.addCleanup(os.unlink, path)
        snapshots, warnings = parse_position_snapshot(path)
        self.assertEqual(warnings, [])
        numbers = {snapshot.account_number: snapshot.account_name for snapshot in snapshots}
        self.assertEqual(
            numbers, {"X43128422": "S&Z Joint account", "Z05826863": "Fidelity Go account"}
        )


class TestDisclaimerAndBlankLinesSkipped(unittest.TestCase):
    def test_footer_boilerplate_does_not_produce_rows(self):
        path = write_positions_csv([ROW_SPAXX, ROW_CROX])
        self.addCleanup(os.unlink, path)
        snapshots, warnings = parse_position_snapshot(path)
        self.assertEqual(len(snapshots[0].rows), 2)
        self.assertEqual(warnings, [])


class TestLoadSnapshots(unittest.TestCase):
    def test_sorted_by_account_then_as_of(self):
        path_a = write_positions_csv([ROW_SPAXX], footer_date="Date downloaded Jan-01-2026 9:00 a.m ET")
        path_b = write_positions_csv([ROW_SPAXX], footer_date="Date downloaded Feb-01-2026 9:00 a.m ET")
        self.addCleanup(os.unlink, path_a)
        self.addCleanup(os.unlink, path_b)
        snapshots, warnings = load_snapshots([path_b, path_a])
        self.assertEqual(warnings, [])
        self.assertEqual([s.as_of.date().isoformat() for s in snapshots], ["2026-01-01", "2026-02-01"])

    def test_duplicate_account_and_as_of_across_files_is_deduped_with_warning(self):
        path_a = write_positions_csv([ROW_SPAXX], footer_date="Date downloaded Jan-01-2026 9:00 a.m ET")
        path_b = write_positions_csv([ROW_SPAXX], footer_date="Date downloaded Jan-01-2026 9:00 a.m ET")
        self.addCleanup(os.unlink, path_a)
        self.addCleanup(os.unlink, path_b)
        snapshots, warnings = load_snapshots([path_a, path_b])
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(len(warnings), 1)
        self.assertIn("duplicate snapshot", warnings[0])

    def test_latest_snapshot_per_account(self):
        path_a = write_positions_csv([ROW_SPAXX], footer_date="Date downloaded Jan-01-2026 9:00 a.m ET")
        path_b = write_positions_csv([ROW_SPAXX], footer_date="Date downloaded Feb-01-2026 9:00 a.m ET")
        self.addCleanup(os.unlink, path_a)
        self.addCleanup(os.unlink, path_b)
        snapshots, _ = load_snapshots([path_a, path_b])
        latest = latest_snapshot_per_account(snapshots)
        self.assertEqual(latest["488896608"].as_of.date().isoformat(), "2026-02-01")


if __name__ == "__main__":
    unittest.main()
