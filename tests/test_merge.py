"""Combining several exports into one seamless timeline."""

from __future__ import annotations

import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_engine import tx  # noqa: E402
from wheel.engine import build_cycles  # noqa: E402
from wheel.parser import (  # noqa: E402
    ASSIGNED,
    BTC,
    EXPIRED,
    STO,
    dedup_key,
    merge_transactions,
)


class TestDedupKey(unittest.TestCase):
    def test_identical_trades_share_a_key(self):
        a = tx("2025-09-19", STO, "-MU250926P150", -1, 3.35, 334.33, row_id=1)
        b = tx("2025-09-19", STO, "-MU250926P150", -1, 3.35, 334.33, row_id=99)
        self.assertEqual(dedup_key(a), dedup_key(b))

    def test_different_price_is_a_different_trade(self):
        a = tx("2025-10-08", STO, "-MU251010P182.5", -1, 0.70, 69.33, row_id=1)
        b = tx("2025-10-08", STO, "-MU251010P182.5", -1, 0.68, 67.33, row_id=2)
        self.assertNotEqual(dedup_key(a), dedup_key(b))

    def test_inconsistent_broker_spacing_still_matches(self):
        """One export writes 'FINL INC NOV', another 'FINL INCNOV'."""
        from dataclasses import replace

        base = tx("2025-10-17", STO, "-BHF251121C55", -2, 0.70, 138.66, row_id=1)
        spaced = replace(base, action_raw="YOU SOLD OPENING CALL (BHF) BRIGHTHOUSE FINL INC NOV 21 25 $55")
        tight = replace(base, action_raw="YOU SOLD OPENING CALL (BHF) BRIGHTHOUSE FINL INCNOV 21 25 $55")
        self.assertEqual(dedup_key(spaced), dedup_key(tight))

    def test_differing_as_of_date_format_still_matches(self):
        """One export writes 'as of Sep-17-2025', another 'as of 2025-09-17'."""
        from dataclasses import replace

        base = tx("2025-09-18", EXPIRED, "-QQQ250917P583", 1, None, 0.0, row_id=1, as_of="2025-09-17")
        long_form = replace(base, action_raw="EXPIRED PUT (QQQ) as of Sep-17-2025 PUT (QQQ)")
        iso_form = replace(base, action_raw="EXPIRED PUT (QQQ) as of 2025-09-17 PUT (QQQ)")
        self.assertEqual(dedup_key(long_form), dedup_key(iso_form))


class TestMerge(unittest.TestCase):
    def test_disjoint_exports_are_concatenated(self):
        first = [tx("2025-09-19", STO, "-MU250926P150", -1, 3.35, 334.33, row_id=1)]
        second = [tx("2026-01-05", STO, "-MU260116P200", -1, 4.00, 399.33, row_id=1)]
        merged, report = merge_transactions([("a.csv", first), ("b.csv", second)])

        self.assertEqual(len(merged), 2)
        self.assertEqual(report.duplicates_removed, 0)
        self.assertEqual(report.first_date, date(2025, 9, 19))
        self.assertEqual(report.last_date, date(2026, 1, 5))

    def test_a_trade_in_both_exports_is_counted_once(self):
        shared = lambda rid: tx("2025-09-19", STO, "-MU250926P150", -1, 3.35, 334.33, row_id=rid)
        merged, report = merge_transactions([("a.csv", [shared(1)]), ("b.csv", [shared(7)])])

        self.assertEqual(len(merged), 1)
        self.assertEqual(report.duplicates_removed, 1)
        self.assertEqual(report.rows_parsed, 2)

    def test_a_repeated_fill_within_one_file_is_preserved(self):
        """Selling the same contract twice at the same price is two real trades.

        A set-based dedup would collapse them; the merge takes the *maximum* count
        seen in any single file instead, so both survive even when a second export
        repeats the pair.
        """
        pair = lambda base: [
            tx("2025-11-14", STO, "-MU251121P240", -1, 7.48, 747.33, row_id=base),
            tx("2025-11-14", STO, "-MU251121P240", -1, 7.48, 747.33, row_id=base + 1),
        ]
        merged, report = merge_transactions([("a.csv", pair(1)), ("b.csv", pair(50))])

        self.assertEqual(len(merged), 2)
        self.assertEqual(report.duplicates_removed, 2)

    def test_a_file_with_more_copies_wins(self):
        one = [tx("2025-11-14", STO, "-MU251121P240", -1, 7.48, 747.33, row_id=1)]
        three = [
            tx("2025-11-14", STO, "-MU251121P240", -1, 7.48, 747.33, row_id=10 + i) for i in range(3)
        ]
        merged, _ = merge_transactions([("a.csv", one), ("b.csv", three)])
        self.assertEqual(len(merged), 3)

    def test_merge_is_order_independent(self):
        a = [
            tx("2025-09-19", STO, "-MU250926P150", -1, 3.35, 334.33, row_id=1),
            tx("2025-09-26", EXPIRED, "-MU250926P150", 1, None, 0.0, row_id=2, as_of="2025-09-26"),
        ]
        b = [
            tx("2025-09-19", STO, "-MU250926P150", -1, 3.35, 334.33, row_id=1),
            tx("2025-10-06", STO, "-MU251010P175", -1, 0.75, 74.33, row_id=2),
        ]
        forward, report_a = merge_transactions([("a.csv", a), ("b.csv", b)])
        backward, report_b = merge_transactions([("b.csv", b), ("a.csv", a)])

        self.assertEqual(report_a.rows_kept, report_b.rows_kept)
        self.assertEqual(
            [(t.event_date, t.occ_symbol, t.action) for t in forward],
            [(t.event_date, t.occ_symbol, t.action) for t in backward],
        )

    def test_row_ids_are_renumbered_chronologically(self):
        a = [tx("2026-01-05", STO, "-MU260116P200", -1, 4.00, 399.33, row_id=1)]
        b = [tx("2025-09-19", STO, "-MU250926P150", -1, 3.35, 334.33, row_id=1)]
        merged, _ = merge_transactions([("a.csv", a), ("b.csv", b)])

        self.assertEqual([t.row_id for t in merged], [0, 1])
        self.assertEqual(merged[0].event_date, date(2025, 9, 19))

    def test_report_attributes_rows_to_their_source(self):
        a = [tx("2025-09-19", STO, "-MU250926P150", -1, 3.35, 334.33, row_id=1)]
        b = [
            tx("2025-09-19", STO, "-MU250926P150", -1, 3.35, 334.33, row_id=1),
            tx("2025-10-06", STO, "-MU251010P175", -1, 0.75, 74.33, row_id=2),
        ]
        _, report = merge_transactions([("a.csv", a), ("b.csv", b)])

        by_name = {source["name"]: source for source in report.sources}
        self.assertEqual(by_name["a.csv"]["kept"] + by_name["b.csv"]["kept"], report.rows_kept)
        self.assertEqual(report.rows_parsed, 3)
        self.assertEqual(report.rows_kept, 2)
        self.assertTrue(report.combined)


class TestMergeFixesSplitHistory(unittest.TestCase):
    """The point of the feature: a position opened in one file, closed in another."""

    def setUp(self):
        self.year_one = [
            tx("2025-12-22", STO, "-MU260102P265", -1, 3.00, 299.33, row_id=1),
        ]
        self.year_two = [
            tx("2026-01-02", BTC, "-MU260102P265", 1, 0.05, -6.07, row_id=1),
        ]

    def test_the_later_file_alone_cannot_match_its_close(self):
        _, engine = build_cycles(self.year_two)
        self.assertEqual(len(engine.unmatched_closes), 1)
        self.assertIn("before this window", engine.unmatched_closes[0]["reason"])

    def test_combining_resolves_the_position(self):
        merged, report = merge_transactions(
            [("2025.csv", self.year_one), ("2026.csv", self.year_two)]
        )
        cycles, engine = build_cycles(merged)

        self.assertEqual(engine.unmatched_closes, [])
        self.assertEqual(report.duplicates_removed, 0)
        self.assertEqual(len(cycles), 1)

        leg = cycles[0].legs[0]
        self.assertFalse(leg.is_open)
        self.assertAlmostEqual(leg.realized_pl, 293.26, places=2)
        self.assertEqual(cycles[0].start_date, date(2025, 12, 22))
        self.assertEqual(cycles[0].end_date, date(2026, 1, 2))

    def test_overlapping_exports_do_not_double_the_position(self):
        """The overlap window must not open the same put twice."""
        overlap_a = self.year_one + [
            tx("2026-01-02", BTC, "-MU260102P265", 1, 0.05, -6.07, row_id=2)
        ]
        overlap_b = [
            tx("2025-12-22", STO, "-MU260102P265", -1, 3.00, 299.33, row_id=1),
            tx("2026-01-02", BTC, "-MU260102P265", 1, 0.05, -6.07, row_id=2),
            tx("2026-02-10", STO, "-MU260220P250", -1, 2.00, 199.33, row_id=3),
        ]
        merged, report = merge_transactions([("a.csv", overlap_a), ("b.csv", overlap_b)])
        cycles, engine = build_cycles(merged)

        self.assertEqual(report.duplicates_removed, 2)
        self.assertEqual(engine.unmatched_closes, [])
        legs = [leg for cycle in cycles for leg in cycle.legs]
        self.assertEqual(sum(1 for leg in legs if leg.occ_symbol == "MU260102P265"), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
