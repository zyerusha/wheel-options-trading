"""Fidelity closed-lots parser: wheel/closed_lots.py."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wheel.closed_lots import (  # noqa: E402
    discover_closed_lots,
    looks_like_closed_lots,
    parse_closed_lots,
    realized_by_underlying,
    realized_totals,
)

HEADER = (
    "Symbol(CUSIP),Security description,Date acquired,Date sold,Quantity,"
    "Cost basis,Proceeds,Short-term gain/loss,Long-term gain/loss"
)


def _write(tmp, name, rows):
    path = os.path.join(tmp, name)
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        handle.write(HEADER + "\n")
        for row in rows:
            handle.write(row + "\n")
    return path


class TestParse(unittest.TestCase):
    def test_option_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(
                tmp,
                "Portfolio_Closed_Lots_Aug-21-2026.csv",
                ["AMZN260102P225(8265299PN),PUT (AMZN) AMAZON.COM INC JAN 02 26 $225 (100 SHS),2026-01-02,2025-12-26,3,$6.07,$106.01, --,$99.94,"],
            )
            report = parse_closed_lots(path)
        self.assertEqual(len(report.lots), 1)
        lot = report.lots[0]
        self.assertEqual(lot.symbol, "AMZN260102P225")
        self.assertEqual(lot.cusip, "8265299PN")
        self.assertTrue(lot.is_option)
        self.assertEqual(lot.underlying, "AMZN")
        self.assertEqual(lot.right, "P")
        self.assertEqual(lot.strike, 225.0)
        self.assertEqual(lot.date_acquired, date(2026, 1, 2))
        self.assertEqual(lot.date_sold, date(2025, 12, 26))  # STO precedes BTC -- expected
        self.assertEqual(lot.quantity, 3.0)
        self.assertEqual(lot.cost_basis, 6.07)
        self.assertEqual(lot.proceeds, 106.01)
        self.assertIsNone(lot.st_gain)
        self.assertEqual(lot.lt_gain, 99.94)
        self.assertEqual(lot.realized, 99.94)
        self.assertEqual(lot.term, "LONG")
        self.assertEqual(report.as_of, date(2026, 8, 21))  # from filename

    def test_equity_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(
                tmp,
                "closed.csv",
                ['MU,MICRON TECHNOLOGY INC,2026-02-01,2026-06-15,100,"$9,500.00","$10,250.00","$750.00", --,'],
            )
            lot = parse_closed_lots(path).lots[0]
        self.assertFalse(lot.is_option)
        self.assertEqual(lot.underlying, "MU")
        self.assertEqual(lot.cost_basis, 9500.0)
        self.assertEqual(lot.proceeds, 10250.0)
        self.assertEqual(lot.st_gain, 750.0)
        self.assertEqual(lot.realized, 750.0)
        self.assertEqual(lot.term, "SHORT")

    def test_money_parsing_quotes_commas_and_dashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(
                tmp,
                "c.csv",
                ['GLD260102C403(8348589BT),CALL (GLD) SPDR GOLD TR JAN 02 26 $403,2026-01-02,2025-12-26,2,$2.05,"$2,912.67", --,$2910.62,'],
            )
            lot = parse_closed_lots(path).lots[0]
        self.assertEqual(lot.proceeds, 2912.67)
        self.assertEqual(lot.cost_basis, 2.05)
        self.assertEqual(lot.lt_gain, 2910.62)

    def test_footer_prose_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(
                tmp,
                "c.csv",
                [
                    "WFC260213C98(8387549BB),CALL (WFC) WELLS FARGO,2026-02-05,2026-02-03,3,$45.07,$108.98, --,$63.91,",
                    'The information provided is general in nature and should not be considered tax advice. Please consult your tax advisor.',
                ],
            )
            report = parse_closed_lots(path)
        self.assertEqual(len(report.lots), 1)


class TestDiscovery(unittest.TestCase):
    def test_header_sniff(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = _write(tmp, "good.csv", [])
            bad = os.path.join(tmp, "bad.csv")
            with open(bad, "w") as handle:
                handle.write("Run Date,Action,Symbol\n")
            self.assertTrue(looks_like_closed_lots(good))
            self.assertFalse(looks_like_closed_lots(bad))
            self.assertEqual(discover_closed_lots([tmp]), [good])


class TestAggregation(unittest.TestCase):
    def _lots(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(
                tmp,
                "c.csv",
                [
                    "MU260102P90(1),PUT (MU) MICRON,2026-01-02,2025-12-20,1,$10.00,$110.00,$100.00, --,",
                    "MU260109P90(2),PUT (MU) MICRON,2026-01-09,2026-01-02,1,$5.00,$60.00,$55.00, --,",
                    'MU,MICRON,2026-01-02,2026-03-01,100,"$9,000.00","$9,400.00","$400.00", --,',
                ],
            )
            return parse_closed_lots(path).lots

    def test_realized_by_underlying_splits_option_vs_equity(self):
        by = realized_by_underlying(self._lots())
        self.assertAlmostEqual(by["MU"]["options"], 155.0, places=2)
        self.assertAlmostEqual(by["MU"]["equity"], 400.0, places=2)
        self.assertAlmostEqual(by["MU"]["realized"], 555.0, places=2)
        self.assertEqual(by["MU"]["lot_count"], 3)

    def test_realized_totals_st_lt_split_and_coverage(self):
        totals = realized_totals(self._lots())
        self.assertAlmostEqual(totals["st"], 555.0, places=2)
        self.assertEqual(totals["lt"], 0.0)
        self.assertAlmostEqual(totals["options_total"], 155.0, places=2)
        self.assertAlmostEqual(totals["equity_total"], 400.0, places=2)
        self.assertEqual(totals["coverage_start"], "2025-12-20")
        self.assertEqual(totals["coverage_end"], "2026-03-01")
        self.assertEqual(totals["lot_count"], 3)


class TestRealFile(unittest.TestCase):
    PATH = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data",
        "Portfolio_Closed_Lots_Aug-21-2026.csv",
    )

    @unittest.skipUnless(os.path.isfile(PATH), "real closed-lots export not on disk")
    def test_parses_without_warnings_and_every_lot_has_a_realized_figure(self):
        report = parse_closed_lots(self.PATH)
        self.assertGreater(len(report.lots), 100)
        self.assertEqual(report.warnings, [])
        self.assertTrue(all(lot.realized is not None for lot in report.lots))
        self.assertTrue(all(lot.underlying for lot in report.lots))


if __name__ == "__main__":
    unittest.main()
