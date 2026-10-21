"""The shared tooltip layout (wheel/static/tiplayout.js + the `.tip*` styles).

Every text tooltip -- `title` / `setFormula` tips and chart readouts' formulas
-- is drawn by one renderer that lays the existing text out as title,
calculation, result and notes. These tests run that renderer in a real
browser (headless Chromium via Playwright) against the real stylesheet, and
check the one promise that matters most: the layout is presentation only.
Every figure and word in a tip still reaches the reader, and none is
recomputed.

Skipped when Playwright or its Chromium build isn't installed.
"""

from __future__ import annotations

import os
import re
import sys
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_hosted_mode import HostedModeTestCase  # noqa: E402 - the real-server harness
from wheel.api import _profit_target_explanation  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "wheel", "static")

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - depends on the dev machine
    sync_playwright = None


def _read(name: str) -> str:
    with open(os.path.join(STATIC, name), encoding="utf-8") as handle:
        return handle.read()


# Representative tips, verbatim from what the dashboard renders.
WHEEL_OPTION_PL = "\n".join(
    [
        "Wheel option P/L (this tile) = CSP/CC P/L + Hedge P/L (protective puts, credit-spread legs)",
        "= $85,337.02 + -$1,846.63",
        "= $83,490.39",
        "  Gross, paid-to-close and open figures cover short legs only.",
    ]
)
TOTAL_POSITION_ROI = "\n".join(
    [
        "Annualized Total Position ROI =",
        "  (Option P/L + Stock realized P/L + Stock unrealized P/L + Dividends)",
        "  ÷ Total initial collateral × (365 ÷ Days)",
        "  Excludes open long-option (hedge) P/L.",
        "= ($83,490.39 + $23,652.75 + -$16,749.39 + $3,013.01) ÷ $1,116,871 × (365 ÷ 292)",
        "= 8.36% × 1.25",
        "= 10.4%",
    ]
)
NET_PL_WITH_BLANK = "\n".join(
    [
        "Net P/L = Premium (net) + Stock realized P/L",
        "",
        "= $29,659.19 + $5,150.00",
        "= $34,809.19",
    ]
)
INITIAL_CAP = "\n".join(
    [
        "Initial cap = day one's total committed capital",
        "  Idle shares (cost basis) + Covered-call shares (cost basis) + Put collateral",
        "= $3,250 + $0 + $1,500",
        "= $4,750",
    ]
)
CAPITAL_TODAY = "\n".join(
    [
        "Today =",
        "  Idle shares (cost basis) + Put collateral",
        "= $805,523",
        "",
        "Avg = time-weighted daily mean, $0 days excluded = $601,583",
        "Peak = highest single day = $836,049",
        "",
        "Avg includes $332,251 of idle shares (no covered call written).",
    ]
)
CASH_ON_CASH = "\n".join(
    [
        "Annualized cash-on-cash return =",
        "  (Avg monthly income × 12) ÷ Avg collateral (time-weighted) × 100",
        "= ($3,812.20 × 12) ÷ $601,583 × 100",
        "= 7.60%",
        "Income = all cash collected, incl. premium on open legs and dividends.",
    ]
)
XIRR = "\n".join(
    [
        "XIRR: annualized return that accounts for when each dollar went in or out.",
        "This wheel: 39.0%",
        "Same cash flows in SPY: 13.3%",
        "Difference: $123,510.15",
        "Same dates on both sides. Dividends not counted.",
    ]
)
# Trade Log.
TRADE_LOG_COLLATERAL = "\n".join(
    [
        "Collateral = Short puts (Strike × 100 × Contracts)",
        "  + Cost basis of shares held",
        "  + Strike × 100 per short call with untracked shares (~)",
        "$0 once the wheel is flat.",
    ]
)
TRADE_LOG_MTM = "\n".join(
    [
        "Mark-to-market P&L = realized options + shares + open options (est.)",
        "= $0 + — + $0",
        "= $0",
    ]
)
TRADE_LOG_PROSE = "\n".join(
    [
        "Plain buy-and-hold: shares were bought but no option has ever",
        "been written against them; no cash-secured put, no covered call.",
        "Not a wheel. Its P&L counts toward every total, but the wheel-return",
        "ratios are withheld. Selling a covered call turns it into a wheel.",
    ]
)
# CSP TO ENTER sizing, Open option positions BTC ladder.
CSP_QTY = "\n".join(
    [
        "Qty = Available Cash ÷ (CSP TO ENTER strike × 100), rounded down",
        "= $48,301.38 ÷ ($150.00 × 100)",
        "= 3 contracts",
    ]
)
BTC_LADDER = "\n".join(
    [
        "BTC targets (rounded down to $0.10 above $0.10/share):",
        "  50% × $14.60 = $7.30",
        "  20% × $14.60 = $2.90",
    ]
)
UNVETTED = "Unverified: these checks had no data, so they were not passed:\n· Earnings date\n· Sector"

REPRESENTATIVE = [
    WHEEL_OPTION_PL,
    TOTAL_POSITION_ROI,
    NET_PL_WITH_BLANK,
    INITIAL_CAP,
    CAPITAL_TODAY,
    CASH_ON_CASH,
    XIRR,
    TRADE_LOG_COLLATERAL,
    TRADE_LOG_MTM,
    TRADE_LOG_PROSE,
    CSP_QTY,
    BTC_LADDER,
    UNVETTED,
]

NUMBER_RE = re.compile(r"\$?\d[\d,]*(?:\.\d+)?%?")
WORD_RE = re.compile(r"[A-Za-z][A-Za-z'/-]{2,}")


@unittest.skipIf(sync_playwright is None, "Playwright is not installed")
class TipLayoutTestCase(unittest.TestCase):
    """One headless browser for the module, a fresh page per test class."""

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
        cls.page.set_content(
            "<!doctype html><html><head><meta charset='utf-8'></head>"
            "<body><div id='tooltip' role='tooltip' class='on pinned'></div></body></html>"
        )
        cls.page.add_style_tag(content=_read("styles.css"))
        cls.page.add_script_tag(content=_read("tiplayout.js"))

    @classmethod
    def tearDownClass(cls):
        cls._browser.close()
        cls._playwright.stop()

    def parse(self, text: str) -> dict:
        return self.page.evaluate("(text) => TipLayout.parse(text)", text)

    def render(self, text: str) -> dict:
        """Render into the page's #tooltip; its text and the classes used."""
        return self.page.evaluate(
            """(text) => {
                const tip = document.getElementById('tooltip');
                tip.className = 'on pinned';
                tip.replaceChildren(TipLayout.render(text));
                return {
                  text: tip.textContent,
                  classes: [...new Set([...tip.querySelectorAll('[class]')].flatMap(n => [...n.classList]))],
                };
            }""",
            text,
        )

    def calc(self, text: str, section: int = 0, index: int = 0) -> dict:
        return self.parse(text)["sections"][section][index]


class TestCalculationStructure(TipLayoutTestCase):
    def test_a_plain_sum_lays_out_as_labeled_rows_and_a_total(self):
        calc = self.calc(WHEEL_OPTION_PL)
        self.assertEqual(calc["name"], "Wheel option P/L (this tile)")
        self.assertEqual(
            [(t["label"], t["sub"], t["less"], t["value"]) for t in calc["terms"]],
            [
                ("CSP/CC P/L", None, False, "$85,337.02"),
                ("Hedge P/L", "protective puts, credit-spread legs", False, "-$1,846.63"),
            ],
        )
        self.assertEqual(calc["result"], "$83,490.39")
        self.assertEqual(calc["notes"], [{"type": "note", "text": "Gross, paid-to-close and open figures cover short legs only."}])

    def test_negative_values_never_read_plus_minus(self):
        # The hedge row shows its own signed figure; no "+ -$" anywhere.
        rendered = self.render(WHEEL_OPTION_PL)["text"]
        self.assertIn("-$1,846.63", rendered)
        self.assertNotIn("+ -", rendered)
        # In a substitution line the double sign is rewritten as the same arithmetic.
        steps = [s["text"] for s in self.calc(TOTAL_POSITION_ROI)["steps"]]
        self.assertEqual(steps[0], "($83,490.39 + $23,652.75 - $16,749.39 + $3,013.01) ÷ $1,116,871 × (365 ÷ 292)")
        self.assertNotIn("+ -", self.render(TOTAL_POSITION_ROI)["text"])

    def test_subtracted_terms_are_marked_less_with_their_value_unchanged(self):
        calc = self.calc("Breakeven = cost basis - premium/share\n= $166.99 - $1.09\n= $165.90")
        self.assertEqual(
            [(t["label"], t["less"], t["value"]) for t in calc["terms"]],
            [("Cost basis", False, "$166.99"), ("Premium/share", True, "$1.09")],
        )
        self.assertEqual(calc["result"], "$165.90")

    def test_a_multi_step_formula_keeps_every_step_and_its_result(self):
        calc = self.calc(TOTAL_POSITION_ROI)
        self.assertEqual(
            calc["formula"],
            [
                "(Option P/L + Stock realized P/L + Stock unrealized P/L + Dividends)",
                "÷ Total initial collateral × (365 ÷ Days)",
            ],
        )
        self.assertEqual([s["text"] for s in calc["steps"]][1:], ["8.36% × 1.25"])
        self.assertEqual(calc["result"], "10.4%")
        self.assertEqual(calc["notes"][0]["text"], "Excludes open long-option (hedge) P/L.")
        self.assertIsNone(calc["terms"])

    def test_the_result_belongs_to_the_named_metric_not_an_inner_input(self):
        # Built by the backend from real inputs; "Floor" is an input of Profit
        # Target, so the final figure must be Profit Target's.
        text = _profit_target_explanation(44.90, 46.00, 44.90, 43.23, 43.23)
        calc = self.calc(text)
        self.assertEqual(calc["name"], "Profit Target")
        self.assertEqual(calc["steps"][0]["name"], "Floor")
        self.assertEqual(calc["steps"][0]["text"], "max($44.90, $43.23, $43.23) = $44.90")
        self.assertEqual(calc["result"], "$46.00")
        self.assertEqual(self.parse(text)["sections"][1], [{"type": "note", "text": "Fixed; doesn't move with the share price."}])

    def test_a_blank_line_before_the_substitution_stays_one_calculation(self):
        sections = self.parse(NET_PL_WITH_BLANK)["sections"]
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0][0]["result"], "$34,809.19")
        self.assertEqual([t["value"] for t in sections[0][0]["terms"]], ["$29,659.19", "$5,150.00"])

    def test_a_term_list_on_its_own_line_pairs_with_the_substitution(self):
        calc = self.calc(INITIAL_CAP)
        self.assertEqual(calc["formula"], ["day one's total committed capital"])
        self.assertEqual(
            [(t["label"], t["value"]) for t in calc["terms"]],
            [("Idle shares", "$3,250"), ("Covered-call shares", "$0"), ("Put collateral", "$1,500")],
        )
        self.assertEqual(calc["notes"], [])

    def test_mismatched_shapes_fall_back_to_steps_never_guess_labels(self):
        calc = self.calc("Total committed = sum of every band above\n= $1 + $2 + $3\n= $6")
        self.assertIsNone(calc["terms"])
        self.assertEqual([s["text"] for s in calc["steps"]], ["$1 + $2 + $3"])
        self.assertEqual(calc["result"], "$6")

    def test_one_line_definitions_become_compact_rows(self):
        sections = self.parse(CAPITAL_TODAY)["sections"]
        self.assertEqual([b["name"] for b in sections[1]], ["Avg", "Peak"])
        self.assertEqual([b["result"] for b in sections[1]], ["$601,583", "$836,049"])
        self.assertEqual(sections[2], [{"type": "note", "text": "Avg includes $332,251 of idle shares (no covered call written)."}])

    def test_a_qualifying_sentence_stays_a_note_of_its_calculation(self):
        sections = self.parse(CASH_ON_CASH)["sections"]
        self.assertEqual(len(sections[0]), 1)
        self.assertEqual(
            sections[0][0]["notes"],
            [{"type": "note", "text": "Income = all cash collected, incl. premium on open legs and dividends."}],
        )

    def test_continued_formula_lines_and_no_result(self):
        calc = self.calc(TRADE_LOG_COLLATERAL)
        self.assertEqual(calc["formula"][1:], ["+ Cost basis of shares held", "+ Strike × 100 per short call with untracked shares (~)"])
        self.assertIsNone(calc["result"])
        self.assertEqual(calc["notes"][0]["text"], "$0 once the wheel is flat.")

    def test_a_missing_value_dash_is_kept(self):
        calc = self.calc(TRADE_LOG_MTM)
        self.assertEqual([t["value"] for t in calc["terms"]], ["$0", "—", "$0"])


class TestTextStructure(TipLayoutTestCase):
    def test_hard_wrapped_prose_is_rejoined_into_sentences(self):
        blocks = self.parse(TRADE_LOG_PROSE)["sections"][0]
        self.assertEqual(
            [b["text"] for b in blocks],
            [
                "Plain buy-and-hold: shares were bought but no option has ever been written against them; no cash-secured put, no covered call.",
                "Not a wheel. Its P&L counts toward every total, but the wheel-return ratios are withheld. Selling a covered call turns it into a wheel.",
            ],
        )

    def test_labeled_values_become_aligned_rows(self):
        blocks = self.parse(XIRR)["sections"][0]
        self.assertEqual([b["type"] for b in blocks], ["text", "rows", "text"])
        self.assertEqual(
            [(r["label"], r["value"]) for r in blocks[1]["rows"]],
            [("This wheel", "39.0%"), ("Same cash flows in SPY", "13.3%"), ("Difference", "$123,510.15")],
        )

    def test_a_heading_over_indented_calculations_is_a_row_list(self):
        block = self.parse(BTC_LADDER)["sections"][0][0]
        self.assertEqual(block["heading"], "BTC targets (rounded down to $0.10 above $0.10/share)")
        self.assertEqual([(r["label"], r["value"]) for r in block["rows"]], [("50% × $14.60", "$7.30"), ("20% × $14.60", "$2.90")])

    def test_bullets_render_as_a_list(self):
        block = self.parse(UNVETTED)["sections"][0][0]
        self.assertEqual(block["type"], "list")
        self.assertEqual(block["heading"], "Unverified: these checks had no data, so they were not passed:")
        self.assertEqual(block["items"], ["Earnings date", "Sector"])

    def test_a_plain_label_is_a_single_line_of_text(self):
        self.assertEqual(self.parse("Open MU on TradingView")["sections"], [[{"type": "text", "text": "Open MU on TradingView"}]])


class TestPresentationOnly(TipLayoutTestCase):
    def test_every_figure_and_word_in_the_source_reaches_the_reader(self):
        for text in REPRESENTATIVE + [_profit_target_explanation(738.0, 753.0, 738.0, None, 710.63)]:
            with self.subTest(text=text.splitlines()[0]):
                rendered = self.render(text)["text"]
                for number in NUMBER_RE.findall(text):
                    self.assertIn(number, rendered)
                lowered = rendered.lower()
                for word in WORD_RE.findall(text):
                    self.assertIn(word.lower(), lowered)

    def test_explanatory_notes_stay_visible(self):
        visible = self.page.evaluate(
            """(text) => {
                const tip = document.getElementById('tooltip');
                tip.replaceChildren(TipLayout.render(text));
                return [...tip.querySelectorAll('.tip-note')].map(n => {
                  const s = getComputedStyle(n);
                  return [n.textContent, s.display !== 'none' && s.visibility !== 'hidden' && n.getClientRects().length > 0];
                });
            }""",
            TOTAL_POSITION_ROI,
        )
        self.assertEqual(visible, [["Excludes open long-option (hedge) P/L.", True]])

    def test_spec_tables_show_each_figure_with_its_bar(self):
        spec = {
            "title": "Signal 4 / 5",
            "sections": [
                [
                    {
                        "type": "table",
                        "heading": "Past wheels",
                        "columns": [{"label": ""}, {"label": "Value", "num": True}, {"label": "Score", "num": True}],
                        "rows": [["ROC", "53.7%", {"meter": 0.78, "text": "0.78"}]],
                    }
                ]
            ],
        }
        result = self.page.evaluate(
            """(spec) => {
                const tip = document.getElementById('tooltip');
                tip.replaceChildren(TipLayout.renderSpec(spec));
                const fill = tip.querySelector('.tip-meter > span');
                return { text: tip.textContent, width: fill.style.width,
                         hidden: tip.querySelector('.tip-meter').getAttribute('aria-hidden') };
            }""",
            spec,
        )
        for piece in ("Signal 4 / 5", "Past wheels", "ROC", "53.7%", "0.78"):
            self.assertIn(piece, result["text"])
        self.assertEqual(result["width"], "78%")
        self.assertEqual(result["hidden"], "true")  # the figure, not the bar, is read out


class TestRenderedLayout(TipLayoutTestCase):
    def box(self, text: str, width: int) -> dict:
        self.page.set_viewport_size({"width": width, "height": 800})
        return self.page.evaluate(
            """(text) => {
                const tip = document.getElementById('tooltip');
                tip.className = 'on pinned';
                tip.style.left = '8px';
                tip.style.top = '8px';
                tip.replaceChildren(TipLayout.render(text));
                const r = tip.getBoundingClientRect();
                return { scroll: tip.scrollWidth, client: tip.clientWidth, right: r.right,
                         height: r.height, viewport: innerWidth, viewportHeight: innerHeight };
            }""",
            text,
        )

    def test_long_formulas_wrap_inside_the_tooltip_at_every_width(self):
        long_formula = "Total = " + " + ".join(f"Component number {i} (cost basis)" for i in range(12))
        long_step = "= " + " + ".join(f"${i},234,567.89" for i in range(12))
        text = f"{long_formula}\n{long_step}\n= $99,999,999.99"
        for width in (1280, 390, 320):
            with self.subTest(width=width):
                box = self.box(text, width)
                self.assertLessEqual(box["scroll"], box["client"])  # no sideways scroll
                self.assertLessEqual(box["right"], box["viewport"])  # never past the window

    def test_short_and_long_tips_stay_readable(self):
        short = self.box("Open MU on TradingView", 1280)
        self.assertGreaterEqual(short["client"], 150)  # a minimum width, not a sliver
        tall = self.box("\n\n".join([TOTAL_POSITION_ROI] * 12), 390)
        self.assertLessEqual(tall["height"], tall["viewportHeight"])  # scrolls inside itself

    def test_results_are_set_apart_by_weight_and_a_rule_not_size_or_colour(self):
        style = self.page.evaluate(
            """(text) => {
                const tip = document.getElementById('tooltip');
                tip.replaceChildren(TipLayout.render(text));
                const total = tip.querySelector('.tip-value.tip-total');
                const term = tip.querySelector('.tip-terms .tip-value');
                const rule = tip.querySelector('.tip-terms .tip-rule');
                const t = getComputedStyle(total), v = getComputedStyle(term);
                return { totalWeight: t.fontWeight, termWeight: v.fontWeight,
                         sameSize: t.fontSize === v.fontSize, sameColour: t.color === v.color,
                         rule: getComputedStyle(rule).borderTopStyle,
                         ruleSpans: rule.getBoundingClientRect().width >= tip.querySelector('.tip-terms').getBoundingClientRect().width - 1 };
            }""",
            WHEEL_OPTION_PL,
        )
        self.assertGreater(int(style["totalWeight"]), int(style["termWeight"]))
        self.assertTrue(style["sameSize"])
        self.assertTrue(style["sameColour"])
        self.assertEqual(style["rule"], "solid")
        self.assertTrue(style["ruleSpans"])

    def test_text_tips_and_tip_cards_share_one_container_style(self):
        styles = self.page.evaluate(
            """() => {
                const tip = document.getElementById('tooltip');
                const read = () => { const s = getComputedStyle(tip);
                  return [s.backgroundColor, s.borderTopColor, s.borderTopWidth, s.borderRadius, s.boxShadow]; };
                tip.className = 'on pinned';
                const text = read();
                tip.className = 'on pinned card';
                const card = read();
                tip.className = 'on';
                const readout = read();
                return { text, card, readout };
            }"""
        )
        self.assertEqual(styles["text"], styles["card"])
        self.assertEqual(styles["text"], styles["readout"])


class TestEveryTooltipUsesTheSharedLayout(unittest.TestCase):
    """Static guards: no tooltip path renders raw pre-wrapped text any more."""

    @classmethod
    def setUpClass(cls):
        cls.js = _read("app.js")
        cls.css = _read("styles.css")

    def body(self, name: str) -> str:
        start = self.js.index(f"function {name}(")
        return self.js[start : self.js.index("\nfunction ", start + 1)]

    def test_readouts_and_pinned_text_tips_render_through_tiplayout(self):
        self.assertIn("TipLayout.render(formula)", self.body("renderTooltip"))
        self.assertIn("TipLayout.renderSpec(spec)", self.body("renderTipSpec"))
        pin = self.body("pinTooltip")
        self.assertIn("renderTipSpec(tip.spec)", pin)
        self.assertIn("renderTooltip('', [], tip.text)", pin)
        self.assertNotIn("tt-text", self.js)

    def test_no_tooltip_text_relies_on_preserved_whitespace(self):
        tooltip_css = self.css[self.css.index("/* ---------------- tooltip ---------------- */") : self.css.index("/* ---------------- notices ---------------- */")]
        self.assertNotIn("pre-line", tooltip_css)
        self.assertNotIn("text-overflow", tooltip_css)  # tooltip text is never cut off
        self.assertNotIn("padEnd(", self.body("cspStarTooltip"))  # columns are a table now

    def test_the_t_key_trigger_is_unchanged(self):
        self.assertIn("const isT = event.key === 't' || event.key === 'T';", self.js)
        self.assertIn("if (tooltipPinned) dismissTooltip();\n  else pinTooltip();", self.js)

    def test_tiplayout_loads_before_app_js(self):
        html = _read("index.html")
        self.assertLess(html.index('<script src="/tiplayout.js"></script>'), html.index('<script src="/app.js"></script>'))


class TestTipLayoutIsServed(HostedModeTestCase):
    """The page loads tiplayout.js from the real server route."""

    def test_the_server_serves_tiplayout_js(self):
        with urllib.request.urlopen(self.base_url + "/tiplayout.js") as response:
            self.assertEqual(response.status, 200)
            self.assertIn("javascript", response.headers["Content-Type"])
            self.assertIn(b"global.TipLayout", response.read())


if __name__ == "__main__":
    unittest.main()
