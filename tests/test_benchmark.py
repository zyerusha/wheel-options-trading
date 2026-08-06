"""Benchmark tests: cash-flow classification, XIRR, and the SPY replay."""

from __future__ import annotations

import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wheel.benchmark import (  # noqa: E402
    EXTERNAL_IN,
    EXTERNAL_OUT,
    INTERNAL,
    UNCLASSIFIED,
    CashFlowEvent,
    _xnpv,
    classify_cashflow,
    compare_to_benchmark,
    external_cashflows,
    simulate_benchmark,
    simulate_benchmark_series,
    xirr,
)
from wheel.parser import OTHER, Transaction


def tx(action_raw: str, amount: float, event_date: date, action: str = OTHER, row_id: int = 0) -> Transaction:
    """A minimal Transaction for cash-flow classification tests -- only the
    fields external_cashflows()/classify_cashflow() actually look at matter.
    """
    return Transaction(
        row_id=row_id,
        run_date=event_date,
        settlement_date=None,
        action=action,
        action_raw=action_raw,
        description="",
        underlying="",
        occ_symbol=None,
        right=None,
        strike=None,
        expiry=None,
        contracts=0.0,
        price=None,
        commission=0.0,
        fees=0.0,
        amount=amount,
        account_type="",
        as_of_date=None,
        source="test.csv",
    )


class TestClassifyCashflow(unittest.TestCase):
    CASES = [
        ("DIVIDEND RECEIVED MICRON TECHNOLOGY INC (MU) (Cash)", INTERNAL),
        ("FEE CHARGED UP FINTECH HOLDING LIMITED (Cash)", INTERNAL),
        ("FOREIGN TAX PAID PETROLEO BRASILEIRO SA (Cash)", INTERNAL),
        ("DISTRIBUTION NAME/SYMBOL CHANGE PUT (DCH) AMERICAN AXLE & FEB 20 26 $8", INTERNAL),
        ("IN LIEU OF FRX SHARE LEU PAYOUT #REORLM0051653590001 TEMPEST", INTERNAL),
        ("INTEREST PAID ON MARGIN BALANCE (Margin)", INTERNAL),
        ("DECREASE COLLATERAL MARK TO MARKET ADJ COLLATERAL DELV TO US BANK", INTERNAL),
        ("TRANSFER OF ASSETS CHECK RECEIVED INSPIRAFINANCI (Cash)", EXTERNAL_IN),
        ("ELECTRONIC FUNDS TRANSFER RECEIVED (Cash)", EXTERNAL_IN),
        ("CHECK RECEIVED (Cash)", EXTERNAL_IN),
        ("WIRE SENT (Cash)", EXTERNAL_OUT),
        ("SOME BRAND NEW LEDGER TEXT NEVER SEEN BEFORE", UNCLASSIFIED),
    ]

    def test_classification_table(self):
        for text, expected in self.CASES:
            with self.subTest(text=text):
                self.assertEqual(classify_cashflow(text), expected)


class TestExternalCashflows(unittest.TestCase):
    def test_only_external_rows_become_events(self):
        transactions = [
            tx("DIVIDEND RECEIVED MU (Cash)", 12.5, date(2026, 1, 1)),
            tx("TRANSFER OF ASSETS CHECK RECEIVED (Cash)", 1000.0, date(2026, 1, 5)),
            tx("SOLD OPENING MU CALL", -300.0, date(2026, 1, 6), action="STO"),
        ]
        events, warnings = external_cashflows(transactions)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].amount, 1000.0)
        self.assertEqual(events[0].kind, EXTERNAL_IN)
        self.assertEqual(warnings, [])

    def test_unclassified_row_excluded_and_warned_once(self):
        transactions = [
            tx("SOME NOVEL LEDGER TEXT", 5.0, date(2026, 1, 1)),
            tx("SOME NOVEL LEDGER TEXT", 6.0, date(2026, 1, 2)),
        ]
        events, warnings = external_cashflows(transactions)
        self.assertEqual(events, [])
        self.assertEqual(len(warnings), 1)


class TestXirr(unittest.TestCase):
    def test_known_answer_ten_percent(self):
        # 2023 is not a leap year, so this span is exactly 365 days.
        rate = xirr([(date(2023, 1, 1), -1000.0), (date(2024, 1, 1), 1100.0)])
        self.assertAlmostEqual(rate, 10.0, places=3)

    def test_multi_flow_result_zeroes_the_npv(self):
        flows = [
            (date(2023, 1, 1), -5000.0),
            (date(2023, 7, 1), -2000.0),
            (date(2024, 1, 1), 1000.0),
            (date(2025, 1, 1), 8000.0),
        ]
        rate = xirr(flows)
        self.assertIsNotNone(rate)
        npv = _xnpv(rate / 100.0, sorted(flows, key=lambda f: f[0]), flows[0][0])
        self.assertAlmostEqual(npv, 0.0, places=1)

    def test_degenerate_single_flow_is_none(self):
        self.assertIsNone(xirr([(date(2024, 1, 1), -100.0)]))

    def test_degenerate_all_same_sign_is_none(self):
        self.assertIsNone(xirr([(date(2024, 1, 1), 100.0), (date(2024, 6, 1), 50.0)]))

    def test_bisection_fallback_still_finds_a_root(self):
        # A large, lopsided series that stresses Newton's method's initial guess.
        flows = [
            (date(2020, 1, 1), -100000.0),
            (date(2020, 2, 1), -1.0),
            (date(2026, 1, 1), 250000.0),
        ]
        rate = xirr(flows)
        self.assertIsNotNone(rate)
        npv = _xnpv(rate / 100.0, sorted(flows, key=lambda f: f[0]), flows[0][0])
        self.assertLess(abs(npv), 1.0)


class _StaticPrice:
    def __init__(self, close: float):
        self.close = close


class TestSimulateBenchmark(unittest.TestCase):
    def test_single_contribution_grows_with_price(self):
        prices = {date(2024, 1, 1): _StaticPrice(100.0), date(2025, 1, 1): _StaticPrice(120.0)}
        events = [CashFlowEvent(date=date(2024, 1, 1), amount=1000.0, label="open", source="x", kind=EXTERNAL_IN)]
        series = simulate_benchmark_series(events, [date(2025, 1, 1)], prices.get)
        self.assertAlmostEqual(series[date(2025, 1, 1)], 1200.0)

    def test_withdrawal_reduces_shares(self):
        prices = {
            date(2024, 1, 1): _StaticPrice(100.0),
            date(2024, 6, 1): _StaticPrice(100.0),
            date(2025, 1, 1): _StaticPrice(100.0),
        }
        events = [
            CashFlowEvent(date=date(2024, 1, 1), amount=1000.0, label="in", source="x", kind=EXTERNAL_IN),
            CashFlowEvent(date=date(2024, 6, 1), amount=-400.0, label="out", source="x", kind=EXTERNAL_OUT),
        ]
        value = simulate_benchmark(events, date(2025, 1, 1), prices.get)
        self.assertAlmostEqual(value, 600.0)

    def test_valuation_date_before_price_series_is_omitted(self):
        prices = {date(2025, 1, 1): _StaticPrice(100.0)}
        events = [CashFlowEvent(date=date(2025, 1, 1), amount=1000.0, label="in", source="x", kind=EXTERNAL_IN)]
        series = simulate_benchmark_series(events, [date(2020, 1, 1)], prices.get)
        self.assertEqual(series, {})


class TestCompareToBenchmark(unittest.TestCase):
    def test_full_comparison(self):
        events = [CashFlowEvent(date=date(2024, 1, 1), amount=1000.0, label="open", source="x", kind=EXTERNAL_IN)]
        result = compare_to_benchmark(events, actual_terminal_value=1300.0, benchmark_terminal_value=1200.0, as_of=date(2025, 1, 1))
        self.assertEqual(result.value_added, 100.0)
        self.assertGreater(result.actual_xirr_pct, result.benchmark_xirr_pct)

    def test_missing_benchmark_terminal_value_leaves_benchmark_side_none(self):
        events = [CashFlowEvent(date=date(2024, 1, 1), amount=1000.0, label="open", source="x", kind=EXTERNAL_IN)]
        result = compare_to_benchmark(events, actual_terminal_value=1300.0, benchmark_terminal_value=None, as_of=date(2025, 1, 1))
        self.assertIsNone(result.benchmark_xirr_pct)
        self.assertIsNone(result.value_added)
        self.assertIsNotNone(result.actual_xirr_pct)


if __name__ == "__main__":
    unittest.main()
