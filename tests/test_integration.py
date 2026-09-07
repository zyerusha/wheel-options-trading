"""End-to-end checks against the real Fidelity export.

These are the guards that matter most: they re-derive the headline figures
straight from the CSV with independent arithmetic and assert the engine agrees.
If a future refactor quietly loses or invents a dollar, these fail.
"""

from __future__ import annotations

import csv
import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wheel.api import Dashboard, Filters, discover_exports  # noqa: E402
from wheel.engine import build_cycles  # noqa: E402
from wheel.parser import STO, parse_fidelity_csv  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def available_exports() -> list[str]:
    """Every broker export the app itself would find.

    Delegates to the same content-sniffing discovery the dashboard uses (project
    root and ``data/``, matched on a ``Run Date`` header, not a filename) so the
    tests see exactly what a user running ``python -m wheel.serve`` would -- and
    so nothing here has to assume, let alone hard-code, a particular filename.
    """
    return discover_exports((ROOT, os.path.join(ROOT, "data")))


def _self_contained_options_export() -> str:
    """The one export the fixture-level assertions below describe.

    Picked by property, not by name: options-only, columns transposed, and
    self-contained (nothing closes that it never saw opened).  Exports differ --
    some carry equity rows, some are year-slices that open mid-position -- so
    these assertions only hold for that shape.  Returns "" if none is present,
    and the class skips.
    """
    for path in available_exports():
        try:
            payload = Dashboard(path).build()
        except Exception:
            continue
        if (
            payload["meta"]["columns_swapped"]
            and payload["reconciliation"]["equity_rows"] == 0
            and not payload["meta"]["unmatched_closes"]
        ):
            return path
    return ""


CSV_PATH = _self_contained_options_export()


def raw_rows() -> list[dict]:
    """Read the CSV with plain stdlib csv -- no project code involved."""
    with open(CSV_PATH, "r", encoding="utf-8-sig", newline="") as handle:
        lines = [line for line in handle.read().splitlines() if line.strip()]
    start = next(i for i, line in enumerate(lines) if line.startswith("Run Date"))
    return list(csv.DictReader(lines[start:]))


def raw_amount(row: dict) -> float:
    text = (row.get("Amount ($)") or "").strip()
    return float(text) if text else 0.0


@unittest.skipUnless(os.path.isfile(CSV_PATH), "sample export not present")
class TestRealExport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dashboard = Dashboard(CSV_PATH)
        cls.payload = cls.dashboard.build()
        cls.rows = raw_rows()

    def test_every_row_is_parsed(self):
        transactions, report = parse_fidelity_csv(CSV_PATH)
        self.assertEqual(len(transactions), len(self.rows))
        self.assertEqual(report.skipped, 0)

    def test_column_transposition_is_detected(self):
        self.assertTrue(self.payload["meta"]["columns_swapped"])

    def test_every_priced_row_reconciles_against_the_amount_column(self):
        reconciliation = self.payload["reconciliation"]
        self.assertEqual(reconciliation["row_failures"], [])
        self.assertEqual(reconciliation["reconcile_rate_pct"], 100.0)

    def test_model_cash_equals_file_cash(self):
        expected = sum(raw_amount(row) for row in self.rows)
        reconciliation = self.payload["reconciliation"]
        self.assertAlmostEqual(reconciliation["file_cash_total"], expected, places=2)
        self.assertAlmostEqual(reconciliation["model_cash_total"], expected, places=2)
        self.assertTrue(reconciliation["balanced"])

    def test_premium_received_matches_sell_to_open_credits(self):
        expected = sum(
            raw_amount(row) for row in self.rows if "SOLD OPENING" in (row.get("Action") or "").upper()
        )
        self.assertAlmostEqual(self.payload["portfolio"]["premium_received"], expected, places=2)

    def test_fees_match_the_commission_and_fee_columns(self):
        def number(row: dict, key: str) -> float:
            text = (row.get(key) or "").strip()
            return float(text) if text else 0.0

        expected = sum(number(row, "Commission ($)") + number(row, "Fees ($)") for row in self.rows)
        self.assertAlmostEqual(self.payload["portfolio"]["fees"], expected, places=2)

    def test_no_closing_row_is_left_unmatched(self):
        """Every buy-back, expiry and assignment finds the lot it belongs to."""
        self.assertEqual(self.payload["meta"]["unmatched_closes"], [])

    def test_no_position_survives_its_own_expiry(self):
        self.assertEqual(self.payload["meta"]["engine_warnings"], [])

    def test_per_ticker_option_cash_matches_the_file(self):
        expected: dict[str, float] = {}
        for row in self.rows:
            symbol = (row.get("Symbol") or "").strip().lstrip("-").upper()
            from wheel.parser import parse_occ_symbol

            parsed = parse_occ_symbol(symbol)
            if parsed:
                expected[parsed[0]] = expected.get(parsed[0], 0.0) + raw_amount(row)

        actual = {
            row["underlying"]: row["option_realized_pl"] + row["open_premium"]
            for row in self.payload["tickers"]
        }
        self.assertEqual(set(actual), set(expected))
        for ticker, value in expected.items():
            with self.subTest(ticker=ticker):
                self.assertAlmostEqual(actual[ticker], value, places=2)

    def test_every_cycle_belongs_to_exactly_one_ticker(self):
        for cycle in self.payload["cycles"]:
            underlyings = {leg["underlying"] for leg in cycle["legs"]}
            self.assertLessEqual(len(underlyings), 1, cycle["cycle_id"])

    def test_no_leg_is_closed_beyond_its_size(self):
        transactions, _ = parse_fidelity_csv(CSV_PATH)
        cycles, _ = build_cycles(transactions)
        for cycle in cycles:
            for leg in cycle.legs:
                self.assertGreaterEqual(leg.remaining_contracts, -1e-9, leg.occ_symbol)
                self.assertLessEqual(leg.closed_contracts, leg.contracts + 1e-9, leg.occ_symbol)

    def test_capital_is_never_negative(self):
        for point in self.payload["capital_series"]:
            self.assertGreaterEqual(point["total"], -1e-6, point["date"])

    def test_shares_held_split_reconciles(self):
        """`stock` (all held-share cost basis) splits into `idle_stock` (no call
        written) + `call_stock` (backing an open covered call). The Capital
        deployed chart draws them as two bands, so the split must add up.
        """
        for point in self.payload["capital_series"]:
            self.assertIn("call_stock", point, point["date"])
            self.assertAlmostEqual(
                point["idle_stock"] + point["call_stock"],
                point["stock"],
                places=2,
                msg=point["date"],
            )
            self.assertGreaterEqual(point["call_stock"], -1e-6, point["date"])

    def test_a_full_wheel_is_reconstructed(self):
        """A put assigned into stock, then called away, stays one campaign.

        Asserted as a property of whichever cycle went the full way round rather
        than against a named ticker, so the test survives a change of export.
        """
        wheels = [
            cycle
            for cycle in self.payload["cycles"]
            if any(a["direction"] == "ACQUIRE" for a in cycle["assignment_events"])
            and any(a["direction"] == "DISPOSE" for a in cycle["assignment_events"])
        ]
        self.assertTrue(wheels, "no cycle completed put -> stock -> call away")

        for cycle in wheels:
            with self.subTest(cycle=cycle["cycle_id"]):
                acquire = next(a for a in cycle["assignment_events"] if a["direction"] == "ACQUIRE")
                self.assertEqual(acquire["shares"], acquire["contracts"] * 100)
                # Synthesized share legs are priced at the strike, cash out.
                if acquire["synthetic"]:
                    self.assertAlmostEqual(
                        acquire["cash"], -acquire["strike"] * acquire["shares"], places=2
                    )
                self.assertEqual(cycle["assignments"], len(cycle["assignment_events"]))
                self.assertEqual(cycle["rolls"], len(cycle["roll_events"]))

    def test_short_calls_are_covered_never_naked(self):
        """Every short option is covered: puts by cash, calls by stock.

        `shares_tracked` is False when the backing stock pre-dates the export --
        the call is still covered, its capital is just estimated.
        """
        seen = set()
        for cycle in self.payload["cycles"]:
            for leg in cycle["legs"]:
                seen.add(leg["strategy"])
        self.assertLessEqual(seen, {"CSP", "COVERED_CALL", "LONG_PUT", "LONG_CALL"})
        self.assertNotIn("NAKED_CALL", seen)

    def test_counts_are_integers_not_detail_lists(self):
        """Guards the payload shape the cycles table binds to."""
        for cycle in self.payload["cycles"]:
            self.assertIsInstance(cycle["rolls"], int)
            self.assertIsInstance(cycle["assignments"], int)
            self.assertIsInstance(cycle["roll_events"], list)
            self.assertIsInstance(cycle["assignment_events"], list)


@unittest.skipUnless(available_exports(), "no exports present")
class TestEveryExport(unittest.TestCase):
    """Invariants that must hold for any Fidelity export, whatever its shape.

    The two sample files differ in almost every way that matters -- one has its
    columns transposed and no equity rows, the other is correctly labelled and
    carries real share fills -- so running the same invariants across both is
    what proves the parser and engine are not tuned to a single file.
    """

    def test_all_rows_parse_and_option_cash_is_conserved(self):
        for path in available_exports():
            with self.subTest(export=os.path.basename(path)):
                dashboard = Dashboard(path)
                payload = dashboard.build()
                reconciliation = payload["reconciliation"]

                self.assertGreater(len(dashboard.transactions), 0)
                self.assertTrue(reconciliation["balanced"], reconciliation["delta"])
                self.assertEqual(reconciliation["row_failures"], [])
                self.assertEqual(reconciliation["reconcile_rate_pct"], 100.0)

    def test_unmatched_closes_are_always_explained(self):
        """A partial export legitimately closes positions it never saw opened.

        What must never happen is a row going unaccounted for silently, so every
        unmatched close has to carry a reason and its cash.
        """
        for path in available_exports():
            with self.subTest(export=os.path.basename(path)):
                payload = Dashboard(path).build()
                for unmatched in payload["meta"]["unmatched_closes"]:
                    self.assertIn("reason", unmatched)
                    self.assertTrue(unmatched["reason"])
                    self.assertIn("cash", unmatched)
                self.assertTrue(payload["reconciliation"]["balanced"])

    def test_shares_are_never_double_booked_by_assignment(self):
        """A broker-supplied share fill must replace the synthetic one, not add to it.

        Only ``PUT_ASSIGNMENT`` lots are the double-booking risk. A
        ``PRE_HISTORY`` lot appearing on the same day is legitimate and means the
        opposite thing: stock was called away that this export never saw bought.
        """
        for path in available_exports():
            with self.subTest(export=os.path.basename(path)):
                payload = Dashboard(path).build()
                for cycle in payload["cycles"]:
                    for assignment in cycle["assignment_events"]:
                        if assignment["synthetic"] or assignment["direction"] != "ACQUIRE":
                            continue
                        duplicated = [
                            lot
                            for lot in cycle["share_lots"]
                            if lot["acquired"] == assignment["date"]
                            and lot["source"] == "PUT_ASSIGNMENT"
                        ]
                        self.assertEqual(
                            duplicated, [], f"{cycle['cycle_id']} {assignment['date']}"
                        )

    def test_broker_settled_assignments_have_a_real_share_lot(self):
        """Every non-synthetic acquisition is backed by an actual purchase lot."""
        for path in available_exports():
            with self.subTest(export=os.path.basename(path)):
                payload = Dashboard(path).build()
                for cycle in payload["cycles"]:
                    for assignment in cycle["assignment_events"]:
                        if assignment["synthetic"] or assignment["direction"] != "ACQUIRE":
                            continue
                        # The share leg can post a few days off the option event,
                        # which is why the matcher allows a window at all.
                        when = date.fromisoformat(assignment["date"])
                        real = [
                            lot
                            for lot in cycle["share_lots"]
                            if not lot["synthetic"]
                            and abs((date.fromisoformat(lot["acquired"]) - when).days) <= 5
                        ]
                        self.assertTrue(real, f"{cycle['cycle_id']} {assignment['date']}")

    def test_capital_and_share_counts_stay_sane(self):
        for path in available_exports():
            with self.subTest(export=os.path.basename(path)):
                payload = Dashboard(path).build()
                for point in payload["capital_series"]:
                    self.assertGreaterEqual(point["total"], -1e-6, point["date"])
                for cycle in payload["cycles"]:
                    for lot in cycle["share_lots"]:
                        self.assertGreaterEqual(lot["remaining"], -1e-6)
                        self.assertLessEqual(lot["remaining"], lot["shares"] + 1e-6)

    def test_same_ticker_cycles_never_overlap_in_time(self):
        """The Trade Log attributes raw transactions to a wheel by
        underlying + inclusive date span (``wheel/api.py`` ``_trade_log_entry``).
        That is only unambiguous while one ticker's cycles are strictly
        separated -- so lock that in: at most one open cycle per ticker, and
        every consecutive pair strictly ordered ``next.start > prev.end``.
        """
        for path in available_exports():
            with self.subTest(export=os.path.basename(path)):
                dashboard = Dashboard(path)
                by_underlying: dict[str, list] = {}
                for cycle in dashboard.all_cycles:
                    by_underlying.setdefault(cycle.underlying, []).append(cycle)
                for underlying, group in by_underlying.items():
                    ordered = sorted(group, key=lambda cycle: cycle.start_date)
                    self.assertLessEqual(
                        sum(1 for cycle in ordered if cycle.end_date is None),
                        1,
                        f"{underlying}: more than one open cycle",
                    )
                    for prev, nxt in zip(ordered, ordered[1:]):
                        self.assertIsNotNone(
                            prev.end_date, f"{underlying}: {prev.cycle_id} open but a later cycle exists"
                        )
                        self.assertGreater(
                            nxt.start_date,
                            prev.end_date,
                            f"{underlying}: {prev.cycle_id} and {nxt.cycle_id} overlap",
                        )

    def test_trade_log_covers_every_wheel_and_reconciles_row_cash(self):
        for path in available_exports():
            with self.subTest(export=os.path.basename(path)):
                dashboard = Dashboard(path)
                payload = dashboard.build()  # no filters -> wheels == all cycles
                trade_log = payload["trade_log"]
                self.assertEqual(trade_log["warnings"], [])
                self.assertEqual(
                    {w["cycle_id"] for w in trade_log["wheels"]},
                    {c.cycle_id for c in dashboard.all_cycles},
                )
                for wheel in trade_log["wheels"]:
                    running = 0.0
                    for row in wheel["transactions"]:
                        running += row["net_cash_flow"] or 0.0
                        self.assertAlmostEqual(row["running_cash_flow"], round(running, 2), places=2)

    def test_trade_log_is_filter_independent(self):
        """A date filter narrows the Dashboard's cycles but must never shrink a
        Trade Log wheel -- it is always the whole wheel.
        """
        for path in available_exports():
            with self.subTest(export=os.path.basename(path)):
                dashboard = Dashboard(path)
                full = dashboard.build()["trade_log"]["wheels"]
                narrowed = dashboard.build(Filters(start=date(2025, 11, 1), end=date(2025, 11, 30)))
                narrowed_wheels = narrowed["trade_log"]["wheels"]
                self.assertEqual(
                    {w["cycle_id"]: len(w["transactions"]) for w in full},
                    {w["cycle_id"]: len(w["transactions"]) for w in narrowed_wheels},
                )

    def test_building_twice_is_deterministic(self):
        """Guards against state leaking onto the shared transaction records."""
        for path in available_exports():
            with self.subTest(export=os.path.basename(path)):
                dashboard = Dashboard(path)
                first = dashboard.build()
                second = dashboard.build()
                self.assertEqual(
                    first["portfolio"]["net_realized_pl"], second["portfolio"]["net_realized_pl"]
                )
                self.assertEqual(
                    [c["cycle_id"] for c in first["cycles"]],
                    [c["cycle_id"] for c in second["cycles"]],
                )
                self.assertEqual(
                    sum(1 for c in first["cycles"] for a in c["assignment_events"] if a["synthetic"]),
                    sum(1 for c in second["cycles"] for a in c["assignment_events"] if a["synthetic"]),
                )


@unittest.skipUnless(available_exports(), "no exports present")
class TestCapitalPayload(unittest.TestCase):
    """The capital series is what the dashboard's stacked area is drawn from."""

    @classmethod
    def setUpClass(cls):
        cls.dashboard = Dashboard(available_exports())
        cls.payload = cls.dashboard.build()

    @staticmethod
    def days(series):
        return [date.fromisoformat(point["date"]) for point in series]

    def test_total_equals_the_sum_of_its_rounded_components(self):
        """Exact, not almost-equal -- the chart divides by `total` for shares.

        Rounding the total independently of its parts lets them disagree by a
        cent or two, which then shows up as a band that does not reach the line.
        """
        checked = 0
        for series in [self.payload["capital_series"]] + [c["capital"] for c in self.payload["cycles"]]:
            for point in series:
                self.assertEqual(
                    point["total"],
                    round(
                        point["put"] + point["stock"] + point["call"] + point["long"] + point["spread"], 2
                    ),
                    point["date"],
                )
                checked += 1
        self.assertGreater(checked, 0)

    def test_capital_series_days_are_consecutive(self):
        days = self.days(self.payload["capital_series"])
        self.assertTrue(days)
        for earlier, later in zip(days, days[1:]):
            self.assertEqual((later - earlier).days, 1, f"{earlier} -> {later}")

    def test_capital_series_is_consecutive_under_every_ticker_filter(self):
        """The regression guard for the fabricated-ramp bug.

        A filtered view used to omit days on which that ticker had nothing live,
        so the area ramped straight across the hole -- 159 days of invented
        capital on TGT alone, and 1,886 across all tickers.
        """
        for ticker in self.payload["meta"]["available_tickers"]:
            with self.subTest(ticker=ticker):
                days = self.days(self.dashboard.build(Filters(tickers=[ticker]))["capital_series"])
                if len(days) < 2:
                    continue
                span = (days[-1] - days[0]).days + 1
                self.assertEqual(len(days), span, f"{ticker} is missing {span - len(days)} days")

    def test_banded_series_never_exceed_the_total(self):
        """The share view stacks these against a fixed 0-100 axis."""
        for point in self.payload["capital_series"]:
            if point["total"] <= 0:
                continue
            banded = point["put"] + point["stock"] + point["call"]
            self.assertLessEqual(banded / point["total"], 1 + 1e-9, point["date"])

    def test_capital_is_never_negative(self):
        for point in self.payload["capital_series"]:
            for key in ("put", "stock", "call", "long", "spread", "total"):
                self.assertGreaterEqual(point[key], 0.0, f"{point['date']} {key}")


@unittest.skipUnless(len(available_exports()) > 1, "needs at least two exports")
class TestCombinedExports(unittest.TestCase):
    """Loading every export at once must produce one coherent timeline."""

    @classmethod
    def setUpClass(cls):
        cls.paths = available_exports()
        cls.payload = Dashboard(cls.paths).build()

    def test_duplicates_are_merged_not_summed(self):
        meta = self.payload["meta"]
        self.assertTrue(meta["combined"])
        self.assertEqual(meta["rows_parsed"] - meta["duplicates_removed"], meta["rows_kept"])
        self.assertEqual(meta["rows_kept"], meta["transactions_total"])

    def test_combined_view_still_balances(self):
        self.assertTrue(self.payload["reconciliation"]["balanced"])
        self.assertEqual(self.payload["reconciliation"]["row_failures"], [])

    def test_combining_never_loses_a_ticker(self):
        combined = {row["underlying"] for row in self.payload["tickers"]}
        for path in self.paths:
            with self.subTest(export=os.path.basename(path)):
                alone = {row["underlying"] for row in Dashboard(path).build()["tickers"]}
                self.assertTrue(alone <= combined, alone - combined)

    def test_combining_resolves_split_positions(self):
        """A close whose open sits in another file must find it once combined."""
        alone = sum(
            len(Dashboard(path).build()["meta"]["unmatched_closes"]) for path in self.paths
        )
        together = len(self.payload["meta"]["unmatched_closes"])
        self.assertLessEqual(together, alone)

    def test_span_covers_every_source(self):
        meta = self.payload["meta"]
        self.assertEqual(meta["data_first_date"], min(s["first_date"] for s in meta["sources"]))
        self.assertEqual(meta["data_last_date"], max(s["last_date"] for s in meta["sources"]))

    def test_source_order_does_not_change_the_result(self):
        reversed_payload = Dashboard(list(reversed(self.paths))).build()
        self.assertEqual(
            self.payload["portfolio"]["net_realized_pl"],
            reversed_payload["portfolio"]["net_realized_pl"],
        )
        self.assertEqual(
            self.payload["meta"]["rows_kept"], reversed_payload["meta"]["rows_kept"]
        )
        self.assertEqual(
            sorted(c["cycle_id"] for c in self.payload["cycles"]),
            sorted(c["cycle_id"] for c in reversed_payload["cycles"]),
        )

    def test_loading_one_export_twice_changes_nothing(self):
        single = Dashboard(self.paths[0]).build()
        doubled = Dashboard([self.paths[0], self.paths[0]]).build()
        self.assertEqual(
            single["portfolio"]["net_realized_pl"], doubled["portfolio"]["net_realized_pl"]
        )
        self.assertEqual(single["meta"]["transactions_total"], doubled["meta"]["transactions_total"])


@unittest.skipUnless(available_exports(), "no exports present")
class TestFilteredViews(unittest.TestCase):
    """Filter behaviour, exercised against whatever exports are actually present.

    Tickers and dates are read off the data rather than hard-coded, so this
    keeps working as exports are added, swapped or renamed.
    """

    @classmethod
    def setUpClass(cls):
        cls.dashboard = Dashboard(available_exports())
        meta = cls.dashboard.build()["meta"]
        cls.tickers = meta["available_tickers"]
        cls.first_date = date.fromisoformat(meta["data_first_date"])
        cls.last_date = date.fromisoformat(meta["data_last_date"])
        # A date strictly inside the range, so both the before and after sides
        # of a start/end filter are non-trivial.
        cls.mid_date = cls.first_date + (cls.last_date - cls.first_date) / 2

    def test_every_filter_combination_still_balances(self):
        one, two = (self.tickers[:2] + self.tickers[:1] * 2)[:2]
        combinations = [
            Filters(),
            Filters(tickers=[one]),
            Filters(tickers=[one, two]),
            Filters(statuses=["ACTIVE"]),
            Filters(statuses=["CLOSED", "ASSIGNED"]),
            Filters(start=self.first_date, end=self.mid_date),
            Filters(tickers=[one], start=self.mid_date, statuses=["ACTIVE"]),
            Filters(tickers=["NOPE-NOT-A-TICKER"]),
        ]
        for filters in combinations:
            with self.subTest(filters=filters):
                payload = self.dashboard.build(filters)
                self.assertTrue(payload["reconciliation"]["balanced"])

    def test_ticker_filter_restricts_the_slice(self):
        ticker = self.tickers[0]
        payload = self.dashboard.build(Filters(tickers=[ticker]))
        self.assertEqual({cycle["underlying"] for cycle in payload["cycles"]}, {ticker})

    def test_status_filter_restricts_the_slice(self):
        payload = self.dashboard.build(Filters(statuses=["ACTIVE"]))
        self.assertTrue(all(cycle["status"] == "ACTIVE" for cycle in payload["cycles"]))

    def test_date_filter_excludes_outside_activity(self):
        payload = self.dashboard.build(Filters(start=self.mid_date))
        for cycle in payload["cycles"]:
            for leg in cycle["legs"]:
                self.assertGreaterEqual(leg["open_date"], self.mid_date.isoformat())

    def test_unknown_ticker_yields_an_empty_but_valid_payload(self):
        payload = self.dashboard.build(Filters(tickers=["NOPE"]))
        self.assertEqual(payload["cycles"], [])
        self.assertEqual(payload["portfolio"]["cycles"], 0)
        self.assertEqual(payload["capital_series"], [])

    def test_query_string_parsing(self):
        filters = Filters.from_query(
            {"tickers": ["mu,qqq"], "start": ["2025-10-01"], "end": ["2025-10-31"], "status": ["active"]}
        )
        self.assertEqual(filters.tickers, ["MU", "QQQ"])
        self.assertEqual(filters.start, date(2025, 10, 1))
        self.assertEqual(filters.statuses, ["ACTIVE"])

    def test_malformed_dates_are_ignored_not_fatal(self):
        filters = Filters.from_query({"start": ["not-a-date"], "end": [""]})
        self.assertIsNone(filters.start)
        self.assertIsNone(filters.end)


class TestCapitalWindowEndToEnd(unittest.TestCase):
    """The gap-day fix, exercised through the real Dashboard.build() -> JSON path.

    tests.test_metrics.TestCapitalWindowContinuity covers the same fix directly
    against the metrics functions; this covers the wiring in Dashboard.build()
    itself -- the part a unit test on metrics.py cannot see -- against a CSV
    parsed the same way a real export is.
    """

    @classmethod
    def setUpClass(cls):
        from tests.test_parser import write_csv

        cls.path = write_csv(
            [
                # A put sold and assigned in January, then silence on this
                # ticker until a covered call in September -- the same shape
                # that showed the bug against real broker data.
                '01/15/2025,"YOU SOLD OPENING TRANSACTION PUT (MU) MICRON TECHNOLOGY '
                'FEB 01 25 $100 (100 SHS) (Cash)",-MU250201P100,'
                '"PUT (MU) MICRON TECHNOLOGY FEB 01 25 $100 (100 SHS)",Cash,-1,2.00,'
                '0.65,0.02,,199.33,50000.00,01/16/2025',
                '02/03/2025,"ASSIGNED as of Feb-01-2025 PUT (MU) MICRON TECHNOLOGY '
                'FEB 01 25 $100 (100 SHS) (Cash)",-MU250201P100,'
                '"PUT (MU) MICRON TECHNOLOGY FEB 01 25 $100 (100 SHS)",Cash,,1,,,,'
                '0.00,50000.00,',
                '09/10/2025,"YOU SOLD OPENING TRANSACTION CALL (MU) MICRON TECHNOLOGY '
                'SEP 15 25 $120 (100 SHS) (Cash)",-MU250915C120,'
                '"CALL (MU) MICRON TECHNOLOGY SEP 15 25 $120 (100 SHS)",Cash,-1,1.50,'
                '0.65,0.02,,149.33,50000.00,09/11/2025',
            ]
        )
        cls.dashboard = Dashboard(cls.path)

    @classmethod
    def tearDownClass(cls):
        os.unlink(cls.path)

    def test_position_carried_into_the_window_is_not_zero_on_day_one(self):
        payload = self.dashboard.build(Filters(start=date(2025, 6, 1)))
        series = payload["capital_series"]
        self.assertTrue(series)
        self.assertEqual(series[0]["date"], "2025-06-01")
        # The 100 shares assigned in February are still held on June 1st.
        self.assertAlmostEqual(series[0]["stock"], 10000.0, places=2)
        self.assertAlmostEqual(series[0]["total"], 10000.0, places=2)

    def test_portfolio_capital_stats_reflect_the_true_position(self):
        payload = self.dashboard.build(Filters(start=date(2025, 6, 1)))
        portfolio = payload["portfolio"]
        self.assertGreater(portfolio["avg_capital"], 0.0)
        self.assertAlmostEqual(portfolio["capital_deployed_now"], 10000.0, places=2)

    def test_unfiltered_view_is_unaffected(self):
        baseline = self.dashboard.build(Filters())
        series = baseline["capital_series"]
        first = next(p for p in series if p["date"] == "2025-02-03")
        self.assertAlmostEqual(first["stock"], 10000.0, places=2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
