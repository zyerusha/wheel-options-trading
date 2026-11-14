"""Open wheel P/L (Dashboard card): each open wheel's mark-to-market P&L split
into Options P/L and Holding P/L.

The split is made once, in ``_trade_log_entry``; ``open_wheel_pl_rows`` only
picks the open wheels and copies their figures. These tests run real
transactions through the engine, so they check the intended accounting, not
just that fields are copied: option P/L = realized option P&L + open options
marked; holding P/L = realized share P&L + dividends + held shares marked;
the two always add up to the Trade Log's Mark-to-market P&L exactly.
"""

from __future__ import annotations

import os
import re
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_engine import tx  # noqa: E402
from tests.test_trade_log import _option_row, _trade_log  # noqa: E402
from wheel.accounts import _combine_trade_log  # noqa: E402
from wheel.api import open_wheel_pl_rows  # noqa: E402
from wheel.parser import ASSIGNED, BTC, BTO, EXPIRED, STO  # noqa: E402
from wheel.positions import AccountSnapshot  # noqa: E402

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - depends on the dev machine
    sync_playwright = None

STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "wheel", "static")

# STO put $300 -> assigned 100 sh @ $100 -> STO covered call $150, still open.
ASSIGNED_WITH_OPEN_CALL = [
    tx("2025-01-02", STO, "-MU250117P100", -1, 3.0, 300.0, row_id=1),
    tx("2025-01-17", ASSIGNED, "-MU250117P100", 1, None, 0.0, row_id=2, as_of="2025-01-17"),
    tx("2025-01-20", STO, "-MU250221C105", -1, 1.5, 150.0, row_id=3),
]


def _call_marked_at(last_price: float) -> list[AccountSnapshot]:
    """A Positions export pricing the open MU call, so it is marked to market."""
    return [
        AccountSnapshot(
            account_number="Z1",
            account_name="IRA",
            as_of=datetime(2025, 2, 1, 17, 0),
            as_of_source="footer",
            rows=[_option_row("-MU250221C105", last_price=last_price)],
        )
    ]


def _only_wheel(**kwargs) -> dict:
    (wheel,) = _trade_log(**kwargs)["wheels"]
    return wheel


class TestMarkToMarketSplit(unittest.TestCase):
    def assertSplit(self, wheel: dict, option_pl: float, holding_pl: float) -> None:
        self.assertAlmostEqual(wheel["option_mtm_pl"], option_pl, places=2)
        self.assertAlmostEqual(wheel["holding_mtm_pl"], holding_pl, places=2)
        # Exact, not approximately: the total is defined as the sum of the parts.
        self.assertEqual(wheel["mark_to_market_pl"], round(wheel["option_mtm_pl"] + wheel["holding_mtm_pl"], 2))

    def test_both_parts_positive(self):
        # Options: $300 kept + $150 open call (unmarked: valued at expiry). Shares: 100 x ($110 - $100).
        wheel = _only_wheel(transactions=ASSIGNED_WITH_OPEN_CALL, prices={"MU": 110.0})
        self.assertSplit(wheel, 450.0, 1000.0)

    def test_options_positive_holding_negative(self):
        wheel = _only_wheel(transactions=ASSIGNED_WITH_OPEN_CALL, prices={"MU": 92.0})
        self.assertSplit(wheel, 450.0, -800.0)
        self.assertAlmostEqual(wheel["mark_to_market_pl"], -350.0, places=2)

    def test_both_parts_negative(self):
        # The open call now costs $10.00 to buy back: $300 + $150 - $1,000 = -$550 on options.
        wheel = _only_wheel(
            transactions=ASSIGNED_WITH_OPEN_CALL, prices={"MU": 92.0}, snapshots=_call_marked_at(10.0)
        )
        self.assertTrue(wheel["open_option_pl_marked"])
        self.assertSplit(wheel, -550.0, -800.0)

    def test_options_negative_holding_positive(self):
        wheel = _only_wheel(
            transactions=ASSIGNED_WITH_OPEN_CALL, prices={"MU": 110.0}, snapshots=_call_marked_at(10.0)
        )
        self.assertSplit(wheel, -550.0, 1000.0)

    def test_holding_part_is_zero_before_any_shares(self):
        wheel = _only_wheel(transactions=[tx("2025-01-02", STO, "-MU250117P100", -1, 3.0, 300.0, row_id=1)])
        self.assertSplit(wheel, 300.0, 0.0)

    def test_options_part_is_zero_when_premium_was_bought_back_at_cost(self):
        wheel = _only_wheel(
            transactions=[
                tx("2025-01-02", STO, "-MU250117P100", -1, 3.0, 300.0, row_id=1),
                tx("2025-01-06", BTC, "-MU250117P100", 1, 3.0, -300.0, row_id=2),
                tx("2025-01-07", STO, "-MU250221P100", -1, 2.0, 200.0, row_id=3),
                tx("2025-01-08", BTC, "-MU250221P100", 1, 2.0, -200.0, row_id=4),
            ]
        )
        self.assertEqual(wheel["option_mtm_pl"], 0.0)

    def test_unpriced_shares_leave_holding_and_total_unknown_not_partial(self):
        wheel = _only_wheel(transactions=ASSIGNED_WITH_OPEN_CALL)  # no MU price
        self.assertAlmostEqual(wheel["option_mtm_pl"], 450.0, places=2)
        self.assertIsNone(wheel["holding_mtm_pl"])
        self.assertIsNone(wheel["mark_to_market_pl"])

    def test_split_reconciles_with_the_trade_log_bridge(self):
        # The waterfall's "Option P&L" subtotal + open options is the options
        # part; its final "P&L now" is the total the parts must add up to.
        wheel = _only_wheel(transactions=ASSIGNED_WITH_OPEN_CALL, prices={"MU": 92.0}, snapshots=_call_marked_at(4.0))
        bridge = {step["label"]: step for step in wheel["pl_bridge"]}
        self.assertAlmostEqual(
            wheel["option_mtm_pl"], bridge["Option P&L"]["running"] + wheel["open_option_pl"], places=2
        )
        self.assertAlmostEqual(wheel["pl_bridge"][-1]["running"], wheel["mark_to_market_pl"], places=2)


def _row(cycle_id, underlying, option_pl, holding_pl, *, is_open=True, is_wheel=True, account_id=None):
    total = None if holding_pl is None else round(option_pl + holding_pl, 2)
    return {
        "cycle_id": cycle_id,
        "underlying": underlying,
        "name": None,
        "start_date": "2025-01-02",
        "is_open": is_open,
        "is_wheel": is_wheel,
        "option_mtm_pl": option_pl,
        "holding_mtm_pl": holding_pl,
        "mark_to_market_pl": total,
        "open_option_pl_marked": True,
        **({"account_id": account_id} if account_id else {}),
    }


class TestOpenWheelRows(unittest.TestCase):
    def test_no_open_wheels(self):
        self.assertEqual(open_wheel_pl_rows([]), [])
        self.assertEqual(open_wheel_pl_rows([_row("c1", "MU", 10.0, 5.0, is_open=False)]), [])

    def test_only_open_wheels_are_kept(self):
        rows = open_wheel_pl_rows(
            [
                _row("c1", "MU", 10.0, 5.0),
                _row("c2", "KO", 10.0, 5.0, is_open=False),  # closed wheel
                _row("c3", "F", -10.0, 0.0, is_wheel=False),  # directional, not a wheel
            ]
        )
        self.assertEqual([row["cycle_id"] for row in rows], ["c1"])

    def test_multiple_wheels_best_total_first_unpriced_last(self):
        rows = open_wheel_pl_rows(
            [
                _row("c1", "MU", -100.0, 50.0),
                _row("c2", "KO", 300.0, None),
                _row("c3", "AAPL", 200.0, -50.0),
                _row("c4", "XOM", 100.0, 50.0),  # ties AAPL at $150: ticker breaks it
            ]
        )
        self.assertEqual([row["underlying"] for row in rows], ["AAPL", "XOM", "MU", "KO"])
        for row in rows[:3]:
            self.assertEqual(row["mark_to_market_pl"], round(row["option_pl"] + row["holding_pl"], 2))
        self.assertIsNone(rows[-1]["mark_to_market_pl"])

    def test_figures_are_copied_from_the_trade_log_entry(self):
        (wheel,) = _trade_log(ASSIGNED_WITH_OPEN_CALL, prices={"MU": 92.0})["wheels"]
        (row,) = open_wheel_pl_rows([wheel])
        self.assertEqual(
            (row["option_pl"], row["holding_pl"], row["mark_to_market_pl"]),
            (wheel["option_mtm_pl"], wheel["holding_mtm_pl"], wheel["mark_to_market_pl"]),
        )

    def test_closed_and_directional_cycles_from_the_engine_are_left_out(self):
        wheels = _trade_log(
            [
                # Closed wheel: put sold and expired worthless.
                tx("2025-01-02", STO, "-KO250117P60", -1, 1.0, 100.0, row_id=1),
                tx("2025-01-17", EXPIRED, "-KO250117P60", 1, None, 0.0, row_id=2, as_of="2025-01-17"),
                # Lone long put: directional, not a wheel.
                tx("2025-03-01", BTO, "-F250321P10", 1, 1.0, -100.0, row_id=3),
                # Open wheel.
                tx("2025-01-02", STO, "-MU250117P100", -1, 3.0, 300.0, row_id=11),
                tx("2025-01-17", ASSIGNED, "-MU250117P100", 1, None, 0.0, row_id=12, as_of="2025-01-17"),
            ],
            prices={"MU": 92.0, "F": 9.0},
        )["wheels"]
        self.assertEqual([row["underlying"] for row in open_wheel_pl_rows(wheels)], ["MU"])

    def test_combined_view_keeps_one_bar_per_account(self):
        wheel = _row("c1", "MU", 10.0, 5.0)
        combined = _combine_trade_log(
            {"ira": {"trade_log": {"wheels": [wheel]}}, "taxable": {"trade_log": {"wheels": [wheel]}}}
        )
        rows = open_wheel_pl_rows(combined["wheels"])
        self.assertEqual(sorted(row["cycle_id"] for row in rows), ["ira:c1", "taxable:c1"])
        self.assertEqual(sorted(row["account_id"] for row in rows), ["ira", "taxable"])


def _static(name: str) -> str:
    with open(os.path.join(STATIC, name), encoding="utf-8") as handle:
        return handle.read()


def _chart_row(cycle_id, underlying, option_pl, holding_pl, account_id=None):
    total = None if holding_pl is None else round(option_pl + holding_pl, 2)
    return {
        "cycle_id": cycle_id,
        "account_id": account_id,
        "underlying": underlying,
        "name": None,
        "option_pl": option_pl,
        "holding_pl": holding_pl,
        "mark_to_market_pl": total,
        "open_option_pl_marked": True,
    }


@unittest.skipIf(sync_playwright is None, "Playwright is not installed")
class TestOpenWheelChart(unittest.TestCase):
    """``drawOpenWheelPl`` in a real browser against the real page and
    stylesheet. The network is stubbed out (every fetch stays pending), so
    the app boots without data and the chart is drawn from the rows given."""

    @classmethod
    def setUpClass(cls):
        try:
            cls._playwright = sync_playwright().start()
            cls._browser = cls._playwright.chromium.launch()
        except Exception as error:  # pragma: no cover - missing browser build
            if getattr(cls, "_playwright", None):
                cls._playwright.stop()
            raise unittest.SkipTest(f"Chromium is not available: {error}")
        cls.page = cls._browser.new_page(viewport={"width": 1280, "height": 900})
        html = re.sub(r"<script[^>]*></script>|<link[^>]*>", "", _static("index.html"))
        cls.page.set_content(html)
        cls.page.add_style_tag(content=_static("styles.css"))
        cls.page.evaluate("() => { window.fetch = () => new Promise(() => {}); }")
        for script in ("tiplayout.js", "app.js"):
            cls.page.add_script_tag(content=_static(script))

    @classmethod
    def tearDownClass(cls):
        cls._browser.close()
        cls._playwright.stop()

    def draw(self, rows: list[dict]) -> dict:
        """Draw ``rows``; return each bar's segments, the zero line and the note."""
        return self.page.evaluate(
            """(rows) => {
                state.tickers.clear();
                drawOpenWheelPl(rows);
                const svg = document.getElementById('chart-open-wheel-pl');
                const zero = svg.querySelector('line.zero-line');
                const marks = [...svg.querySelectorAll('rect.mark')].map((r) => ({
                    part: r.dataset.part, sign: r.dataset.sign, fill: r.getAttribute('fill'),
                    x: +r.getAttribute('x'), top: +r.getAttribute('y'),
                    bottom: +r.getAttribute('y') + +r.getAttribute('height'),
                }));
                const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
                const note = document.getElementById('open-wheel-pl-note');
                return {
                    zeroY: zero ? +zero.getAttribute('y1') : null,
                    marks,
                    labels: [...svg.querySelectorAll('text.tick-label[transform]')].map((t) => [t.textContent, t.getAttribute('transform')]),
                    hits: svg.querySelectorAll('rect.hit').length,
                    note: note.hidden ? null : note.textContent,
                    colors: { gainStrong: css('--gain-strong'), gainSoft: css('--gain-soft'),
                              lossStrong: css('--loss-strong'), lossSoft: css('--loss-soft') },
                    table: document.getElementById('open-wheel-pl-table').textContent,
                };
            }""",
            rows,
        )

    def bar(self, drawn: dict, index: int) -> dict:
        xs = sorted({mark["x"] for mark in drawn["marks"]})
        return {mark["part"]: mark for mark in drawn["marks"] if mark["x"] == xs[index]}

    def test_both_positive_stack_above_zero_options_next_to_zero(self):
        drawn = self.draw([_chart_row("c1", "MU", 450.0, 1000.0)])
        bar, zero = self.bar(drawn, 0), drawn["zeroY"]
        self.assertAlmostEqual(bar["option_pl"]["bottom"], zero, places=3)
        self.assertAlmostEqual(bar["holding_pl"]["bottom"], bar["option_pl"]["top"], places=3)
        self.assertEqual(bar["option_pl"]["fill"], drawn["colors"]["gainStrong"])
        self.assertEqual(bar["holding_pl"]["fill"], drawn["colors"]["gainSoft"])

    def test_both_negative_stack_below_zero(self):
        drawn = self.draw([_chart_row("c1", "MU", -550.0, -800.0)])
        bar, zero = self.bar(drawn, 0), drawn["zeroY"]
        self.assertAlmostEqual(bar["option_pl"]["top"], zero, places=3)
        self.assertAlmostEqual(bar["holding_pl"]["top"], bar["option_pl"]["bottom"], places=3)
        self.assertEqual(bar["option_pl"]["fill"], drawn["colors"]["lossStrong"])
        self.assertEqual(bar["holding_pl"]["fill"], drawn["colors"]["lossSoft"])

    def test_mixed_signs_extend_to_opposite_sides_of_zero(self):
        drawn = self.draw([_chart_row("c1", "MU", 450.0, -800.0), _chart_row("c2", "KO", -550.0, 1000.0)])
        zero = drawn["zeroY"]
        # Rows are drawn in the order given (the backend has already sorted them).
        mu, ko = self.bar(drawn, 0), self.bar(drawn, 1)
        self.assertAlmostEqual(mu["option_pl"]["bottom"], zero, places=3)  # options gain: up from zero
        self.assertAlmostEqual(mu["holding_pl"]["top"], zero, places=3)  # holding loss: down from zero
        self.assertAlmostEqual(ko["option_pl"]["top"], zero, places=3)
        self.assertAlmostEqual(ko["holding_pl"]["bottom"], zero, places=3)
        self.assertEqual(mu["holding_pl"]["sign"], "loss")
        self.assertEqual(ko["holding_pl"]["sign"], "gain")
        # Each side's length is to scale: $800 down is 800/450 of $450 up.
        up = zero - mu["option_pl"]["top"]
        down = mu["holding_pl"]["bottom"] - zero
        self.assertAlmostEqual(down / up, 800.0 / 450.0, places=3)

    def test_a_zero_part_draws_no_segment(self):
        drawn = self.draw([_chart_row("c1", "MU", 300.0, 0.0)])
        self.assertEqual([mark["part"] for mark in drawn["marks"]], ["option_pl"])

    def test_no_open_wheels_says_so(self):
        drawn = self.draw([])
        self.assertEqual(drawn["marks"], [])
        self.assertEqual(drawn["note"], "No open wheels right now.")

    def test_unpriced_wheel_is_named_not_drawn(self):
        drawn = self.draw([_chart_row("c1", "MU", 450.0, 1000.0), _chart_row("c2", "KO", 120.0, None)])
        self.assertEqual(drawn["hits"], 1)
        self.assertIn("KO", drawn["note"])
        self.assertIn("KO", drawn["table"])  # the table still lists it, with its known options part

    def test_many_wheels_get_one_rotated_label_each(self):
        rows = [_chart_row(f"c{i}", f"T{i:02d}", 10.0 * i, -5.0 * i) for i in range(1, 61)]
        drawn = self.draw(rows)
        self.assertEqual(drawn["hits"], 60)
        self.assertEqual(len(drawn["labels"]), 60)
        for _, transform in drawn["labels"]:
            self.assertTrue(transform.startswith("rotate(-90"))

    def test_tooltip_shows_ticker_parts_and_total(self):
        self.draw([_chart_row("c1", "MU", 450.0, -800.25)])
        self.page.hover("#chart-open-wheel-pl rect.hit")
        text = self.page.text_content("#tooltip")
        for piece in ("MU", "Options P/L", "Holding P/L", "Total P/L now", "$450.00", "-$800.25", "-$350.25"):
            self.assertIn(piece, text)


if __name__ == "__main__":
    unittest.main()
