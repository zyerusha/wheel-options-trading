"""Monthly cash-flow tests: bucketing, fee accounting, range summary,
Combined-view aggregation.
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_engine import tx  # noqa: E402
from wheel.accounts import _combine_cash_flow  # noqa: E402
from wheel.cashflow import (  # noqa: E402
    format_ascii_chart,
    format_monthly_table,
    format_report,
    month_average_collateral,
    monthly_cashflow_series,
    range_summary,
)
from wheel.parser import BTC, BTO, OTHER, STC, STO, Transaction  # noqa: E402


def dividend(day: str, amount: float, label: str = "DIVIDEND RECEIVED QQQ") -> Transaction:
    """An ``OTHER``-action ledger row, the shape a dividend/fee/tax row takes."""
    return Transaction(
        row_id=0,
        run_date=date.fromisoformat(day),
        settlement_date=None,
        action=OTHER,
        action_raw=label,
        description=label,
        underlying="QQQ",
        occ_symbol=None,
        right=None,
        strike=None,
        expiry=None,
        contracts=0.0,
        price=None,
        commission=0.0,
        fees=0.0,
        amount=amount,
        account_type="Margin",
        as_of_date=None,
    )


class TestOptionRowSplitting(unittest.TestCase):
    def test_sto_credit_recovers_fee_without_double_counting(self):
        """A $334.33 STO with $0.67 of commission+fees nets to a $335.00 gross
        premium: $335.00 credit, $0.67 fee, and net (credit - fee) must equal
        the broker's own Amount, or the fee has been subtracted twice.
        """
        rows = monthly_cashflow_series(
            [tx("2025-09-19", STO, "-MU250926P150", -1, 3.35, 334.33, commission=0.65, fees=0.02)],
            [],
            date(2025, 9, 19),
        )
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertAlmostEqual(row["gross_credits"], 335.00, places=2)
        self.assertAlmostEqual(row["gross_debits"], 0.0)
        self.assertAlmostEqual(row["fees"], 0.67, places=2)
        self.assertAlmostEqual(row["net_cash_flow"], 334.33, places=2)

    def test_btc_is_a_debit(self):
        rows = monthly_cashflow_series(
            [tx("2025-09-25", BTC, "-MU250926P150", 1, 0.95, -95.67, commission=0.65, fees=0.02)],
            [],
            date(2025, 9, 25),
        )
        row = rows[0]
        self.assertAlmostEqual(row["gross_credits"], 0.0)
        self.assertAlmostEqual(row["gross_debits"], 95.0, places=2)
        self.assertAlmostEqual(row["fees"], 0.67, places=2)
        self.assertAlmostEqual(row["net_cash_flow"], -95.67, places=2)

    def test_bto_protective_put_is_a_debit(self):
        rows = monthly_cashflow_series(
            [tx("2025-09-19", BTO, "-MU250926P140", 1, 1.20, -120.65, commission=0.65)],
            [],
            date(2025, 9, 19),
        )
        row = rows[0]
        self.assertAlmostEqual(row["gross_debits"], 120.0, places=2)
        self.assertAlmostEqual(row["fees"], 0.65, places=2)

    def test_stc_can_be_a_credit(self):
        """Closing a long option at a profit is cash in, even though STC is the
        same action a losing close would use -- the sign of the row decides,
        not the action verb.
        """
        rows = monthly_cashflow_series(
            [tx("2025-09-25", STC, "-MU250926P140", -1, 2.00, 199.35, commission=0.65)],
            [],
            date(2025, 9, 25),
        )
        row = rows[0]
        self.assertAlmostEqual(row["gross_credits"], 200.00, places=2)
        self.assertAlmostEqual(row["gross_debits"], 0.0)


class TestLedgerRowSplitting(unittest.TestCase):
    def test_dividend_is_a_credit(self):
        rows = monthly_cashflow_series([dividend("2025-12-31", 158.82)], [], date(2025, 12, 31))
        row = rows[0]
        self.assertAlmostEqual(row["gross_credits"], 158.82, places=2)
        self.assertAlmostEqual(row["fees"], 0.0)
        self.assertAlmostEqual(row["net_cash_flow"], 158.82, places=2)

    def test_fee_charged_lands_in_fees_not_gross_debits(self):
        rows = monthly_cashflow_series(
            [dividend("2025-12-30", -4.17, "FEE CHARGED PETROLEO BRASILEIRO SA")], [], date(2025, 12, 30)
        )
        row = rows[0]
        self.assertAlmostEqual(row["gross_debits"], 0.0)
        self.assertAlmostEqual(row["fees"], 4.17, places=2)
        self.assertAlmostEqual(row["net_cash_flow"], -4.17, places=2)

    def test_foreign_tax_and_margin_interest_are_fees(self):
        rows = monthly_cashflow_series(
            [
                dividend("2025-06-05", -1.50, "FOREIGN TAX PAID"),
                dividend("2025-06-06", -3.25, "MARGIN INTEREST PAID"),
            ],
            [],
            date(2025, 6, 6),
        )
        row = rows[0]
        self.assertAlmostEqual(row["fees"], 4.75, places=2)

    def test_collateral_marks_and_renames_are_excluded(self):
        rows = monthly_cashflow_series(
            [
                dividend("2025-06-05", 5000.0, "DECREASE COLLATERAL"),
                dividend("2025-06-05", 0.0, "DISTRIBUTION NAME/SYMBOL CHANGE"),
            ],
            [],
            date(2025, 6, 5),
        )
        self.assertEqual(rows, [])

    def test_unrecognized_ledger_text_is_excluded_not_guessed(self):
        rows = monthly_cashflow_series([dividend("2025-06-05", 250.0, "SOME UNKNOWN LEDGER EVENT")], [], date(2025, 6, 5))
        self.assertEqual(rows, [])


class TestCapitalAllocationsExcluded(unittest.TestCase):
    def test_share_assignment_and_call_away_are_not_cash_flow(self):
        from wheel.parser import BUY_STOCK, SELL_STOCK

        rows = monthly_cashflow_series(
            [
                tx("2025-06-05", BUY_STOCK, "MU", -100, 100.0, -10000.0),
                tx("2025-06-20", SELL_STOCK, "MU", 100, 105.0, 10500.0),
            ],
            [],
            date(2025, 6, 20),
        )
        self.assertEqual(rows, [])

    def test_assigned_and_expired_rows_carry_no_cash(self):
        rows = monthly_cashflow_series(
            [
                tx("2025-09-19", STO, "-MU250926P150", -1, 3.35, 334.33),
                tx("2025-09-26", "EXPIRED", "-MU250926P150", 1, None, 0.0, as_of="2025-09-26"),
            ],
            [],
            date(2025, 9, 26),
        )
        # Only the STO row contributes; EXPIRED adds nothing on top of it.
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["net_cash_flow"], 334.33, places=2)


class TestMonthlyBucketing(unittest.TestCase):
    def test_gap_months_are_filled_at_zero(self):
        rows = monthly_cashflow_series(
            [
                tx("2025-01-15", STO, "-MU250926P150", -1, 3.35, 335.0),
                tx("2025-03-15", STO, "-MU250926P150", -1, 3.35, 335.0),
            ],
            [],
            date(2025, 3, 31),
        )
        periods = [row["period"] for row in rows]
        self.assertEqual(periods, ["2025-01", "2025-02", "2025-03"])
        self.assertAlmostEqual(rows[1]["net_cash_flow"], 0.0)
        self.assertAlmostEqual(rows[1]["gross_credits"], 0.0)

    def test_never_extends_past_through(self):
        rows = monthly_cashflow_series(
            [tx("2025-01-15", STO, "-MU250926P150", -1, 3.35, 335.0)],
            [],
            date(2025, 1, 20),
        )
        self.assertEqual([row["period"] for row in rows], ["2025-01"])

    def test_since_crops_after_full_reconstruction(self):
        rows = monthly_cashflow_series(
            [
                tx("2025-01-15", STO, "-MU250926P150", -1, 3.35, 335.0),
                tx("2025-03-15", STO, "-MU250926P150", -1, 3.35, 335.0),
            ],
            [],
            date(2025, 3, 31),
            since=date(2025, 2, 1),
        )
        self.assertEqual([row["period"] for row in rows], ["2025-02", "2025-03"])

    def test_no_transactions_returns_empty(self):
        self.assertEqual(monthly_cashflow_series([], [], date(2025, 1, 1)), [])


class TestMonthlyYield(unittest.TestCase):
    def test_yield_uses_time_weighted_average_collateral(self):
        capital_points = [(date(2025, 6, d), 10000.0) for d in range(1, 16)] + [
            (date(2025, 6, d), 0.0) for d in range(16, 31)
        ]
        avg = month_average_collateral(capital_points, 2025, 6)
        self.assertAlmostEqual(avg, 10000.0)  # zero days excluded, not averaged in

        rows = monthly_cashflow_series(
            [tx("2025-06-05", STO, "-MU250926P150", -1, 3.35, 500.0)], capital_points, date(2025, 6, 30)
        )
        row = rows[0]
        self.assertAlmostEqual(row["avg_collateral"], 10000.0)
        self.assertAlmostEqual(row["monthly_yield_pct"], 5.0, places=2)

    def test_yield_is_none_with_no_collateral_committed(self):
        rows = monthly_cashflow_series(
            [dividend("2025-06-05", 100.0)], [], date(2025, 6, 30)
        )
        self.assertIsNone(rows[0]["monthly_yield_pct"])


class TestRangeSummary(unittest.TestCase):
    def _row(self, year: int, month: int, net: float) -> dict:
        return {
            "year": year,
            "month": month,
            "period": f"{year:04d}-{month:02d}",
            "gross_credits": max(net, 0.0),
            "gross_debits": max(-net, 0.0),
            "fees": 0.0,
            "net_cash_flow": net,
            "avg_collateral": None,
            "monthly_yield_pct": None,
        }

    def test_covers_every_row_passed_in_not_a_fixed_window(self):
        """No built-in lookback: give it 30 months and every one counts --
        range_summary has no window of its own, the caller's own date filter
        (already baked into `rows`) decides how many months are in scope.
        """
        rows = []
        year, month = 2023, 1
        for _ in range(30):
            rows.append(self._row(year, month, 100.0))
            month += 1
            if month > 12:
                month = 1
                year += 1
        result = range_summary(rows, [(date(2023, 1, 15), 10000.0)], date(2025, 6, 30))
        self.assertEqual(result["months_counted"], 30)
        self.assertAlmostEqual(result["cash_flow"], 3000.0, places=2)
        self.assertAlmostEqual(result["avg_monthly_income"], 100.0, places=2)

    def test_avg_collateral_is_one_time_weighted_average_not_average_of_monthly_averages(self):
        """The bug this replaced: averaging each month's own average gives a
        month engaged for 1 day the same weight as one engaged for 29 -- a
        true time-weighted average must not, and must instead match
        wheel.metrics' own denominator for Annualized Wheel ROC.
        """
        rows = [self._row(2024, 1, 0.0), self._row(2024, 2, 0.0)]
        capital_points = [(date(2024, 1, 1), 10000.0)] + [  # January: 1 engaged day at $10,000
            (date(2024, 2, d), 1000.0) for d in range(1, 30)  # February: 29 engaged days at $1,000
        ]
        result = range_summary(rows, capital_points, date(2024, 2, 29))
        # True time-weighted average = (10000*1 + 1000*29) / 30 = 1300.
        self.assertAlmostEqual(result["avg_collateral"], 1300.0, places=2)
        # NOT the average-of-monthly-averages the old trailing_metrics gave:
        # (10000 + 1000) / 2 = 5500.
        self.assertNotAlmostEqual(result["avg_collateral"], 5500.0, places=2)

    def test_zero_capital_days_are_excluded(self):
        capital_points = [(date(2024, 1, d), 10000.0) for d in range(1, 11)] + [
            (date(2024, 1, d), 0.0) for d in range(11, 32)
        ]
        result = range_summary([self._row(2024, 1, 0.0)], capital_points, date(2024, 1, 31))
        self.assertAlmostEqual(result["avg_collateral"], 10000.0, places=2)

    def test_annualized_return_formula(self):
        rows = [self._row(2024, m, 100.0) for m in range(1, 13)]
        result = range_summary(rows, [(date(2024, 1, 1), 10000.0)], date(2024, 12, 31))
        self.assertAlmostEqual(result["avg_monthly_income"], 100.0, places=2)
        # (100 * 12) / 10000 * 100 = 12%
        self.assertAlmostEqual(result["annualized_cash_on_cash_return_pct"], 12.0, places=2)

    def test_no_collateral_means_no_return_pct(self):
        result = range_summary([self._row(2024, 1, 500.0)], [], date(2024, 1, 31))
        self.assertIsNone(result["annualized_cash_on_cash_return_pct"])
        self.assertAlmostEqual(result["avg_monthly_income"], 500.0, places=2)

    def test_empty_rows(self):
        result = range_summary([], [], date(2025, 1, 1))
        self.assertEqual(result["months_counted"], 0)
        self.assertIsNone(result["avg_monthly_income"])
        self.assertIsNone(result["annualized_cash_on_cash_return_pct"])


class TestReportFormatting(unittest.TestCase):
    def test_formats_without_error_and_contains_key_figures(self):
        rows = monthly_cashflow_series(
            [tx("2025-06-05", STO, "-MU250926P150", -1, 3.35, 500.0)], [], date(2025, 6, 30)
        )
        summary = range_summary(rows, [], date(2025, 6, 30))
        report = format_report(rows, summary)
        self.assertIn("2025-06", report)
        self.assertIn("500.00", format_monthly_table(rows))
        self.assertIn("2025-06", format_ascii_chart(rows))

    def test_empty_formatters_do_not_crash(self):
        self.assertIn("no cash-flow activity", format_monthly_table([]))
        self.assertIn("no cash-flow activity", format_ascii_chart([]))


class TestCombineCashFlow(unittest.TestCase):
    """Pure-function test of the Combined-view aggregator: sums each account's
    monthly credits/debits/fees, then recomputes yield from the combined
    capital series -- never averages each account's own monthly yield %.
    """

    def _payload(self, months: list[dict], through: str) -> dict:
        return {"cash_flow": {"months": months}, "meta": {"through": through}}

    def test_sums_absolutes_and_recomputes_yield_from_combined_capital(self):
        payloads = {
            "a": self._payload(
                [
                    {
                        "year": 2025,
                        "month": 6,
                        "period": "2025-06",
                        "gross_credits": 300.0,
                        "gross_debits": 0.0,
                        "fees": 0.0,
                        "net_cash_flow": 300.0,
                        "avg_collateral": 10000.0,
                        "monthly_yield_pct": 3.0,
                    }
                ],
                "2025-06-30",
            ),
            "b": self._payload(
                [
                    {
                        "year": 2025,
                        "month": 6,
                        "period": "2025-06",
                        "gross_credits": 100.0,
                        "gross_debits": 0.0,
                        "fees": 0.0,
                        "net_cash_flow": 100.0,
                        "avg_collateral": 40000.0,
                        "monthly_yield_pct": 0.25,
                    }
                ],
                "2025-06-30",
            ),
        }
        # A combined capital series where every June day totals $50,000 (the
        # two accounts' $10,000 + $40,000 collateral, already summed).
        capital_series = [{"date": f"2025-06-{d:02d}", "total": 50000.0} for d in range(1, 31)]

        combined = _combine_cash_flow(payloads, capital_series)
        self.assertEqual(len(combined["months"]), 1)
        row = combined["months"][0]
        self.assertAlmostEqual(row["gross_credits"], 400.0, places=2)
        self.assertAlmostEqual(row["net_cash_flow"], 400.0, places=2)
        self.assertAlmostEqual(row["avg_collateral"], 50000.0, places=2)
        # 400 / 50000 * 100 = 0.8% -- not (3.0 + 0.25) / 2, which averaging
        # each account's own yield would wrongly produce.
        self.assertAlmostEqual(row["monthly_yield_pct"], 0.8, places=2)
        self.assertIn("trailing", combined)


if __name__ == "__main__":
    unittest.main()
