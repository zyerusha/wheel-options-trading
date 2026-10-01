"""Workspace isolation: a session-scoped DashboardState/AccountRegistry must
never discover, dedup against, or read from anything outside its own
directories -- the property hosted mode's per-tenant isolation depends on.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wheel.accounts as accounts  # noqa: E402
from wheel.accounts import AccountRegistry  # noqa: E402
from wheel.serve import DashboardState, Workspace  # noqa: E402

HISTORY_HEADER = (
    "Run Date,Action,Symbol,Description,Type,Quantity,Price ($),Commission ($),"
    "Fees ($),Accrued Interest ($),Amount ($),Cash Balance ($),Settlement Date"
)
ONE_TRADE_ROW = (
    '09/19/2025,"YOU SOLD OPENING TRANSACTION PUT (MU) ...",-MU250926P150,'
    '"PUT ...",Cash,-1,3.35,0,0,,335.00,10000.00,09/19/2025'
)


def _write_history_csv(path: str, rows: list[str] = (ONE_TRADE_ROW,)) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        handle.write(HISTORY_HEADER + "\n")
        handle.write("\n".join(rows) + ("\n" if rows else ""))


class TestWorkspaceIsolation(unittest.TestCase):
    """A Workspace built with extra_dirs=() (hosted mode's shape) must never
    see a file that lives outside its own base_dir -- even one that a
    same-process Option-B Workspace would happily pick up.
    """

    def setUp(self):
        self.session_dir = tempfile.TemporaryDirectory()
        self.outside_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.session_dir.cleanup)
        self.addCleanup(self.outside_dir.cleanup)

    def test_session_scoped_state_ignores_files_outside_its_own_base_dir(self):
        _write_history_csv(os.path.join(self.outside_dir.name, "History_for_Account.csv"))

        state = DashboardState([], upload_dir=self.session_dir.name, extra_dirs=())
        self.assertEqual(state.search_paths(), [])

    def test_session_scoped_state_finds_files_inside_its_own_base_dir(self):
        own = os.path.join(self.session_dir.name, "History_for_Account.csv")
        _write_history_csv(own)

        state = DashboardState([], upload_dir=self.session_dir.name, extra_dirs=())
        self.assertEqual(state.search_paths(), [os.path.abspath(own)])

    def test_workspace_build_with_no_extra_dirs_matches_state_directly(self):
        _write_history_csv(os.path.join(self.session_dir.name, "History_for_Account.csv"))
        _write_history_csv(os.path.join(self.outside_dir.name, "Other_Account.csv"))

        workspace = Workspace.build([], base_dir=self.session_dir.name, extra_dirs=())
        names = {os.path.basename(p) for p in workspace.state.search_paths()}
        self.assertEqual(names, {"History_for_Account.csv"})

    def test_local_mode_workspace_still_merges_extra_dirs(self):
        """The Option-B shape (extra_dirs=(PROJECT_ROOT,)-equivalent) is the
        one existing behavior this refactor must not narrow."""
        _write_history_csv(os.path.join(self.session_dir.name, "History_for_Account.csv"))
        _write_history_csv(os.path.join(self.outside_dir.name, "Other_Account.csv"))

        workspace = Workspace.build([], base_dir=self.session_dir.name, extra_dirs=(self.outside_dir.name,))
        names = {os.path.basename(p) for p in workspace.state.search_paths()}
        self.assertEqual(names, {"History_for_Account.csv", "Other_Account.csv"})


class TestAccountRegistryClosedLotsScoping(unittest.TestCase):
    """Regression guard: AccountRegistry._realized_gains must scan only this
    registry's own base_dir/extra_dirs, not whatever wheel.closed_lots'
    global default directories happen to be -- otherwise a session-scoped
    registry would read another session's (or the host's own) closed-lots
    export.
    """

    def setUp(self):
        self.base_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.base_dir.cleanup)

    def test_realized_gains_scans_only_this_registrys_own_dirs(self):
        _write_history_csv(os.path.join(self.base_dir.name, "History_for_Account.csv"))
        registry = AccountRegistry(base_dir=self.base_dir.name, extra_dirs=())

        captured: list[tuple] = []

        def fake_discover_closed_lots(directories):
            captured.append(tuple(directories))
            return []

        with patch.object(accounts, "discover_closed_lots", fake_discover_closed_lots):
            registry.build(None)

        self.assertTrue(captured, "discover_closed_lots was never called")
        for directories in captured:
            self.assertEqual(set(directories), set(registry._scan_dirs))
            # Never the global DISCOVERY_DIRS-shaped default (".", DATA_DIR) --
            # this registry's own tmp base_dir is the only thing scanned.
            self.assertNotIn(".", directories)


if __name__ == "__main__":
    unittest.main()
