"""Account discovery and Combined-view aggregation tests.

Every test builds its own isolated ``data/`` layout under a temp directory and
passes ``extra_dirs=()`` explicitly, so nothing here ever touches (or is
affected by) the real, gitignored ``data/`` folder in the repo.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import date
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wheel.accounts import (  # noqa: E402
    DEFAULT_ACCOUNT_ID,
    AccountConfig,
    AccountRegistry,
    discover_account_dirs,
    load_account_config,
)
from wheel.api import Dashboard  # noqa: E402

HISTORY_HEADER = (
    "Run Date,Action,Symbol,Description,Type,Quantity,Price ($),Commission ($),"
    "Fees ($),Accrued Interest ($),Amount ($),Cash Balance ($),Settlement Date"
)
POSITIONS_HEADER = (
    "Account number,Account name,Symbol,Description,Quantity,Last price,Last price change,"
    "Current value,Today's gain/loss dollar,Today's gain/loss percent,Total gain/loss dollar,"
    "Total gain/loss percent,Percent of account,Cost basis total,Average cost basis,Type"
)


def write_history_csv(path: str, rows: list[str]) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        handle.write(HISTORY_HEADER + "\n")
        handle.write("\n".join(rows) + "\n")


def write_positions_csv(path: str, account_number: str, account_name: str, rows: list[str], as_of: str) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        handle.write(POSITIONS_HEADER + "\n")
        handle.write("\n".join(rows) + "\n")
        handle.write("\n")
        handle.write(f'"Date downloaded {as_of} 5:00 p.m ET"\n')


def csp_round_trip_rows(symbol: str, open_date: str, close_date: str, open_price: float, close_price: float) -> list[str]:
    """A single sell-to-open/buy-to-close CSP, written the way an unswapped
    (correctly labelled) Fidelity export would -- Quantity holds signed
    contracts, Price ($) holds the per-share premium.
    """
    open_amount = round(1 * open_price * 100, 2)
    close_amount = round(-1 * close_price * 100, 2)
    return [
        f'{open_date},"YOU SOLD OPENING TRANSACTION PUT ({symbol.split("2")[0]}) ...",{symbol},'
        f'"PUT ...",Cash,-1,{open_price},0,0,,{open_amount},10000.00,{open_date}',
        f'{close_date},"YOU BOUGHT CLOSING TRANSACTION PUT ({symbol.split("2")[0]}) ...",{symbol},'
        f'"PUT ...",Cash,1,{close_price},0,0,,{close_amount},9900.00,{close_date}',
    ]


def positions_row(account_number: str, account_name: str, symbol: str, value: float) -> str:
    return (
        f'{account_number},"{account_name}",{symbol},{symbol} DESCRIPTION,10,${value / 10:.2f},,'
        f"${value:.2f},,,,,10.00%,${value:.2f},${value / 10:.2f},Cash,"
    )


class TestDiscoverAccountDirs(unittest.TestCase):
    def test_subfolders_and_implicit_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = os.path.join(tmp, "data")
            ira_dir = os.path.join(data_dir, "ira")
            empty_dir = os.path.join(data_dir, "empty")
            os.makedirs(ira_dir)
            os.makedirs(empty_dir)

            # Loose files directly in data/ -- the implicit "default" account.
            write_history_csv(
                os.path.join(data_dir, "History_for_Account_x.csv"),
                csp_round_trip_rows("-MU250926P150", "09/19/2025", "09/25/2025", 3.35, 0.95),
            )
            write_positions_csv(
                os.path.join(data_dir, "Portfolio_Positions_x.csv"),
                "111111111",
                "Root Account",
                [positions_row("111111111", "Root Account", "MU", 1000.0)],
                "Sep-25-2025",
            )

            # A named subfolder with both file types.
            write_history_csv(
                os.path.join(ira_dir, "History_for_Account_y.csv"),
                csp_round_trip_rows("-MU251010P200", "10/01/2025", "10/10/2025", 5.0, 2.0),
            )
            write_positions_csv(
                os.path.join(ira_dir, "Portfolio_Positions_y.csv"),
                "222222222",
                "IRA Account",
                [positions_row("222222222", "IRA Account", "MU", 2000.0)],
                "Oct-10-2025",
            )

            dirs = discover_account_dirs(base_dir=data_dir, extra_dirs=())
            ids = {d.id: d for d in dirs}

            self.assertIn(DEFAULT_ACCOUNT_ID, ids)
            self.assertEqual(len(ids[DEFAULT_ACCOUNT_ID].history_paths), 1)
            self.assertEqual(len(ids[DEFAULT_ACCOUNT_ID].position_paths), 1)

            self.assertIn("ira", ids)
            self.assertEqual(len(ids["ira"].history_paths), 1)
            self.assertEqual(len(ids["ira"].position_paths), 1)

            # A subfolder with nothing usable in it is not a discovered account.
            self.assertNotIn("empty", ids)


class TestAccountRegistry(unittest.TestCase):
    """Two accounts, each with one closed CSP cycle on MU, on non-overlapping
    date ranges and different capital sizes -- chosen so a naive average of
    each account's own ROI would differ measurably from the correct
    capital-weighted recomputation, and so both accounts land a cycle on the
    same underlying (exercising the cross-account cycle_id collision fix).
    """

    IRA_PREMIUM = 240.0  # 335.00 credit - 95.00 debit
    IRA_COLLATERAL = 15000.0  # strike 150 x 100
    # collateral_on() reads 0 on the day a leg closes, so the engaged-days count
    # in the capital timeline matches days_held (open date through the day
    # before close), not an inclusive day count.
    IRA_HELD = (date(2025, 9, 25) - date(2025, 9, 19)).days  # 6
    IRA_DAYS = IRA_HELD

    TAXABLE_PREMIUM = 300.0  # 500.00 credit - 200.00 debit
    TAXABLE_COLLATERAL = 20000.0  # strike 200 x 100
    TAXABLE_HELD = (date(2025, 10, 10) - date(2025, 10, 1)).days  # 9
    TAXABLE_DAYS = TAXABLE_HELD

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = os.path.join(self.tmp.name, "data")
        ira_dir = os.path.join(self.data_dir, "ira")
        taxable_dir = os.path.join(self.data_dir, "taxable")
        os.makedirs(ira_dir)
        os.makedirs(taxable_dir)

        write_history_csv(
            os.path.join(ira_dir, "History_for_Account.csv"),
            csp_round_trip_rows("-MU250926P150", "09/19/2025", "09/25/2025", 3.35, 0.95),
        )
        write_positions_csv(
            os.path.join(ira_dir, "Portfolio_Positions.csv"),
            "111111111",
            "IRA",
            [positions_row("111111111", "IRA", "MU", 15000.0)],
            "Sep-25-2025",
        )

        write_history_csv(
            os.path.join(taxable_dir, "History_for_Account.csv"),
            csp_round_trip_rows("-MU251010P200", "10/01/2025", "10/10/2025", 5.0, 2.0),
        )
        write_positions_csv(
            os.path.join(taxable_dir, "Portfolio_Positions.csv"),
            "222222222",
            "Taxable",
            [positions_row("222222222", "Taxable", "MU", 20000.0)],
            "Oct-10-2025",
        )

        self.registry = AccountRegistry(base_dir=self.data_dir, extra_dirs=())

    def test_list_accounts(self):
        accounts = {row["id"]: row for row in self.registry.list_accounts()}
        self.assertEqual(set(accounts), {"ira", "taxable"})
        self.assertEqual(accounts["ira"]["account_number"], "111111111")
        self.assertEqual(accounts["taxable"]["account_number"], "222222222")

    def test_single_account_build_matches_direct_dashboard(self):
        payload = self.registry.build("ira")
        self.assertEqual(payload["portfolio"]["cycles"], 1)
        self.assertAlmostEqual(payload["portfolio"]["net_realized_pl"], self.IRA_PREMIUM, places=2)
        self.assertEqual(payload["meta"]["account_id"], "ira")

    def test_unknown_account_raises_keyerror(self):
        with self.assertRaises(KeyError):
            self.registry.build("does-not-exist")

    def test_single_account_csp_candidates_are_widened_across_all_accounts(self):
        import wheel.accounts as accts

        calls = []
        sentinel = [{"underlying": "SENTINEL"}]

        def fake_combine(payloads, exposure):
            calls.append(set(payloads))
            return sentinel

        with mock.patch.object(accts, "_combine_csp_candidates", side_effect=fake_combine):
            payload = self.registry.build("ira")

        self.assertEqual(payload["csp_candidates"], sentinel)  # widened list replaces the per-account one
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], {"ira", "taxable"})  # every account fed in, not just the one asked for

    def test_combined_cycles_are_concatenated_not_merged(self):
        combined = self.registry.build("combined")
        self.assertEqual(combined["portfolio"]["cycles"], 2)
        self.assertEqual(len(combined["cycles"]), 2)
        account_ids = {cycle["account_id"] for cycle in combined["cycles"]}
        self.assertEqual(account_ids, {"ira", "taxable"})
        # Both accounts' first MU cycle would share the engine's own cycle_id
        # (e.g. "MU-1") -- the combined view must disambiguate it.
        cycle_ids = [cycle["cycle_id"] for cycle in combined["cycles"]]
        self.assertEqual(len(cycle_ids), len(set(cycle_ids)))
        self.assertTrue(all(":" in cid for cid in cycle_ids))

    def test_combined_trade_log_is_concatenated_and_id_prefixed(self):
        combined = self.registry.build("combined")
        trade_log = combined["trade_log"]
        self.assertEqual(sorted(trade_log.keys()), ["warnings", "wheels"])
        self.assertEqual(len(trade_log["wheels"]), 2)
        self.assertEqual({w["account_id"] for w in trade_log["wheels"]}, {"ira", "taxable"})
        cycle_ids = [w["cycle_id"] for w in trade_log["wheels"]]
        self.assertEqual(len(cycle_ids), len(set(cycle_ids)))
        self.assertTrue(all(":" in cid for cid in cycle_ids))
        # Same tagged id the combined timeline chart emits, so click-through lines up.
        self.assertEqual(
            {w["cycle_id"] for w in trade_log["wheels"]},
            {c["cycle_id"] for c in combined["cycles"]},
        )

    def test_combined_net_worth_and_avg_days_in_trade_present(self):
        combined = self.registry.build("combined")
        # Regression guard: avg_days_in_trade must exist on the combined payload
        # (it was previously omitted, which crashed the frontend on undefined.toFixed()).
        self.assertIn("avg_days_in_trade", combined["portfolio"])
        expected_avg_days = (self.IRA_HELD * 1 + self.TAXABLE_HELD * 1) / 2
        self.assertAlmostEqual(combined["portfolio"]["avg_days_in_trade"], expected_avg_days, places=2)

        self.assertTrue(combined["net_worth"]["available"])
        self.assertAlmostEqual(
            combined["net_worth"]["combined"]["total_value"], 15000.0 + 20000.0, places=2
        )

    def test_combined_roi_is_capital_weighted_not_averaged(self):
        combined = self.registry.build("combined")
        portfolio = combined["portfolio"]

        net_pl = self.IRA_PREMIUM + self.TAXABLE_PREMIUM
        expected_avg_capital = (
            self.IRA_COLLATERAL * self.IRA_DAYS + self.TAXABLE_COLLATERAL * self.TAXABLE_DAYS
        ) / (self.IRA_DAYS + self.TAXABLE_DAYS)
        expected_roi = 100.0 * net_pl / expected_avg_capital

        # The naive average of each account's own ROI -- what a bug would produce.
        ira_roi = 100.0 * self.IRA_PREMIUM / self.IRA_COLLATERAL
        taxable_roi = 100.0 * self.TAXABLE_PREMIUM / self.TAXABLE_COLLATERAL
        naive_average_roi = (ira_roi + taxable_roi) / 2

        self.assertAlmostEqual(portfolio["avg_capital"], expected_avg_capital, places=1)
        self.assertAlmostEqual(portfolio["roi_on_avg_wheel_pct"], expected_roi, places=2)
        self.assertGreater(abs(portfolio["roi_on_avg_wheel_pct"] - naive_average_roi), 0.5)

    def test_combined_net_option_yield_and_total_position_roi_are_summed_not_averaged(self):
        """Same principle as test_combined_roi_is_capital_weighted_not_averaged,
        one level over: both dual-track fields are quoted against
        total_initial_collateral, the sum of each account's own summed
        per-cycle initial collateral -- never each account's own percentage
        averaged together.
        """
        combined = self.registry.build("combined")
        portfolio = combined["portfolio"]

        expected_total_initial = self.IRA_COLLATERAL + self.TAXABLE_COLLATERAL
        self.assertAlmostEqual(portfolio["total_initial_collateral"], expected_total_initial, places=2)

        net_pl = self.IRA_PREMIUM + self.TAXABLE_PREMIUM
        expected_net_option_yield = 100.0 * net_pl / expected_total_initial
        self.assertAlmostEqual(portfolio["net_option_yield_pct"], expected_net_option_yield, places=2)

        # No stock, no dividends, no unrealized P&L in this fixture -- both
        # of the dual-track pair's numerators collapse to the same figure.
        self.assertAlmostEqual(portfolio["total_position_roi_pct"], expected_net_option_yield, places=2)
        self.assertAlmostEqual(portfolio["dividends_received"], 0.0, places=2)
        self.assertAlmostEqual(portfolio["stock_unrealized_pl"], 0.0, places=2)

        # The naive average of each account's own yield -- close to, but not
        # exactly, the sum-weighted figure above, since these two accounts'
        # collateral sizes are similar (unlike the time-weighted-average ROC
        # test, a sum-of-collateral denominator only diverges sharply from a
        # naive average when account sizes differ a lot; the exact-value
        # assertions above are the real correctness check here).
        ira_yield = 100.0 * self.IRA_PREMIUM / self.IRA_COLLATERAL
        taxable_yield = 100.0 * self.TAXABLE_PREMIUM / self.TAXABLE_COLLATERAL
        naive_average = (ira_yield + taxable_yield) / 2
        self.assertNotAlmostEqual(portfolio["net_option_yield_pct"], naive_average, places=2)

    def test_combined_capital_series_spread_component_sums_across_accounts(self):
        """Every account's own capital_series already carries a "spread" key
        (net credit-spread collateral) -- the combined series must sum it
        like every other component, or a spread-heavy account's collateral
        would silently vanish from the Combined capital chart.
        """
        combined = self.registry.build("combined")
        for point in combined["capital_series"]:
            self.assertIn("spread", point)
            self.assertAlmostEqual(
                point["total"],
                round(point["put"] + point["stock"] + point["call"] + point["long"] + point["spread"], 2),
                places=2,
            )

    def test_refresh_picks_up_a_newly_added_account_folder(self):
        self.assertEqual(len(self.registry.list_accounts()), 2)
        new_dir = os.path.join(self.data_dir, "third")
        os.makedirs(new_dir)
        write_positions_csv(
            os.path.join(new_dir, "Portfolio_Positions.csv"),
            "333333333",
            "Third",
            [positions_row("333333333", "Third", "IVV", 5000.0)],
            "Nov-01-2025",
        )
        self.registry.refresh()
        self.assertEqual(len(self.registry.list_accounts()), 3)


class TestDuplicateAccountMerging(unittest.TestCase):
    """A folder is a convenience, not identity -- two folders reporting the
    same real account number (from their own Positions snapshot) must collapse
    into one account, never be counted twice in Combined.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = os.path.join(self.tmp.name, "data")
        ira_dir = os.path.join(self.data_dir, "ira")
        os.makedirs(self.data_dir, exist_ok=True)
        os.makedirs(ira_dir)

        history_rows = csp_round_trip_rows("-MU250926P150", "09/19/2025", "09/25/2025", 3.35, 0.95)
        position_rows = [positions_row("111111111", "Some Account", "MU", 15000.0)]

        # The same account's files, duplicated in both the root (default)
        # bucket and a named subfolder -- e.g. stray files left over from
        # before the account got its own folder.
        write_history_csv(os.path.join(self.data_dir, "History_for_Account.csv"), history_rows)
        write_positions_csv(
            os.path.join(self.data_dir, "Portfolio_Positions.csv"),
            "111111111", "Some Account", position_rows, "Sep-25-2025",
        )
        write_history_csv(os.path.join(ira_dir, "History_for_Account.csv"), history_rows)
        write_positions_csv(
            os.path.join(ira_dir, "Portfolio_Positions.csv"),
            "111111111", "Some Account", position_rows, "Sep-25-2025",
        )

        self.registry = AccountRegistry(base_dir=self.data_dir, extra_dirs=())

    def test_only_one_account_is_listed(self):
        accounts = self.registry.list_accounts()
        self.assertEqual([row["id"] for row in accounts], ["ira"])

    def test_default_is_absorbed_not_dropped(self):
        # The default bucket's data isn't lost -- its transactions/positions
        # end up inside the merged 'ira' account, deduplicated rather than summed
        # (both folders hold byte-identical duplicate files).
        payload = self.registry.build("ira")
        self.assertEqual(payload["portfolio"]["cycles"], 1)
        self.assertAlmostEqual(payload["net_worth"]["total_value"], 15000.0, places=2)

    def test_combined_does_not_double_count(self):
        combined = self.registry.build("combined")
        self.assertEqual(combined["portfolio"]["cycles"], 1)
        self.assertAlmostEqual(combined["net_worth"]["combined"]["total_value"], 15000.0, places=2)

    def test_merge_is_reported_as_a_warning(self):
        self.registry.build("combined")
        self.assertTrue(any("merged into 'ira'" in w for w in self.registry._build_warnings))

    def test_unresolved_folder_with_no_positions_file_stands_alone(self):
        # A folder with no Positions snapshot has no verifiable identity, so it
        # must never be silently merged into another account on a guess.
        other_dir = os.path.join(self.data_dir, "no_positions_yet")
        os.makedirs(other_dir)
        write_history_csv(
            os.path.join(other_dir, "History_for_Account.csv"),
            csp_round_trip_rows("-MU251010P200", "10/01/2025", "10/10/2025", 5.0, 2.0),
        )
        self.registry.refresh(force=True)
        ids = {row["id"] for row in self.registry.list_accounts()}
        self.assertEqual(ids, {"ira", "no_positions_yet"})


class TestAccountConfig(unittest.TestCase):
    """A Positions file listing more than one real account -- e.g. a Fidelity
    "all accounts" download -- is handled two ways: any account with no
    folder of its own is auto-discovered as its own positions-only account
    (no config needed), and ``data/accounts.json`` lets a folder's own
    account be named explicitly (``"folders"``) or an account be hidden
    entirely (``"ignore"``).
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = os.path.join(self.tmp.name, "data")
        self.ira_dir = os.path.join(self.data_dir, "ira")
        os.makedirs(self.ira_dir)

        write_history_csv(
            os.path.join(self.ira_dir, "History_for_Account.csv"),
            csp_round_trip_rows("-MU250926P150", "09/19/2025", "09/25/2025", 3.35, 0.95),
        )
        # A combined "all accounts" download: this folder's own account
        # (111111111) plus a second, unrelated linked account (222222222)
        # that happens to be included in the same file and has no folder of
        # its own.
        write_positions_csv(
            os.path.join(self.ira_dir, "Portfolio_Positions.csv"),
            "111111111", "IRA",
            [
                positions_row("111111111", "IRA", "MU", 15000.0),
                positions_row("222222222", "Roth IRA", "AAPL", 8000.0),
            ],
            "Sep-25-2025",
        )

    def _write_config(self, config: dict) -> None:
        with open(os.path.join(self.data_dir, "accounts.json"), "w", encoding="utf-8") as handle:
            json.dump(config, handle)

    def test_load_account_config_missing_file(self):
        config, warnings = load_account_config(self.data_dir)
        self.assertEqual(config.folders, {})
        self.assertEqual(config.ignore, [])
        self.assertEqual(warnings, [])

    def test_load_account_config_malformed_json(self):
        with open(os.path.join(self.data_dir, "accounts.json"), "w", encoding="utf-8") as handle:
            handle.write("{not valid json")
        config, warnings = load_account_config(self.data_dir)
        self.assertEqual(config.folders, {})
        self.assertEqual(len(warnings), 1)

    def test_second_account_is_auto_discovered_without_any_config(self):
        registry = AccountRegistry(base_dir=self.data_dir, extra_dirs=())
        accounts = {row["id"]: row for row in registry.list_accounts()}
        self.assertIn("ira", accounts)
        self.assertIn("roth-ira", accounts)
        self.assertEqual(accounts["roth-ira"]["account_number"], "222222222")
        self.assertEqual(accounts["roth-ira"]["account_name"], "Roth IRA")
        self.assertEqual(accounts["roth-ira"]["transactions"], 0)

    def test_ira_folder_is_scoped_to_its_own_account_only(self):
        registry = AccountRegistry(base_dir=self.data_dir, extra_dirs=())
        payload = registry.build("ira")
        self.assertEqual(payload["net_worth"]["account_number"], "111111111")
        self.assertAlmostEqual(payload["net_worth"]["total_value"], 15000.0, places=2)
        self.assertTrue(
            any("222222222" in w and "111111111" in w for w in payload["net_worth"]["warnings"])
        )

    def test_auto_discovered_account_has_correct_net_worth_and_no_cycles(self):
        registry = AccountRegistry(base_dir=self.data_dir, extra_dirs=())
        payload = registry.build("roth-ira")
        self.assertAlmostEqual(payload["net_worth"]["total_value"], 8000.0, places=2)
        self.assertEqual(payload["portfolio"]["cycles"], 0)

    def test_ignore_by_account_name_hides_the_account_everywhere(self):
        self._write_config({"ignore": ["Roth IRA"]})
        registry = AccountRegistry(base_dir=self.data_dir, extra_dirs=())
        ids = {row["id"] for row in registry.list_accounts()}
        self.assertNotIn("roth-ira", ids)
        combined = registry.build("combined")
        self.assertAlmostEqual(combined["net_worth"]["combined"]["total_value"], 15000.0, places=2)

    def test_ignore_by_account_number(self):
        self._write_config({"ignore": ["222222222"]})
        registry = AccountRegistry(base_dir=self.data_dir, extra_dirs=())
        ids = {row["id"] for row in registry.list_accounts()}
        self.assertNotIn("roth-ira", ids)

    def test_folders_config_widens_search_to_a_shared_positions_file(self):
        # A folder with its own transaction history but no local Positions
        # file at all -- its account only ever appears in a shared download
        # that physically lives inside a different folder.
        joint_dir = os.path.join(self.data_dir, "joint")
        os.makedirs(joint_dir)
        write_history_csv(
            os.path.join(joint_dir, "History_for_Account.csv"),
            csp_round_trip_rows("-MU251010P200", "10/01/2025", "10/10/2025", 5.0, 2.0),
        )
        self._write_config({"folders": {"joint": "222222222"}})
        registry = AccountRegistry(base_dir=self.data_dir, extra_dirs=())
        payload = registry.build("joint")
        self.assertAlmostEqual(payload["net_worth"]["total_value"], 8000.0, places=2)
        self.assertEqual(payload["portfolio"]["cycles"], 1)
        # 222222222 is now claimed by the 'joint' folder -- it must not also
        # appear as a separate auto-discovered account.
        ids = {row["id"] for row in registry.list_accounts()}
        self.assertNotIn("roth-ira", ids)

    def test_editing_config_alone_triggers_refresh(self):
        registry = AccountRegistry(base_dir=self.data_dir, extra_dirs=())
        self.assertIn("roth-ira", {row["id"] for row in registry.list_accounts()})
        self._write_config({"ignore": ["Roth IRA"]})
        registry.refresh()
        self.assertNotIn("roth-ira", {row["id"] for row in registry.list_accounts()})

    def test_default_account_is_exposed_when_valid(self):
        self._write_config({"default_account": "ira"})
        registry = AccountRegistry(base_dir=self.data_dir, extra_dirs=())
        self.assertEqual(registry.default_account_id, "ira")

    def test_unknown_default_account_falls_back_to_combined_with_a_warning(self):
        self._write_config({"default_account": "does-not-exist"})
        registry = AccountRegistry(base_dir=self.data_dir, extra_dirs=())
        self.assertIsNone(registry.default_account_id)
        self.assertTrue(any("default_account" in w for w in registry._build_warnings))

    def test_no_default_account_configured(self):
        registry = AccountRegistry(base_dir=self.data_dir, extra_dirs=())
        self.assertIsNone(registry.default_account_id)

    def test_default_account_accepts_an_account_number(self):
        # A user copies the number straight out of Fidelity -- they shouldn't
        # have to know that auto-discovery (or a folder) landed it under some
        # particular id/slug.
        self._write_config({"default_account": "111111111"})
        registry = AccountRegistry(base_dir=self.data_dir, extra_dirs=())
        self.assertEqual(registry.default_account_id, "ira")

    def test_folder_account_matches_case_insensitively(self):
        # data/<folder>/ casing is whatever the OS happened to create; a
        # config entry shouldn't have to match it exactly.
        config = AccountConfig(folders={"IRA": "111111111"})
        self.assertEqual(config.folder_account("ira"), "111111111")
        self.assertEqual(config.folder_account("IRA"), "111111111")
        self.assertIsNone(config.folder_account("other"))

    def test_default_range_is_exposed_when_valid(self):
        self._write_config({"default_range": "ytd"})
        registry = AccountRegistry(base_dir=self.data_dir, extra_dirs=())
        self.assertEqual(registry.default_range, "ytd")

    def test_default_range_accepts_a_specific_year_and_a_day_count(self):
        self._write_config({"default_range": "year:2025"})
        registry = AccountRegistry(base_dir=self.data_dir, extra_dirs=())
        self.assertEqual(registry.default_range, "year:2025")

        self._write_config({"default_range": "30"})
        registry.refresh(force=True)
        self.assertEqual(registry.default_range, "30")

    def test_invalid_default_range_is_rejected_with_a_warning(self):
        self._write_config({"default_range": "last week"})
        registry = AccountRegistry(base_dir=self.data_dir, extra_dirs=())
        self.assertIsNone(registry.default_range)
        self.assertTrue(any("default_range" in w for w in registry._build_warnings))

    def test_no_default_range_configured(self):
        registry = AccountRegistry(base_dir=self.data_dir, extra_dirs=())
        self.assertIsNone(registry.default_range)

    def test_opening_balance_lookup_matches_case_insensitively(self):
        config = AccountConfig(opening_balances={"IRA": (date(2024, 1, 1), 50000.0)})
        self.assertEqual(config.opening_balance("ira"), (date(2024, 1, 1), 50000.0))
        self.assertEqual(config.opening_balance("IRA"), (date(2024, 1, 1), 50000.0))
        self.assertIsNone(config.opening_balance("other"))

    def test_opening_balances_loaded_from_json(self):
        self._write_config({"opening_balances": {"ira": {"date": "2024-01-01", "balance": 50000}}})
        config, warnings = load_account_config(self.data_dir)
        self.assertEqual(config.opening_balances, {"ira": (date(2024, 1, 1), 50000.0)})
        self.assertEqual(warnings, [])

    def test_opening_balances_invalid_entries_are_skipped_with_a_warning(self):
        self._write_config(
            {
                "opening_balances": {
                    "ira": {"date": "not-a-date", "balance": 50000},
                    "roth-ira": {"date": "2024-01-01", "balance": "fifty thousand"},
                    "other": "not an object",
                }
            }
        )
        config, warnings = load_account_config(self.data_dir)
        self.assertEqual(config.opening_balances, {})
        self.assertEqual(len(warnings), 3)

    def test_opening_balance_unblocks_benchmark_with_only_one_real_snapshot(self):
        """The 'ira' folder's own Positions file (see setUp) has exactly one
        snapshot -- not enough on its own to compute a benchmark return. A
        configured opening_balances entry dated before it must unblock the
        comparison without needing a second real Positions export.
        """
        registry = AccountRegistry(base_dir=self.data_dir, extra_dirs=())
        baseline = registry.build("ira")
        self.assertFalse(baseline["benchmark"]["available"])

        self._write_config({"opening_balances": {"ira": {"date": "2025-01-01", "balance": 10000}}})
        registry.refresh(force=True)
        payload = registry.build("ira")

        self.assertTrue(payload["benchmark"]["available"])
        opening = payload["benchmark"]["cash_flow_events"][0]
        self.assertEqual(opening["label"], "Opening balance (configured in accounts.json)")
        self.assertEqual(opening["date"], "2025-01-01")
        self.assertAlmostEqual(opening["amount"], 10000.0, places=2)

    def test_opening_balance_is_ignored_when_not_earlier_than_the_real_snapshot(self):
        """A configured date on or after the one real snapshot on file (Sep
        25, 2025 -- see setUp) adds nothing a real snapshot doesn't already
        have, and must not paper over "not enough snapshots yet".
        """
        self._write_config({"opening_balances": {"ira": {"date": "2025-12-01", "balance": 10000}}})
        registry = AccountRegistry(base_dir=self.data_dir, extra_dirs=())
        payload = registry.build("ira")
        self.assertFalse(payload["benchmark"]["available"])


class TestCombinedWheelState(unittest.TestCase):
    """Two accounts, each contributing a different wheel-state bucket, so the
    Combined view has to sum them rather than pick one or overwrite the other.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = os.path.join(self.tmp.name, "data")
        ira_dir = os.path.join(self.data_dir, "ira")
        taxable_dir = os.path.join(self.data_dir, "taxable")
        os.makedirs(ira_dir)
        os.makedirs(taxable_dir)

        # ira: a lone open CSP, still active -- Cash-Secured Puts bucket.
        write_history_csv(
            os.path.join(ira_dir, "History_for_Account.csv"),
            [
                '09/19/2025,"YOU SOLD OPENING TRANSACTION PUT (MU) ...",-MU250926P150,'
                '"PUT ...",Cash,-1,3.35,0,0,,334.33,10000.00,09/19/2025',
                # Keeps this account's own "through" resolving after the filter
                # window below (Nov 1) even though the CSP itself opened
                # earlier -- exactly the real scenario: some later ledger
                # activity anchors "through" into the window while the
                # position that matters predates it and is still active.
                '11/15/2025,"DIVIDEND RECEIVED MU",MU,"MU DESCRIPTION",Cash,,,0,0,,5.00,10005.00,11/15/2025',
            ],
        )
        write_positions_csv(
            os.path.join(ira_dir, "Portfolio_Positions.csv"),
            "111111111",
            "IRA",
            [positions_row("111111111", "IRA", "MU", 15000.0)],
            "Sep-19-2025",
        )

        # taxable: assigned shares with an open covered call -- Covered Calls
        # bucket carries the shares' cost basis (the whole point being tested:
        # this cycle's stock capital must NOT land in Holding Shares).
        write_history_csv(
            os.path.join(taxable_dir, "History_for_Account.csv"),
            [
                '10/01/2025,"YOU SOLD OPENING TRANSACTION PUT (WFC) ...",-WFC251010P50,'
                '"PUT ...",Cash,-1,2.00,0,0,,200.00,20000.00,10/01/2025',
                '10/10/2025,"ASSIGNED PUT as of Oct-10-2025",-WFC251010P50,"PUT ...",'
                "Cash,1,,0,0,,0.00,19800.00,10/10/2025",
                '10/15/2025,"YOU SOLD OPENING TRANSACTION CALL (WFC) ...",-WFC251101C55,'
                '"CALL ...",Cash,-1,1.50,0,0,,150.00,19950.00,10/15/2025',
                # Same day as ira's dividend below: keeps both accounts'
                # independently-computed "through" aligned, so the combined
                # capital series (bucketed by exact date, see
                # _combine_capital_series) has both accounts represented on
                # its last day. Misaligned per-account "through" dates are a
                # separate, pre-existing concern in the combine layer, not
                # something this fix touches.
                '11/15/2025,"DIVIDEND RECEIVED WFC",WFC,"WFC DESCRIPTION",Cash,,,0,0,,3.00,19953.00,11/15/2025',
            ],
        )
        write_positions_csv(
            os.path.join(taxable_dir, "Portfolio_Positions.csv"),
            "222222222",
            "Taxable",
            [positions_row("222222222", "Taxable", "WFC", 5000.0)],
            "Oct-15-2025",
        )

        self.registry = AccountRegistry(base_dir=self.data_dir, extra_dirs=())

    def test_combined_wheel_state_sums_across_accounts(self):
        combined = self.registry.build("combined")
        buckets = combined["wheel_state"]["buckets"]

        self.assertAlmostEqual(buckets["puts"]["amount"], 15000.0, places=2)  # ira's CSP
        self.assertEqual(buckets["puts"]["tickers"], ["MU"])

        self.assertAlmostEqual(buckets["calls"]["amount"], 5000.0, places=2)  # taxable's shares
        self.assertEqual(buckets["calls"]["tickers"], ["WFC"])
        self.assertAlmostEqual(buckets["holding"]["amount"], 0.0, places=2)

        self.assertEqual(combined["wheel_state"]["active_cycles"], 2)

    def test_single_account_wheel_state_matches_its_own_slice(self):
        ira_only = self.registry.build("ira")
        self.assertAlmostEqual(ira_only["wheel_state"]["buckets"]["puts"]["amount"], 15000.0, places=2)
        self.assertEqual(ira_only["wheel_state"]["active_cycles"], 1)

        taxable_only = self.registry.build("taxable")
        self.assertAlmostEqual(taxable_only["wheel_state"]["buckets"]["calls"]["amount"], 5000.0, places=2)
        self.assertEqual(taxable_only["wheel_state"]["active_cycles"], 1)

    def test_wheel_state_capital_matches_capital_deployed_now_under_a_date_filter(self):
        """The regression this whole feature exists to guard: filtering to a
        window that starts after both positions opened must not change either
        figure -- both are state snapshots, not window-scoped event counts.
        """
        from wheel.accounts import Filters

        filtered = self.registry.build("combined", Filters(start=date(2025, 11, 1)))
        buckets = filtered["wheel_state"]["buckets"]
        total = sum(bucket["amount"] for bucket in buckets.values())
        self.assertAlmostEqual(total, filtered["portfolio"]["capital_deployed_now"], places=2)
        self.assertAlmostEqual(total, 20000.0, places=2)  # unchanged from the unfiltered view


class TestReservedAccountIds(unittest.TestCase):
    """A folder or auto-discovered account must never end up registered
    under the reserved "combined" id -- build() would silently route every
    request for it to the aggregate view instead of its own dashboard.
    """

    def test_folder_named_combined_is_disambiguated(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = os.path.join(tmp, "data")
            combined_dir = os.path.join(data_dir, "combined")
            os.makedirs(combined_dir)
            write_history_csv(
                os.path.join(combined_dir, "History_for_Account.csv"),
                csp_round_trip_rows("-MU250926P150", "09/19/2025", "09/25/2025", 3.35, 0.95),
            )
            write_positions_csv(
                os.path.join(combined_dir, "Portfolio_Positions.csv"),
                "111111111", "Some Account",
                [positions_row("111111111", "Some Account", "MU", 15000.0)],
                "Sep-25-2025",
            )

            registry = AccountRegistry(base_dir=data_dir, extra_dirs=())
            ids = {row["id"] for row in registry.list_accounts()}
            self.assertNotIn("combined", ids)
            # Renamed, not dropped -- the account's own data is still reachable.
            renamed = next(i for i in ids if i.startswith("combined-"))
            payload = registry.build(renamed)
            self.assertEqual(payload["portfolio"]["cycles"], 1)
            # "combined" itself still means the aggregate view, not this account.
            self.assertTrue(registry.build("combined")["meta"]["combined"])


class TestLiveDefaultDashboardMerge(unittest.TestCase):
    """When the default bucket's own account number matches a named folder's,
    the live (externally managed, e.g. just-uploaded) default Dashboard must
    win -- not get silently discarded in favor of a fresh rebuild from disk,
    and the named folder must not also register separately (which would
    double-count the same real account in Combined).
    """

    def test_default_dashboard_identity_is_preserved_on_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = os.path.join(tmp, "data")
            ira_dir = os.path.join(data_dir, "ira")
            os.makedirs(ira_dir)

            history_rows = csp_round_trip_rows("-MU250926P150", "09/19/2025", "09/25/2025", 3.35, 0.95)
            position_rows = [positions_row("111111111", "Some Account", "MU", 15000.0)]

            default_history = os.path.join(data_dir, "History_for_Account.csv")
            default_positions = os.path.join(data_dir, "Portfolio_Positions.csv")
            write_history_csv(default_history, history_rows)
            write_positions_csv(default_positions, "111111111", "Some Account", position_rows, "Sep-25-2025")
            write_history_csv(os.path.join(ira_dir, "History_for_Account.csv"), history_rows)
            write_positions_csv(
                os.path.join(ira_dir, "Portfolio_Positions.csv"),
                "111111111", "Some Account", position_rows, "Sep-25-2025",
            )

            registry = AccountRegistry(base_dir=data_dir, extra_dirs=())
            live_dashboard = Dashboard([default_history], position_paths=[default_positions])
            registry.set_default_dashboard(live_dashboard)

            self.assertIs(registry.get(DEFAULT_ACCOUNT_ID), live_dashboard)
            # 'ira' shares the same account number -- it must not also show
            # up as its own separate account.
            ids = {row["id"] for row in registry.list_accounts()}
            self.assertEqual(ids, {DEFAULT_ACCOUNT_ID})

            combined = registry.build("combined")
            self.assertEqual(combined["portfolio"]["cycles"], 1)


class TestDefaultAccountCaseInsensitive(unittest.TestCase):
    def test_default_account_matches_folder_casing_insensitively(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = os.path.join(tmp, "data")
            joint_dir = os.path.join(data_dir, "Joint")
            os.makedirs(joint_dir)
            write_history_csv(
                os.path.join(joint_dir, "History_for_Account.csv"),
                csp_round_trip_rows("-MU250926P150", "09/19/2025", "09/25/2025", 3.35, 0.95),
            )
            write_positions_csv(
                os.path.join(joint_dir, "Portfolio_Positions.csv"),
                "111111111", "Joint Account",
                [positions_row("111111111", "Joint Account", "MU", 15000.0)],
                "Sep-25-2025",
            )
            with open(os.path.join(data_dir, "accounts.json"), "w", encoding="utf-8") as handle:
                json.dump({"default_account": "JOINT"}, handle)

            registry = AccountRegistry(base_dir=data_dir, extra_dirs=())
            self.assertEqual(registry.default_account_id, "Joint")


class TestCombineBenchmarkDateAlignment(unittest.TestCase):
    """Each account's own benchmark series only has a point on the dates it
    happened to snapshot -- summing by exact date match silently drops (or
    understates) every day two accounts' Positions exports weren't taken on
    the same day, which is the common case. The combined series must
    forward-fill each account's last-known value instead.
    """

    @staticmethod
    def _benchmark_payload(series):
        return {
            "available": True,
            "warnings": [],
            "as_of": series[-1]["as_of"],
            "cash_flow_events": [
                {"date": "2026-01-01", "amount": 10000.0, "label": "Opening balance", "kind": "OPENING_BALANCE"}
            ],
            "actual": {"terminal_value": series[-1]["actual_value"], "xirr_pct": None},
            "benchmark": {"name": "SPY", "terminal_value": series[-1]["benchmark_value"], "xirr_pct": None},
            "value_added": None,
            "series": series,
        }

    def test_combined_series_forward_fills_across_misaligned_snapshot_dates(self):
        from wheel.accounts import _combine_benchmark

        payloads = {
            "a": {
                "benchmark": self._benchmark_payload(
                    [{"as_of": "2026-06-01", "actual_value": 50000.0, "benchmark_value": 48000.0}]
                )
            },
            "b": {
                "benchmark": self._benchmark_payload(
                    [{"as_of": "2026-07-15", "actual_value": 30000.0, "benchmark_value": 29000.0}]
                )
            },
        }

        combined = _combine_benchmark(payloads)
        self.assertTrue(combined["available"])
        series = {point["as_of"]: point for point in combined["series"]}

        # Both dates are represented -- not just whichever account happened
        # to snapshot that exact day.
        self.assertEqual(set(series), {"2026-06-01", "2026-07-15"})
        # 'b' has no snapshot yet as of 'a's date -- only 'a' contributes.
        self.assertAlmostEqual(series["2026-06-01"]["actual_value"], 50000.0, places=2)
        # By 'b's date, 'a's last-known value (50000) is forward-filled and
        # summed with 'b's own real reading -- never silently just 30000.
        self.assertAlmostEqual(series["2026-07-15"]["actual_value"], 80000.0, places=2)
        self.assertAlmostEqual(series["2026-07-15"]["benchmark_value"], 48000.0 + 29000.0, places=2)

    def test_multiple_indices_are_summed_and_carried_through(self):
        from wheel.accounts import _combine_benchmark

        def payload(actual, spy, qqq):
            base = self._benchmark_payload(
                [{"as_of": "2026-07-01", "actual_value": actual, "benchmark_value": spy}]
            )
            base["series"][0]["benchmark_value_qqq"] = qqq
            base["benchmarks"] = [
                {"name": "SPY", "terminal_value": spy, "xirr_pct": None, "value_added": None},
                {"name": "QQQ", "terminal_value": qqq, "xirr_pct": None, "value_added": None},
            ]
            base["benchmark"] = base["benchmarks"][0]
            return {"benchmark": base}

        combined = _combine_benchmark({"a": payload(50000.0, 48000.0, 52000.0),
                                       "b": payload(30000.0, 29000.0, 31000.0)})
        names = [e["name"] for e in combined["benchmarks"]]
        self.assertEqual(names, ["SPY", "QQQ"])
        self.assertAlmostEqual(combined["benchmarks"][1]["terminal_value"], 83000.0, places=2)
        point = combined["series"][0]
        self.assertAlmostEqual(point["benchmark_value"], 77000.0, places=2)
        self.assertAlmostEqual(point["benchmark_value_qqq"], 83000.0, places=2)
        # Legacy key still points at the primary index
        self.assertEqual(combined["benchmark"]["name"], "SPY")


if __name__ == "__main__":
    unittest.main()
