"""Capital deployed card: Cash and Unrealized on top of committed capital.

Committed capital is at cost, so when holdings are worth less than was paid
for them the account value sits below committed capital + cash. These tests
pin the rule that every Positions reading is kept and drawn at the real
account value, gain or loss (`splitAccountValue` in wheel/static/app.js), on
both the history chart (`drawCapital`) and the donut (`drawWheelState`).

Regression: a reading below cost used to be dropped entirely (no dot, no
band), and a smaller loss was hidden by shrinking Cash. The real case was
Zafrir's IRA on 2026-11-13: account value $806,169.25 against $830,253.53
committed, so the chart ended at the Nov 2 reading.

Runs the real page script in headless Chromium; skipped without Playwright.
"""

from __future__ import annotations

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - depends on the dev machine
    sync_playwright = None

STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "wheel", "static")

# The real Nov 13 IRA reading.
TOTAL_VALUE = 806169.25
CASH_TOTAL = 26171.79
COMMITTED = 830253.53


def _static(name: str) -> str:
    with open(os.path.join(STATIC, name), encoding="utf-8") as handle:
        return handle.read()


def _point(date: str, stock: float, put: float = 0.0) -> dict:
    return {
        "date": date, "put": put, "stock": stock, "call": 0.0, "long": 0.0, "spread": 0.0,
        "idle_stock": stock, "call_stock": 0.0, "total": stock + put,
    }


def _net_worth(*readings: tuple[str, float, float]) -> dict:
    return {
        "available": True,
        "timeline": [{"as_of": d, "total_value": v, "cash_total": c} for d, v, c in readings],
    }


@unittest.skipIf(sync_playwright is None, "Playwright is not installed")
class CapitalOverlayTestCase(unittest.TestCase):
    """The real page and stylesheet with every fetch left pending, so the app
    boots without data and each test draws from the inputs it gives."""

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


class TestSplitAccountValue(CapitalOverlayTestCase):
    def split(self, total_value, cash_total, committed, put):
        return self.page.evaluate(
            "([v, c, k, p]) => splitAccountValue(v, c, k, p)", [total_value, cash_total, committed, put]
        )

    def test_holdings_below_cost_give_a_negative_unrealized_and_keep_real_cash(self):
        split = self.split(TOTAL_VALUE, CASH_TOTAL, COMMITTED, 0.0)
        self.assertAlmostEqual(split["cash"], CASH_TOTAL, places=2)
        self.assertAlmostEqual(split["amount"], TOTAL_VALUE - COMMITTED, places=2)
        self.assertAlmostEqual(split["unrealized"], TOTAL_VALUE - COMMITTED - CASH_TOTAL, places=2)
        self.assertLess(split["unrealized"], 0)

    def test_parts_always_add_back_to_the_account_value(self):
        for args in [(TOTAL_VALUE, CASH_TOTAL, COMMITTED, 0.0), (900000, 100000, 700000, 0), (50000, 1000, 48000, 5000)]:
            with self.subTest(args=args):
                split = self.split(*args)
                self.assertAlmostEqual(args[2] + split["cash"] + split["unrealized"], args[0], places=6)

    def test_put_collateral_comes_out_of_cash_and_cash_never_goes_negative(self):
        self.assertAlmostEqual(self.split(100000, 30000, 60000, 20000)["cash"], 10000, places=6)
        self.assertEqual(self.split(100000, 5000, 60000, 20000)["cash"], 0)


class TestCapitalChart(CapitalOverlayTestCase):
    def draw(self, points, net_worth) -> dict:
        return self.page.evaluate(
            """([points, netWorth]) => {
                state.capitalMode = 'value';
                drawCapital(points, netWorth);
                const svg = document.getElementById('chart-capital');
                const dots = [...svg.querySelectorAll('circle')].map((c) => ({ cx: +c.getAttribute('cx'), cy: +c.getAttribute('cy'), fill: c.getAttribute('fill') }));
                const hatch = [...svg.querySelectorAll('path')].filter((p) => (p.getAttribute('fill') || '').includes('capital-loss-hatch'));
                const dashed = [...svg.querySelectorAll('path')].filter((p) => p.getAttribute('stroke-dasharray') === '4,3' && p.getAttribute('fill') === 'none');
                const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
                return {
                    dots, hatchCount: hatch.length, dashed: dashed.map((p) => p.getAttribute('d')),
                    legend: document.getElementById('legend-capital').textContent,
                    table: document.getElementById('capital-table').textContent,
                    lossColor: css('--loss-strong'),
                    nd: Object.fromEntries(notDeployedByDate(points, netWorth)),
                };
            }""",
            [points, net_worth],
        )

    def test_reading_below_cost_is_kept_and_drawn_at_the_real_account_value(self):
        points = [_point("2026-11-12", COMMITTED), _point("2026-11-13", COMMITTED)]
        drawn = self.draw(points, _net_worth(("2026-11-13", TOTAL_VALUE, CASH_TOTAL)))
        nd = drawn["nd"]["2026-11-13"]
        self.assertAlmostEqual(COMMITTED + nd["amount"], TOTAL_VALUE, places=2)
        self.assertAlmostEqual(nd["cash"], CASH_TOTAL, places=2)
        # Two dots: top of Cash, and the account value in the loss colour, lower down.
        self.assertEqual(len(drawn["dots"]), 2)
        cash_dot, value_dot = sorted(drawn["dots"], key=lambda d: d["cy"])
        self.assertGreater(value_dot["cy"], cash_dot["cy"])
        self.assertEqual(value_dot["fill"], drawn["lossColor"])
        self.assertIn("-$50,256", drawn["table"])  # Unrealized column, signed
        self.assertIn("$806,169", drawn["table"])  # Total value column, the real figure

    def test_a_loss_draws_the_hatched_band_and_its_key_entry(self):
        points = [_point("2026-11-02", 802759.0), _point("2026-11-13", COMMITTED)]
        drawn = self.draw(points, _net_worth(("2026-11-02", 814651.0, 89687.0), ("2026-11-13", TOTAL_VALUE, CASH_TOTAL)))
        self.assertGreater(drawn["hatchCount"], 0)
        self.assertIn("Unrealized loss", drawn["legend"])
        self.assertEqual(len(drawn["dashed"]), 1)  # one Total-value line across both snapshots

    def test_a_gain_draws_no_loss_band(self):
        points = [_point("2026-09-01", 700000.0), _point("2026-09-10", 700000.0)]
        drawn = self.draw(points, _net_worth(("2026-09-01", 850000.0, 100000.0), ("2026-09-10", 860000.0, 100000.0)))
        self.assertEqual(drawn["hatchCount"], 0)
        self.assertNotIn("Unrealized loss", drawn["legend"])

    def test_a_put_sold_between_snapshots_moves_cash_on_that_day_without_changing_account_value(self):
        # Broker cash and account value don't move when a put is sold; its
        # collateral is a hold on that cash. Between two identical snapshots,
        # the day the put opens must show the same account value, with Cash
        # down by the collateral and committed capital up by it.
        points = [
            _point("2026-10-01", 500000.0),
            _point("2026-10-02", 500000.0, put=20000.0),
            _point("2026-10-03", 500000.0, put=20000.0),
        ]
        net_worth = _net_worth(("2026-10-01", 600000.0, 80000.0), ("2026-10-03", 600000.0, 80000.0))
        days = self.page.evaluate(
            """([points, netWorth]) => {
                const nd = notDeployedByDate(points, netWorth);
                const anchors = [...nd.keys()].sort().map((d) => ({ t: parseDay(d).getTime(), ...nd.get(d) }));
                return points.map((p) => {
                    const t = parseDay(p.date).getTime();
                    const hi = anchors.findIndex((a) => a.t >= t);
                    const a = anchors[hi - 1] || anchors[0], b = anchors[hi] || anchors[0];
                    const f = b.t === a.t ? 0 : (t - a.t) / (b.t - a.t);
                    const s = splitAccountValue(a.totalValue + (b.totalValue - a.totalValue) * f,
                                                a.cashTotal + (b.cashTotal - a.cashTotal) * f, p.total, p.put);
                    return { cash: s.cash, value: p.total + s.amount };
                });
            }""",
            [points, net_worth],
        )
        self.assertEqual([round(d["value"]) for d in days], [600000, 600000, 600000])
        self.assertEqual([round(d["cash"]) for d in days], [80000, 60000, 60000])


class TestWheelStateDonut(CapitalOverlayTestCase):
    def draw(self, total_value, cash_total) -> dict:
        return self.page.evaluate(
            """([totalValue, cashTotal]) => {
                const wheelState = {
                    active_cycles: 3,
                    buckets: { holding: { amount: 830253.53, cycles: 3, tickers: ['MU'] } },
                    parts: { holding_cost_basis: 830253.53 },
                };
                drawWheelState(wheelState, { available: true, total_value: totalValue, cash_total: cashTotal, as_of: '2026-11-13T17:43:00' });
                const svg = document.getElementById('chart-wheel-state');
                return {
                    centre: [...svg.querySelectorAll('text')].map((t) => t.textContent),
                    legend: document.getElementById('legend-wheel-state').innerText,
                    wedges: svg.querySelectorAll('path.mark').length,
                };
            }""",
            [total_value, cash_total],
        )

    def test_below_cost_shows_real_cash_the_loss_and_the_real_total_value(self):
        drawn = self.draw(TOTAL_VALUE, CASH_TOTAL)
        self.assertEqual(drawn["centre"], ["$806k", "total value"])
        self.assertEqual(drawn["wedges"], 2)  # holdings + cash; a loss is not a wedge
        self.assertRegex(drawn["legend"], r"Cash\s*\$26,172")
        self.assertRegex(drawn["legend"], r"Unrealized loss\s*-\$50,256")
        # Rounded parts add up: 830,254 + 26,172 - 50,256 = 806,170.
        self.assertRegex(drawn["legend"], r"Total value\s*\$806,170")

    def test_above_cost_is_unchanged(self):
        drawn = self.draw(900000.0, CASH_TOTAL)
        self.assertEqual(drawn["wedges"], 3)
        self.assertNotIn("Unrealized loss", drawn["legend"])
        self.assertRegex(drawn["legend"], r"Unrealized\s*\$43,575")


if __name__ == "__main__":
    unittest.main()
