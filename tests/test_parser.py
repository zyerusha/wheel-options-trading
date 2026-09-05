"""Parser tests: column orientation, symbol grammar, dates, robustness."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wheel.parser import (  # noqa: E402
    ASSIGNED,
    BTC,
    BTO,
    BUY_STOCK,
    EXPIRED,
    SELL_STOCK,
    STC,
    STO,
    FidelityFormatError,
    classify_action,
    company_name_from_description,
    parse_fidelity_csv,
    parse_occ_symbol,
)

HEADER = (
    "Run Date,Action,Symbol,Description,Type,Quantity,Price ($),Commission ($),"
    "Fees ($),Accrued Interest ($),Amount ($),Cash Balance ($),Settlement Date"
)

# The newer dialect seen (so far) on non-retirement accounts: no " ($)" suffix
# on the dollar columns, a few extra FX columns this parser never reads, and
# Price ahead of Quantity rather than behind it.
MODERN_HEADER = (
    "Run Date,Action,Symbol,Description,Type,Exchange Quantity,Exchange Currency,"
    "Currency,Price,Quantity,Exchange Rate,Commission,Fees,Accrued Interest,"
    "Amount,Cash Balance,Settlement Date"
)


def write_csv(rows: list[str], bom: bool = True, blanks: int = 2, header: str = HEADER) -> str:
    """Materialize a Fidelity-shaped CSV and return its path."""
    handle = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8-sig" if bom else "utf-8")
    handle.write("\n" * blanks)
    handle.write(header + "\n")
    handle.write("\n".join(rows) + "\n")
    handle.close()
    return handle.name


class TestSymbolGrammar(unittest.TestCase):
    def test_plain_symbol(self):
        self.assertEqual(parse_occ_symbol("-MU251003P157.5"), ("MU", "P", 157.5, date(2025, 10, 3)))

    def test_leading_dash_optional(self):
        self.assertEqual(parse_occ_symbol("MU251003P157.5")[0], "MU")

    def test_ticker_containing_digits_is_not_split_early(self):
        """TQQQ must not be read as ticker 'T' with the rest as the date."""
        underlying, right, strike, expiry = parse_occ_symbol("-TQQQ251128C51.75")
        self.assertEqual(underlying, "TQQQ")
        self.assertEqual(right, "C")
        self.assertEqual(strike, 51.75)
        self.assertEqual(expiry, date(2025, 11, 28))

    def test_dotted_ticker(self):
        self.assertEqual(parse_occ_symbol("-BRK.B260116P400")[0], "BRK.B")

    def test_integer_strike(self):
        self.assertEqual(parse_occ_symbol("-QQQ250917C588")[2], 588.0)

    def test_equity_symbol_is_rejected(self):
        self.assertIsNone(parse_occ_symbol("MU"))

    def test_invalid_calendar_date_is_rejected(self):
        self.assertIsNone(parse_occ_symbol("-MU251345P100"))


class TestActionClassification(unittest.TestCase):
    def test_all_lifecycle_verbs(self):
        cases = {
            "YOU SOLD OPENING TRANSACTION PUT (MU) ...": STO,
            "YOU BOUGHT CLOSING TRANSACTION PUT (MU) ...": BTC,
            "YOU BOUGHT OPENING TRANSACTION CALL (MU) ...": BTO,
            "YOU SOLD CLOSING TRANSACTION CALL (MU) ...": STC,
            "ASSIGNED as of Nov-20-2025 PUT (MU) ...": ASSIGNED,
            "EXPIRED PUT (IVV) ... as of Nov-14-2025": EXPIRED,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(classify_action(text), expected)

    def test_opening_beats_bare_bought(self):
        """'YOU BOUGHT OPENING' must not fall through to the equity BUY rule."""
        self.assertEqual(classify_action("YOU BOUGHT OPENING TRANSACTION PUT (WFC) ..."), BTO)

    def test_assignment_share_legs_are_stock_trades_not_assignments(self):
        """The equity leg settling an assignment must not match the ASSIGNED rule.

        'YOU BOUGHT ASSIGNED PUTS' is the share purchase, not the option event;
        classifying it as ASSIGNED drops the shares and their cash entirely.
        """
        self.assertEqual(
            classify_action("YOU BOUGHT ASSIGNED PUTS AS OF 02-20-26 DAUCH CORPORATION (DCH)"),
            BUY_STOCK,
        )
        self.assertEqual(
            classify_action("YOU SOLD ASSIGNED CALLS AS OF 09-17-25 INVESCO QQQ TR UNIT (QQQ)"),
            SELL_STOCK,
        )

    def test_option_assignment_still_classifies_as_assigned(self):
        self.assertEqual(
            classify_action("ASSIGNED as of Feb-20-2026 PUT (DCH) AMERICAN AXLE & FEB 20 26 $8"),
            ASSIGNED,
        )


class TestCompanyName(unittest.TestCase):
    """Best-effort, cosmetic-only issuer name for the Trade Log summary."""

    def test_option_description(self):
        self.assertEqual(
            company_name_from_description("PUT (MU) MICRON TECHNOLOGY JAN 17 25 $100 (100 SHS)", "MU"),
            "Micron Technology",
        )

    def test_name_ending_in_digits_survives(self):
        self.assertEqual(
            company_name_from_description("CALL (IWM) ISHARES RUSSELL 2000JAN 09 26 $253 (100 SHS)", "IWM"),
            "Ishares Russell 2000",
        )

    def test_assignment_settlement_description(self):
        self.assertEqual(
            company_name_from_description("YOU BOUGHT ASSIGNED PUTS AS OF 02-20-26 MICRON TECHNOLOGY", "MU"),
            "Micron Technology",
        )

    def test_ticker_only_trailer_is_rejected(self):
        self.assertIsNone(company_name_from_description("(MU)", "MU"))

    def test_bare_ticker_is_rejected(self):
        self.assertIsNone(company_name_from_description("MU", "MU"))

    def test_empty_description_is_rejected(self):
        self.assertIsNone(company_name_from_description("", "MU"))

    def test_fragment_with_stray_dollar_is_rejected(self):
        self.assertIsNone(company_name_from_description("PUT (MU) $100", "MU"))


class TestColumnOrientation(unittest.TestCase):
    def test_detects_transposed_columns(self):
        """Fidelity's export puts price under 'Quantity' and vice versa."""
        path = write_csv(
            [
                '09/15/2025,"YOU SOLD OPENING TRANSACTION CALL (QQQ) ...",-QQQ250917C588,'
                '"CALL (QQQ) ...",Cash,3.32,-1,0.65,0.02,,331.33,60051.48,09/16/2025',
                '09/15/2025,"YOU SOLD OPENING TRANSACTION CALL (KEY) ...",-KEY251031C20,'
                '"CALL (KEY) ...",Cash,0.4,-2,1.3,0.05,,78.65,60130.13,09/16/2025',
            ]
        )
        transactions, report = parse_fidelity_csv(path)
        os.unlink(path)

        self.assertTrue(report.columns_swapped)
        self.assertEqual(transactions[0].contracts, -1)
        self.assertEqual(transactions[0].price, 3.32)
        self.assertEqual(transactions[1].contracts, -2)
        self.assertEqual(transactions[1].price, 0.40)

    def test_correctly_labelled_export_is_left_alone(self):
        """A future fixed export must parse without being 'un-swapped'."""
        path = write_csv(
            [
                '09/15/2025,"YOU SOLD OPENING TRANSACTION CALL (QQQ) ...",-QQQ250917C588,'
                '"CALL (QQQ) ...",Cash,-1,3.32,0.65,0.02,,331.33,60051.48,09/16/2025',
                '09/15/2025,"YOU SOLD OPENING TRANSACTION CALL (KEY) ...",-KEY251031C20,'
                '"CALL (KEY) ...",Cash,-2,0.4,1.3,0.05,,78.65,60130.13,09/16/2025',
            ]
        )
        transactions, report = parse_fidelity_csv(path)
        os.unlink(path)

        self.assertFalse(report.columns_swapped)
        self.assertEqual(transactions[0].contracts, -1)
        self.assertEqual(transactions[0].price, 3.32)


class TestModernDialect(unittest.TestCase):
    """Fidelity's newer, non-'($)'-suffixed export dialect (seen so far on
    non-retirement accounts) must reach the exact same parsing logic as the
    classic one -- see wheel.parser._resolve_columns. A file using it must
    never fall back to reading 'Price ($)'/'Amount ($)' (which don't exist in
    this dialect) and silently zeroing out every dollar amount.
    """

    def test_correctly_labelled_modern_export_parses(self):
        path = write_csv(
            [
                '08/05/2026,"YOU SOLD OPENING TRANSACTION CALL (QQQ) ...", -QQQ260904C745,'
                '"CALL (QQQ) ...",Cash,0,"",USD,9,-1,0,0.65,0.03,,899.32,Processing,08/06/2026',
            ],
            header=MODERN_HEADER,
        )
        transactions, report = parse_fidelity_csv(path)
        os.unlink(path)

        self.assertFalse(report.columns_swapped)
        self.assertEqual(len(transactions), 1)
        transaction = transactions[0]
        self.assertEqual(transaction.action, STO)
        self.assertEqual(transaction.contracts, -1)
        self.assertEqual(transaction.price, 9.0)
        self.assertEqual(transaction.amount, 899.32)
        self.assertEqual(report.reconciled, 1)

    def test_modern_export_transposed_columns_are_still_detected(self):
        """Same swap quirk, same detection mechanism, just the modern column names."""
        path = write_csv(
            [
                '09/15/2025,"YOU SOLD OPENING TRANSACTION CALL (QQQ) ...", -QQQ250917C588,'
                '"CALL (QQQ) ...",Cash,0,"",USD,-1,3.32,0,0.65,0.02,,331.33,60051.48,09/16/2025',
                '09/15/2025,"YOU SOLD OPENING TRANSACTION CALL (KEY) ...", -KEY251031C20,'
                '"CALL (KEY) ...",Cash,0,"",USD,-2,0.4,0,1.3,0.05,,78.65,60130.13,09/16/2025',
            ],
            header=MODERN_HEADER,
        )
        transactions, report = parse_fidelity_csv(path)
        os.unlink(path)

        self.assertTrue(report.columns_swapped)
        self.assertEqual(transactions[0].contracts, -1)
        self.assertEqual(transactions[0].price, 3.32)
        self.assertEqual(transactions[1].contracts, -2)
        self.assertEqual(transactions[1].price, 0.40)


class TestRobustness(unittest.TestCase):
    def test_bom_and_leading_blank_lines(self):
        path = write_csv(
            [
                '09/15/2025,"YOU SOLD OPENING TRANSACTION CALL (QQQ) ...",-QQQ250917C588,'
                '"CALL (QQQ) ...",Cash,3.32,-1,0.65,0.02,,331.33,60051.48,09/16/2025'
            ],
            bom=True,
            blanks=3,
        )
        transactions, report = parse_fidelity_csv(path)
        os.unlink(path)
        self.assertEqual(report.parsed, 1)
        self.assertEqual(transactions[0].underlying, "QQQ")

    def test_trailing_disclaimer_is_skipped(self):
        path = write_csv(
            [
                '09/15/2025,"YOU SOLD OPENING TRANSACTION CALL (QQQ) ...",-QQQ250917C588,'
                '"CALL (QQQ) ...",Cash,3.32,-1,0.65,0.02,,331.33,60051.48,09/16/2025',
                '"Brokerage services provided by Fidelity Brokerage Services LLC."',
                '"Date downloaded 12/01/2025"',
            ]
        )
        transactions, report = parse_fidelity_csv(path)
        os.unlink(path)
        self.assertEqual(len(transactions), 1)
        self.assertEqual(report.total_rows, 1)

    def test_processing_cash_balance_does_not_break_parse(self):
        path = write_csv(
            [
                '12/01/2025,"ASSIGNED as of Nov-28-2025 PUT (MU) ...",-MU251128P235,'
                '"PUT (MU) ...",Cash,,2,,,,0.00,Processing,'
            ]
        )
        transactions, _ = parse_fidelity_csv(path)
        os.unlink(path)
        self.assertEqual(transactions[0].action, ASSIGNED)
        self.assertEqual(transactions[0].contracts, 2)
        self.assertIsNone(transactions[0].price)

    def test_missing_header_raises(self):
        handle = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False)
        handle.write("nothing,useful,here\n1,2,3\n")
        handle.close()
        with self.assertRaises(FidelityFormatError):
            parse_fidelity_csv(handle.name)
        os.unlink(handle.name)


class TestPendingRepostDedup(unittest.TestCase):
    """Fidelity sometimes re-posts the identical trade a second time once its
    running Cash Balance settles: the first copy reads the literal string
    "Processing" in that column, the second is otherwise byte-for-byte
    identical with a real number there. Left alone that is a double-posted
    CSP/covered-call row -- the real-world case this covers is exactly two
    HPE CSP rows, $52 strike, -2 contracts, $218.67 premium, one "Processing"
    and one settled, that would otherwise show up as a duplicate open
    position in the dashboard.
    """

    PENDING = (
        '09/04/2025,"YOU SOLD OPENING TRANSACTION PUT (HPE) HEWLETT PACKARD SEP 11 25 $52 (100 SHS) (Cash)",'
        '-HPE250911P52,"PUT (HPE) HEWLETT PACKARD SEP 11 25 $52 (100 SHS)",Cash,-2,1.1,1.3,0.03,,218.67,'
        "Processing,09/08/2025"
    )
    SETTLED = (
        '09/04/2025,"YOU SOLD OPENING TRANSACTION PUT (HPE) HEWLETT PACKARD SEP 11 25 $52 (100 SHS) (Cash)",'
        '-HPE250911P52,"PUT (HPE) HEWLETT PACKARD SEP 11 25 $52 (100 SHS)",Cash,-2,1.1,1.3,0.03,,218.67,'
        "10999.94,09/08/2025"
    )

    def test_settled_repost_of_a_pending_row_is_dropped(self):
        path = write_csv([self.PENDING, self.SETTLED])
        transactions, report = parse_fidelity_csv(path)
        os.unlink(path)
        self.assertEqual(len(transactions), 1)
        self.assertAlmostEqual(transactions[0].amount, 218.67, places=2)
        self.assertTrue(any("re-posted" in warning for warning in report.warnings))

    def test_order_does_not_matter(self):
        """The settled copy can just as easily come first in the file."""
        path = write_csv([self.SETTLED, self.PENDING])
        transactions, _ = parse_fidelity_csv(path)
        os.unlink(path)
        self.assertEqual(len(transactions), 1)

    def test_pending_row_with_no_settled_twin_is_kept(self):
        """The ordinary case: a trade from the file's own last day or two,
        whose balance simply hasn't posted yet, has nothing to be a repost of
        and must not be dropped.
        """
        path = write_csv([self.PENDING])
        transactions, _ = parse_fidelity_csv(path)
        os.unlink(path)
        self.assertEqual(len(transactions), 1)

    def test_two_settled_identical_rows_are_both_kept(self):
        """A genuine repeated fill -- same price, same day, no "Processing"
        involved anywhere -- is two real fills, not a repost, and both survive
        (the same principle `merge_transactions` applies across files).
        """
        path = write_csv([self.SETTLED, self.SETTLED])
        transactions, _ = parse_fidelity_csv(path)
        os.unlink(path)
        self.assertEqual(len(transactions), 2)

    def test_modern_dialect_cash_balance_column_is_also_recognized(self):
        pending = (
            '09/04/2025,"YOU SOLD OPENING TRANSACTION PUT (HPE) HEWLETT PACKARD SEP 11 25 $52 (100 SHS) (Cash)",'
            '-HPE250911P52,"PUT (HPE) HEWLETT PACKARD SEP 11 25 $52 (100 SHS)",Cash,0,,USD,1.1,-2,0,1.3,0.03,,'
            "218.67,Processing,09/08/2025"
        )
        settled = (
            '09/04/2025,"YOU SOLD OPENING TRANSACTION PUT (HPE) HEWLETT PACKARD SEP 11 25 $52 (100 SHS) (Cash)",'
            '-HPE250911P52,"PUT (HPE) HEWLETT PACKARD SEP 11 25 $52 (100 SHS)",Cash,0,,USD,1.1,-2,0,1.3,0.03,,'
            "218.67,10999.94,09/08/2025"
        )
        path = write_csv([pending, settled], header=MODERN_HEADER)
        transactions, _ = parse_fidelity_csv(path)
        os.unlink(path)
        self.assertEqual(len(transactions), 1)


class TestEventDate(unittest.TestCase):
    def test_as_of_date_overrides_run_date(self):
        """Assignments post next business day but state the real date inline."""
        path = write_csv(
            [
                '11/21/2025,"ASSIGNED as of Nov-20-2025 PUT (MU) MICRON TECHNOLOGY NOV 21 25 $230 (100 SHS) (Cash)",'
                '-MU251121P230,"PUT (MU) ...",Cash,,1,,,,0.00,230482.91,'
            ]
        )
        transactions, _ = parse_fidelity_csv(path)
        os.unlink(path)
        self.assertEqual(transactions[0].run_date, date(2025, 11, 21))
        self.assertEqual(transactions[0].as_of_date, date(2025, 11, 20))
        self.assertEqual(transactions[0].event_date, date(2025, 11, 20))

    def test_numeric_as_of_date_on_share_settlement_legs(self):
        """Equity settlement rows write 'AS OF 02-20-26', not 'as of Feb-20-2026'."""
        path = write_csv(
            [
                '02/23/2026,"YOU BOUGHT ASSIGNED PUTS AS OF 02-20-26 DAUCH CORPORATION COMMON STOCK (DCH)",'
                'DCH,"DAUCH CORPORATION COMMON STOCK",Cash,1000,8,,,,-8000,192000.00,02/24/2026'
            ]
        )
        transactions, _ = parse_fidelity_csv(path)
        os.unlink(path)
        transaction = transactions[0]
        self.assertEqual(transaction.action, BUY_STOCK)
        self.assertEqual(transaction.contracts, 1000)
        self.assertTrue(transaction.assignment_settlement)
        self.assertEqual(transaction.as_of_date, date(2026, 2, 20))
        self.assertEqual(transaction.event_date, date(2026, 2, 20))

    def test_plain_trade_uses_run_date(self):
        path = write_csv(
            [
                '09/15/2025,"YOU SOLD OPENING TRANSACTION CALL (QQQ) ...",-QQQ250917C588,'
                '"CALL (QQQ) ...",Cash,3.32,-1,0.65,0.02,,331.33,60051.48,09/16/2025'
            ]
        )
        transactions, _ = parse_fidelity_csv(path)
        os.unlink(path)
        self.assertEqual(transactions[0].event_date, date(2025, 9, 15))


class TestReconciliation(unittest.TestCase):
    def test_amount_reconciles_and_is_reported(self):
        path = write_csv(
            [
                '09/15/2025,"YOU SOLD OPENING TRANSACTION CALL (KEY) ...",-KEY251031C20,'
                '"CALL (KEY) ...",Cash,0.4,-2,1.3,0.05,,78.65,60130.13,09/16/2025'
            ]
        )
        _, report = parse_fidelity_csv(path)
        os.unlink(path)
        self.assertEqual(report.reconciled, 1)
        self.assertEqual(report.reconcile_failures, [])

    def test_equity_price_rounding_is_tolerated(self):
        """Fidelity prints a rounded average price beside an exact total.

        6,000 shares at a true 3.1265 display as 3.13, so price x quantity misses
        the real amount by $21. That is display rounding, not a broken row.
        """
        path = write_csv(
            [
                '10/17/2025,"YOU SOLD NUVEEN REAL ASSET (NRO) (Cash)",NRO,'
                '"NUVEEN REAL ASSET",Cash,-6000,3.13,,,,18759,100000.00,10/20/2025'
            ]
        )
        transactions, report = parse_fidelity_csv(path)
        os.unlink(path)
        self.assertEqual(transactions[0].contracts, -6000)
        self.assertEqual(transactions[0].price, 3.13)
        self.assertEqual(report.reconcile_failures, [])
        self.assertEqual(report.reconciled, 1)

    def test_equity_error_beyond_rounding_is_still_flagged(self):
        path = write_csv(
            [
                '10/17/2025,"YOU SOLD NUVEEN REAL ASSET (NRO) (Cash)",NRO,'
                '"NUVEEN REAL ASSET",Cash,-6000,3.13,,,,15000,100000.00,10/20/2025'
            ]
        )
        _, report = parse_fidelity_csv(path)
        os.unlink(path)
        self.assertEqual(len(report.reconcile_failures), 1)

    def test_option_rows_get_no_rounding_slack(self):
        path = write_csv(
            [
                '09/15/2025,"YOU SOLD OPENING TRANSACTION CALL (KEY) ...",-KEY251031C20,'
                '"CALL (KEY) ...",Cash,0.4,-2,1.3,0.05,,79.65,60130.13,09/16/2025'
            ]
        )
        _, report = parse_fidelity_csv(path)
        os.unlink(path)
        self.assertEqual(len(report.reconcile_failures), 1)

    def test_bad_amount_is_flagged_not_swallowed(self):
        path = write_csv(
            [
                '09/15/2025,"YOU SOLD OPENING TRANSACTION CALL (KEY) ...",-KEY251031C20,'
                '"CALL (KEY) ...",Cash,0.4,-2,1.3,0.05,,999.99,60130.13,09/16/2025'
            ]
        )
        _, report = parse_fidelity_csv(path)
        os.unlink(path)
        self.assertEqual(len(report.reconcile_failures), 1)
        self.assertAlmostEqual(report.reconcile_failures[0]["expected"], 78.65, places=2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
