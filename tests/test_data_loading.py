"""How fast the dashboard gets its data, without changing what it shows.

Each class pins one fix: what must stay exactly as it was (the same values, the
same price-freshness limit, honest fetch times) and the cost that must not come
back (one quote request per account, re-parsing price files by the slow path,
two dashboard fetches per page load, redrawing every tab on a tab switch).

No test makes a real network call. The browser tests run the real page against
a real server on a temp folder, and are skipped when Playwright or its Chromium
build isn't installed.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wheel.api as api  # noqa: E402
from tests.test_accounts import (  # noqa: E402
    csp_round_trip_rows,
    positions_row,
    write_history_csv,
    write_positions_csv,
)
from tests.test_engine import tx  # noqa: E402
from wheel import marketdata  # noqa: E402
from wheel.accounts import AccountRegistry  # noqa: E402
from wheel.api import Dashboard, LiveQuoteBatch, prefetch_option_chains  # noqa: E402
from wheel.engine import build_cycles  # noqa: E402
from wheel.marketdata import (  # noqa: E402
    PricePoint,
    _cache_date,
    _cache_num,
    load_cache,
    price_on_or_before,
    save_cache,
)
from wheel.metrics import _CAPITAL_TIMELINE_MEMO_LIMIT, _compute_capital_timeline, capital_timeline  # noqa: E402
from wheel.parser import EXPIRED, STO, _num, _parse_date  # noqa: E402
from wheel.serve import DashboardHTTPServer, Handler, Workspace  # noqa: E402

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - depends on the dev machine
    sync_playwright = None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TTL = api._LIVE_PRICE_TTL_SECONDS


def _two_account_folder(parent: str) -> str:
    data_dir = os.path.join(parent, "data")
    for folder, number, name, symbol, opened, closed in (
        ("ira", "111111111", "IRA", "-MU250926P150", "09/19/2025", "09/25/2025"),
        ("taxable", "222222222", "Taxable", "-MU251010P200", "10/01/2025", "10/10/2025"),
    ):
        os.makedirs(os.path.join(data_dir, folder))
        write_history_csv(
            os.path.join(data_dir, folder, "History_for_Account.csv"),
            csp_round_trip_rows(symbol, opened, closed, 3.35, 0.95),
        )
        write_positions_csv(
            os.path.join(data_dir, folder, "Portfolio_Positions.csv"),
            number,
            name,
            [positions_row(number, name, "MU", 15000.0)],
            "Oct-10-2025",
        )
    return data_dir


class TestPriceCacheParsing(unittest.TestCase):
    def test_a_saved_cache_is_read_without_the_slow_general_parser(self):
        points = [
            PricePoint(date(2026, 1, 2), 101.5, 102.0, 100.25, 1000.0),
            PricePoint(date(2026, 1, 5), 103.0),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "MU.csv")
            save_cache(points, path)
            with mock.patch("wheel.marketdata._parse_date", side_effect=AssertionError("slow path")), mock.patch(
                "wheel.marketdata._num", side_effect=AssertionError("slow path")
            ):
                self.assertEqual(load_cache(path), points)

    def test_every_date_cell_reads_as_the_general_parser_reads_it(self):
        cells = ["2026-01-02", "01/02/2026", "2026-13-01", "2026-02-30", "20260102", "2026-1-2", "2026-W01-1", "", "x"]
        cells += ["Jan-02-2026", "01/02/26", "2026/01/02", "2026-01-0x"]
        for cell in cells:
            self.assertEqual(_cache_date(cell), _parse_date(cell), cell)

    def test_every_number_cell_reads_as_the_general_parser_reads_it(self):
        for cell in ["1.5", "-2", "1e3", "$5", "--", "-", "", "(3)", "n/a", "N/A", "abc", "1_000", " 7 "]:
            self.assertEqual(_cache_num(cell), _num(cell), cell)

    def test_a_hand_edited_file_still_reads_row_by_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "MU.csv")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("2026-01-05,$10\n01/02/2026,9.5\nnot-a-date,1\n2026-01-06,--\n2026-01-07,11,,,\nbad\n")
            self.assertEqual(
                load_cache(path),
                [
                    PricePoint(date(2026, 1, 2), 9.5),
                    PricePoint(date(2026, 1, 5), 10.0),
                    PricePoint(date(2026, 1, 7), 11.0),
                ],
            )


class TestPriceLookup(unittest.TestCase):
    def test_matches_a_plain_scan_on_every_day_around_the_series(self):
        points = [PricePoint(date(2026, 1, 5) + timedelta(days=offset), float(offset)) for offset in (0, 1, 4, 7)]
        day = date(2026, 1, 1)
        while day < date(2026, 1, 20):
            earlier = [point for point in points if point.day <= day]
            self.assertEqual(price_on_or_before(points, day), earlier[-1] if earlier else None, day)
            day += timedelta(days=1)


class SharedQuoteCase(unittest.TestCase):
    """Two real accounts, a controllable clock and a counting quote endpoint."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.registry = AccountRegistry(base_dir=_two_account_folder(tmp.name), extra_dirs=())
        self.clock = 1000.0
        self.quote_calls: list[list[str]] = []
        self.quote_value = 100.0
        self.quote_warnings: list[str] = []

        def fake_get_last_prices(tickers, **_):
            self.quote_calls.append(sorted(tickers))
            return {ticker: self.quote_value for ticker in tickers}, list(self.quote_warnings)

        self._patch("time.monotonic", lambda: self.clock)
        self._patch("wheel.marketdata.get_last_prices", fake_get_last_prices)
        self._patch("wheel.marketdata.get_price_series", lambda *a, **k: ([PricePoint(date(2026, 1, 2), 90.0)], []))
        self._patch("wheel.marketdata.get_option_expirations", lambda *a, **k: ({}, []))
        self._patch("wheel.marketdata.get_fundamentals", lambda *a, **k: ({}, []))
        self.held = mock.patch.object(Dashboard, "_held_tickers", lambda dashboard: ["MU"])
        self.held.start()
        self.addCleanup(self.held.stop)

    def _patch(self, target, replacement):
        patcher = mock.patch(target, replacement)
        patcher.start()
        self.addCleanup(patcher.stop)

    def dashboards(self):
        return list(self.registry._accounts.values())


class TestOneQuoteRequestPerRefresh(SharedQuoteCase):
    def test_every_view_costs_one_request_for_all_accounts(self):
        for view in ("combined", "ira", "taxable"):
            self.registry.build("combined")  # candidates known from here on
            self.clock += TTL + 1
            self.quote_calls.clear()
            self.registry.build(view)
            self.assertEqual(len(self.quote_calls), 1, view)
            self.assertIn("MU", self.quote_calls[0])

    def test_requests_inside_the_limit_fetch_nothing(self):
        self.registry.build("combined")
        self.quote_calls.clear()
        self.clock += TTL - 1
        self.registry.build("combined")
        self.registry.build("ira")
        self.assertEqual(self.quote_calls, [])

    def test_a_price_is_refetched_the_moment_it_reaches_the_limit(self):
        self.registry.build("ira")
        self.clock += TTL
        self.quote_value = 123.0
        self.quote_calls.clear()
        self.registry.build("ira")
        self.assertEqual(len(self.quote_calls), 1)
        for dashboard in self.dashboards():
            self.assertEqual(dashboard._current_prices()["MU"], 123.0)

    def test_freshness_counts_from_when_the_quote_was_fetched(self):
        # A shared batch must not reset the clock when a Dashboard reads it
        # later, or the price could be nearly twice the limit old.
        dashboard = self.dashboards()[0]
        dashboard.adopt_quote_batch(LiveQuoteBatch({"MU": 50.0}, (), self.clock))
        self.clock += TTL - 1
        self.assertEqual(dashboard._current_prices()["MU"], 50.0)
        self.assertEqual(dashboard._price_cache_at, self.clock - (TTL - 1))
        self.clock += 1
        self.assertTrue(dashboard.live_prices_expired())

    def test_an_expired_batch_is_never_used(self):
        dashboard = self.dashboards()[0]
        dashboard.adopt_quote_batch(LiveQuoteBatch({"MU": 50.0}, (), self.clock))
        self.clock += TTL
        self.quote_calls.clear()
        self.assertEqual(dashboard._current_prices()["MU"], 100.0)
        self.assertEqual(self.quote_calls, [["MU"]])

    def test_a_ticker_the_batch_lacks_is_fetched_directly(self):
        dashboard = self.dashboards()[0]
        dashboard.adopt_quote_batch(LiveQuoteBatch({"OTHER": 50.0}, (), self.clock))
        self.quote_calls.clear()
        self.assertEqual(dashboard._current_prices()["MU"], 100.0)
        self.assertEqual(self.quote_calls, [["MU"]])

    def test_the_next_batch_covers_candidates_priced_last_time(self):
        dashboard = self.dashboards()[0]
        dashboard._price_stats(["CAND"])
        self.assertEqual(dashboard.live_quote_tickers(), {"MU", "CAND"})

    def test_a_failed_quote_warns_and_falls_back_to_the_close_never_an_invented_price(self):
        self.quote_value = None
        self.quote_warnings = ["could not fetch last prices"]
        self.registry.build("combined")
        for dashboard in self.dashboards():
            self.assertEqual(dashboard._current_prices()["MU"], 90.0)
            self.assertIn("could not fetch last prices", dashboard._price_warnings)

    def test_when_one_account_expires_every_account_reads_the_same_quotes(self):
        self.registry.build("combined")
        first, second = self.dashboards()
        first._price_cache_at -= TTL
        self.quote_value = 123.0
        self.quote_calls.clear()
        self.registry.build("combined")
        self.assertEqual(len(self.quote_calls), 1)
        self.assertEqual(first._price_cache_at, second._price_cache_at)
        self.assertEqual(second._current_prices()["MU"], 123.0)

    def test_chains_are_prefetched_once_per_refresh(self):
        with mock.patch("wheel.accounts.prefetch_option_chains") as prefetch:
            self.registry.build("combined")
            self.assertEqual(prefetch.call_count, 1)
            self.clock += TTL - 1
            self.registry.build("combined")
            self.assertEqual(prefetch.call_count, 1)
            self.clock += 1
            self.registry.build("ira")
            self.assertEqual(prefetch.call_count, 2)

    def test_a_dashboard_on_its_own_still_fetches_for_itself(self):
        dashboard = self.dashboards()[0]
        self.assertIsNone(dashboard._quote_batch)
        self.quote_calls.clear()
        dashboard._current_prices()
        self.assertEqual(self.quote_calls, [["MU"]])


class TestOptionChainPrefetch(unittest.TestCase):
    PAIR = ("MU", date.today() + timedelta(days=30))
    OTHER = ("QQQ", date.today() + timedelta(days=30))

    def setUp(self):
        self.addCleanup(self._reset)
        self._reset()
        self.clock = 1000.0
        self.fetches: list[tuple[str, date]] = []
        self.failing: set[str] = set()

        def fake_fetch(ticker, expiry, timeout=10.0):
            self.fetches.append((ticker, expiry))
            if ticker in self.failing:
                raise marketdata.MarketDataError("no chain")
            return f"{ticker}|{expiry}"

        def fake_parse(text, expiry, ticker=None):
            return f"parsed {text}"

        for target, replacement in (
            ("time.monotonic", lambda: self.clock),
            ("wheel.marketdata.fetch_yahoo_option_chain", fake_fetch),
            ("wheel.marketdata.parse_yahoo_option_chain", fake_parse),
        ):
            patcher = mock.patch(target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.dashboard = Dashboard.__new__(Dashboard)

    @staticmethod
    def _reset():
        api._OPTION_CHAIN_CACHE.clear()
        api._OPTION_CHAIN_DEMAND.clear()

    def test_the_first_refresh_has_nothing_to_prefetch(self):
        prefetch_option_chains()
        self.assertEqual(self.fetches, [])

    def test_a_refresh_fetches_last_times_chains_once_and_builds_read_them(self):
        self.dashboard._option_chains({self.PAIR, self.OTHER})
        self.clock += TTL
        self.fetches.clear()
        prefetch_option_chains()
        self.assertEqual(sorted(self.fetches), sorted([self.PAIR, self.OTHER]))
        self.fetches.clear()
        chains = self.dashboard._option_chains({self.PAIR, self.OTHER})
        self.assertEqual(self.fetches, [])
        self.assertEqual(chains[self.PAIR][0], f"parsed MU|{self.PAIR[1]}")

    def test_a_chain_inside_the_limit_is_not_refetched(self):
        self.dashboard._option_chains({self.PAIR})
        self.clock += TTL - 1
        self.fetches.clear()
        prefetch_option_chains()
        self.assertEqual(self.fetches, [])

    def test_a_prefetched_chain_is_refetched_when_it_reaches_the_limit(self):
        self.dashboard._option_chains({self.PAIR})
        self.clock += TTL
        prefetch_option_chains()
        self.dashboard._option_chains({self.PAIR})
        self.clock += TTL
        self.fetches.clear()
        self.dashboard._option_chains({self.PAIR})
        self.assertEqual(self.fetches, [self.PAIR])

    def test_a_prefetched_chain_reports_its_real_fetch_time(self):
        self.dashboard._option_chains({self.PAIR})
        first_fetched_at = self.dashboard._option_chain_fetched_at[self.PAIR]
        self.clock += TTL
        before = datetime.now(timezone.utc)
        prefetch_option_chains()
        after = datetime.now(timezone.utc)
        self.dashboard._option_chains({self.PAIR})
        fetched_at = self.dashboard._option_chain_fetched_at[self.PAIR]
        self.assertGreaterEqual(fetched_at, first_fetched_at)
        self.assertTrue(before <= fetched_at <= after)

    def test_a_failed_prefetch_reads_as_unavailable_with_its_warning(self):
        self.dashboard._option_chains({self.PAIR})
        self.clock += TTL
        self.failing.add("MU")
        prefetch_option_chains()
        snapshot, warning = self.dashboard._option_chains({self.PAIR})[self.PAIR]
        self.assertIsNone(snapshot)
        self.assertIn("could not fetch option chain for MU", warning)

    def test_a_chain_nothing_reads_any_more_stops_being_fetched(self):
        self.dashboard._option_chains({self.PAIR})
        self.clock += TTL
        prefetch_option_chains()
        self.clock += TTL
        self.fetches.clear()
        prefetch_option_chains()
        self.assertEqual(self.fetches, [])

    def test_an_expiration_that_has_passed_is_not_fetched(self):
        api._OPTION_CHAIN_DEMAND.add(("MU", date.today() - timedelta(days=1)))
        prefetch_option_chains()
        self.assertEqual(self.fetches, [])
        self.assertEqual(api._OPTION_CHAIN_DEMAND, set())

    def test_a_private_cache_adds_nothing_to_the_shared_refresh(self):
        self.dashboard._option_chain_cache = {}
        self.dashboard._option_chains({self.PAIR})
        self.assertEqual(api._OPTION_CHAIN_DEMAND, set())


class TestCapitalTimelineMemo(unittest.TestCase):
    def setUp(self):
        self.cycle = build_cycles(
            [
                tx("2025-09-19", STO, "-MU250926P150", -2, 3.35, 668.66),
                tx("2025-09-26", EXPIRED, "-MU250926P150", 2, None, 0.0, as_of="2025-09-26"),
            ]
        )[0][0]

    def test_values_match_a_fresh_calculation_for_each_horizon(self):
        for through in (date(2025, 9, 18), date(2025, 9, 22), date(2025, 9, 26), date(2025, 10, 5)):
            self.assertEqual(capital_timeline(self.cycle, through), _compute_capital_timeline(self.cycle, through))

    def test_repeat_calls_do_not_recalculate(self):
        through = date(2025, 9, 26)
        self.assertIs(capital_timeline(self.cycle, through), capital_timeline(self.cycle, through))

    def test_a_timeline_that_comes_back_empty_is_still_correct(self):
        self.assertEqual(capital_timeline(self.cycle, date(2025, 9, 18)), [])

    def test_old_horizons_do_not_pile_up(self):
        for offset in range(_CAPITAL_TIMELINE_MEMO_LIMIT * 3):
            capital_timeline(self.cycle, date(2025, 9, 20) + timedelta(days=offset))
        self.assertLessEqual(len(self.cycle.capital_timeline_memo), _CAPITAL_TIMELINE_MEMO_LIMIT)

    def test_the_memo_is_not_part_of_the_cycles_identity(self):
        twin = build_cycles(
            [
                tx("2025-09-19", STO, "-MU250926P150", -2, 3.35, 668.66),
                tx("2025-09-26", EXPIRED, "-MU250926P150", 2, None, 0.0, as_of="2025-09-26"),
            ]
        )[0][0]
        capital_timeline(self.cycle, date(2025, 9, 26))
        self.assertEqual(self.cycle, twin)
        self.assertNotIn("capital_timeline_memo", repr(self.cycle))


class TestAccountListLastDate(SharedQuoteCase):
    def test_each_row_matches_the_date_its_own_dashboard_reports(self):
        rows = {row["id"]: row for row in self.registry.list_accounts()}
        for account_id in ("ira", "taxable"):
            payload = self.registry.build(account_id)
            self.assertEqual(rows[account_id]["data_last_date"], payload["meta"]["data_last_date"])

    def test_combined_reports_the_latest_of_any_account(self):
        # app.js applyRestoredPreset() takes this maximum before the first fetch.
        rows = self.registry.list_accounts()
        latest = max(row["data_last_date"] for row in rows)
        self.assertEqual(self.registry.build("combined")["meta"]["data_last_date"], latest)


class TestServerBacklog(unittest.TestCase):
    def test_a_page_opening_during_a_build_is_not_refused(self):
        # A page load opens about ten connections at once.
        self.assertGreaterEqual(DashboardHTTPServer.request_queue_size, 64)


class TestRenderOrder(unittest.TestCase):
    def test_the_restored_tab_is_showing_before_anything_draws(self):
        # With one fetch per page load, the first draw is the only draw: a tab
        # still hidden then would keep charts sized for a zero-width container.
        with open(os.path.join(ROOT, "wheel", "static", "app.js"), encoding="utf-8") as handle:
            source = handle.read()
        body = source[source.index("function render({ dashboardTabOnly") :]
        self.assertLess(body.index("renderTabs();"), body.index("renderTiles("))


@unittest.skipIf(sync_playwright is None, "Playwright isn't installed")
class TestPageLoadInBrowser(unittest.TestCase):
    """The real page against a real server: requests made and tabs redrawn."""

    CONFIG = {"account": "combined", "activeTab": "dashboard", "datePreset": "ytd", "dateStart": "2020-01-01", "dateEnd": "2020-02-01"}

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        data_dir = _two_account_folder(tmp.name)
        with open(os.path.join(data_dir, "config.json"), "w", encoding="utf-8") as handle:
            json.dump(self.CONFIG, handle)

        def no_network(*_, **__):
            raise marketdata.MarketDataError("no network in tests")

        for name in ("fetch_yahoo_quotes", "fetch_yahoo_chart", "fetch_yahoo_option_chain", "fetch_yahoo_option_expirations"):
            patcher = mock.patch(f"wheel.marketdata.{name}", no_network)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch("wheel.marketdata._yahoo_auth", lambda: (None, None))
        patcher.start()
        self.addCleanup(patcher.stop)

        workspace = Workspace.build([], base_dir=data_dir, extra_dirs=())

        class TestHandler(Handler):
            def log_message(self, *_):
                pass

        TestHandler.workspace = workspace
        TestHandler.session_manager = None
        self.server = DashboardHTTPServer(("127.0.0.1", 0), TestHandler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/"
        self.last_date = max(row["data_last_date"] for row in workspace.registry.list_accounts())

    def _open_page(self, playwright):
        try:
            browser = playwright.chromium.launch(headless=True)
        except Exception as error:  # pragma: no cover - depends on the dev machine
            self.skipTest(f"Chromium isn't installed: {error}")
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        self.dashboard_requests: list[str] = []
        self.page_errors: list[str] = []
        page.on("request", lambda r: self.dashboard_requests.append(r.url) if "/api/dashboard" in r.url else None)
        page.on("pageerror", lambda error: self.page_errors.append(str(error)))
        page.goto(self.url, wait_until="load")
        page.wait_for_function("() => state.data && !document.querySelector('.wrap').classList.contains('loading')")
        page.wait_for_timeout(300)
        return page

    def test_a_page_load_fetches_the_dashboard_once_with_the_right_dates(self):
        with sync_playwright() as playwright:
            page = self._open_page(playwright)
            self.assertEqual(len(self.dashboard_requests), 1, self.dashboard_requests)
            self.assertIn(f"end={self.last_date}", self.dashboard_requests[0])
            self.assertIn(f"start={self.last_date[:4]}-01-01", self.dashboard_requests[0])
            self.assertEqual(page.input_value("#preset"), "ytd")
            self.assertEqual(page.input_value("#end"), self.last_date)
            self.assertEqual(self.page_errors, [])

    def test_if_the_account_list_disagrees_the_response_wins_with_a_second_fetch(self):
        def stale_account_list(route):
            response = route.fetch()
            listing = response.json()
            for row in listing["accounts"]:
                row["data_last_date"] = "2020-02-01"
            route.fulfill(response=response, json=listing)

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            requests: list[str] = []
            page.on("request", lambda r: requests.append(r.url) if "/api/dashboard" in r.url else None)
            page.route("**/api/accounts", stale_account_list)
            page.goto(self.url, wait_until="load")
            page.wait_for_function(
                "(last) => state.data && state.data.meta.filters.end === last"
                " && !document.querySelector('.wrap').classList.contains('loading')",
                arg=self.last_date,
            )
            self.assertEqual(len(requests), 2, requests)
            self.assertIn(f"end={self.last_date}", requests[-1])

    def test_a_tab_switch_fetches_nothing_and_redraws_only_that_tab(self):
        with sync_playwright() as playwright:
            page = self._open_page(playwright)
            page.evaluate(
                """() => {
                  window.__calls = [];
                  for (const name of ['renderPlanner', 'renderRealizedGains', 'renderCapitalProjection', 'drawCapital']) {
                    const original = window[name];
                    window[name] = function () { window.__calls.push(name); return original.apply(this, arguments); };
                  }
                }"""
            )
            before = len(self.dashboard_requests)
            expected = {
                "planner": ["renderPlanner"],
                "realized": ["renderRealizedGains"],
                "tools": ["renderCapitalProjection"],
                "tradelog": [],
                "dashboard": ["drawCapital"],
            }
            for tab, calls in expected.items():
                page.evaluate("() => { window.__calls.length = 0; }")
                page.click(f'.tab[data-tab="{tab}"]')
                self.assertEqual(page.evaluate("() => window.__calls"), calls, tab)
                self.assertFalse(page.evaluate("(t) => document.getElementById('tab-' + t).hidden", tab))
            self.assertEqual(len(self.dashboard_requests), before)
            self.assertEqual(self.page_errors, [])

    def test_a_poll_still_redraws_every_tab(self):
        # Print All reads hidden tabs, so fresh data must reach them too.
        with sync_playwright() as playwright:
            page = self._open_page(playwright)
            calls = page.evaluate(
                """async () => {
                  const calls = [];
                  for (const name of ['renderPlanner', 'renderRealizedGains', 'renderCapitalProjection', 'drawCapital']) {
                    const original = window[name];
                    window[name] = function () { calls.push(name); return original.apply(this, arguments); };
                  }
                  await load({ background: true });
                  return calls;
                }"""
            )
            self.assertEqual(sorted(calls), ["drawCapital", "renderCapitalProjection", "renderPlanner", "renderRealizedGains"])


if __name__ == "__main__":
    unittest.main()
