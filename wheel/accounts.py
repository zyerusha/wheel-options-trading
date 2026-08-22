"""Multi-account discovery and the "Combined" aggregate view.

Fidelity transaction-history exports carry no account column (see
``docs/DESIGN.md``, "Combining exports"), so the only reliable way to tell two
accounts' trades apart is to keep their files apart on disk. Each immediate
subdirectory of ``data/`` is therefore treated as one account's own files --
its transaction history *and* its Portfolio Positions snapshots -- and gets its
own :class:`~wheel.api.Dashboard`, built purely from what's in that folder.
Loose files sitting directly in ``data/``/``.`` (today's layout) are grouped as
one more, implicit ``"default"`` account, so nothing already in the repo has to
be reorganized.

Combining accounts into a "Combined" view is *not* the same operation as
combining several exports of the *same* account (``wheel.parser.parse_exports``)
-- two accounts independently wheeling the same ticker must not be merged into
one cycle. So :class:`AccountRegistry` builds each account's Dashboard
independently and aggregates their already-built JSON payloads one level up:
cycles/tickers are concatenated and tagged with the account they came from,
day-keyed series (capital, P/L) are summed by date, and portfolio-level ROI/ROC
are recomputed from the combined absolutes -- never averaged from each
account's own percentage, which would weight a small account the same as a
large one (the same principle ``wheel.metrics.portfolio_metrics`` already
applies one level down, going from cycles to the portfolio).

**Folder identity is a convenience, not the ground truth.** Two folders can
describe the very same real account -- e.g. stray files left in ``data/``
root from before an account got its own subfolder, or an upload that landed
in the default bucket while a named account was only selected in the UI, not
via ``X-Account``. Left alone, that would show up as two accounts with the
same name and quietly double-count real money in Combined. So ``refresh()``
groups folders by the account number their *own* Positions snapshot reports
(never by folder name) before building anything, and merges any group with
more than one folder into a single Dashboard -- preferring a named folder's
identity over the default bucket's. A folder with no Positions snapshot yet
has no verifiable identity and is never merged on a guess.

**The reverse case -- one Positions file naming more than one real account --
is handled by auto-discovery, not just a heuristic.** A Fidelity "all
accounts" download lists every linked account's positions in a single CSV --
often many more accounts than there are folders under ``data/``, since not
every account has (or needs) its own transaction-history folder. So
``refresh()`` doesn't stop at one Dashboard per folder: after resolving each
folder's own account number (config or heuristic, as above), it scans every
Positions file found anywhere under ``data/`` for account numbers that no
folder claimed, and gives each of *those* its own positions-only Dashboard
too (no transaction/cycle history -- there's no column to attribute it by --
just Net Worth and holdings). This is what makes "every real account shows up
somewhere" true without requiring a folder per account.

Three knobs in the optional ``data/accounts.json`` shape this:

* ``"folders"`` -- ``{"<folder>": "<account number>", ...}`` -- names a
  folder's account explicitly instead of leaving it to the "whichever
  snapshot was seen most recently" heuristic, which can pick the wrong
  account when a folder's own Positions file (or the shared "all accounts"
  one) lists several. A configured folder's Dashboard also widens its search
  to *every* discovered Positions file, not just its own folder's -- since a
  folder can have transaction history of its own while its Positions data
  only ever appears in someone else's "all accounts" download.
* ``"ignore"`` -- ``["<account number or name>", ...]`` -- drops an account
  entirely (from its own tab and from Combined), by either its account number
  or its ``Account name`` exactly as Fidelity reports it. For a linked account
  that isn't worth tracking here (e.g. a robo-advisor sleeve), this is the
  only way to hide it, since auto-discovery otherwise surfaces every account
  a Positions file mentions.
* ``"default_account"`` -- an account id (a folder name, or an
  auto-discovered slug like ``"roth-ira"``) *or* an account number -- which
  account tab the UI opens to, in place of "Combined"
  (``AccountRegistry.default_account_id``, surfaced to the frontend via
  ``/api/accounts``). Accepting the number too means it can be copied
  straight out of Fidelity without knowing which id auto-discovery landed
  on. Not to be confused with the ``"default"`` *folder* id
  (:data:`DEFAULT_ACCOUNT_ID`) -- this can name any known account, including
  a merged/configured one. A value that resolves to neither a known id nor a
  known number is ignored, with a warning, rather than left to silently
  produce an empty dashboard.
* ``"default_range"`` -- one of ``"all"``, ``"ytd"``, ``"1y"``/``"3y"``/``"5y"``,
  a specific calendar year (``"year:2025"``), or a bare day count -- the date
  range the UI opens to, in place of "All" (``AccountRegistry.default_range``,
  also surfaced via ``/api/accounts``). These are exactly the presets
  ``wheel/static/app.js``'s ``#preset`` select already understands
  (``applyPreset``), so a value outside that set is rejected up front with a
  warning rather than reaching the frontend as an unparsable range.
* ``"opening_balances"`` -- ``{"<folder>": {"date": "YYYY-MM-DD", "balance":
  <number>}, ...}`` -- a manual starting point for the S&P 500 benchmark
  comparison (``Dashboard._build_benchmark()``), keyed the same way as
  ``"folders"``. That comparison needs two Portfolio Positions snapshots,
  taken on different dates, to have both an opening and a terminal value to
  measure a return between -- an account with only one snapshot on file (the
  common case for a while after first setting this up) can't compute one yet.
  An entry here stands in for the missing earlier snapshot: "the account was
  worth this much on this date" (from your own records -- a statement, a
  memory, whatever you trust), letting the comparison run against one real
  snapshot instead of two. It's also honored ahead of a real snapshot when
  its date is *earlier* than the earliest one on file, stretching the
  comparison window further back than your Positions export history alone
  would allow. Never used for anything but the benchmark comparison --
  Total value, Capital deployed, and every other current-state figure still
  come only from real, itemized Positions data.

See :func:`load_account_config` and :class:`AccountConfig`. Whichever way an
account number is resolved, ``Dashboard.__init__``'s ``account_number``
argument does the actual filtering: rows for any other account found in the
same physical file(s) are dropped (with a warning), never blended in.

**A multi-account *transaction-history* export -- e.g. Fidelity's
"Accounts_History.csv" download -- is a different problem, not yet solved,
and is deliberately kept out of import rather than parsed wrong.** Unlike
Positions, ``History_for_Account_*.csv`` never carries an account column, and
this combined download's other column names don't match this parser's
expected header either (``Amount`` vs. ``Amount ($)``, etc.), so importing it
would silently zero out every dollar amount. ``wheel.api.looks_like_export``
excludes it outright (see ``looks_like_multi_account_export``), and
``refresh()`` scans for it anyway just to warn that it was seen and skipped,
rather than have it vanish with no explanation.
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Any, Sequence

from wheel import benchmark as bm
from wheel import cashflow as cf
from wheel.api import Dashboard, Filters, discover_exports, discover_multi_account_exports
from wheel.metrics import roi_and_annualized, time_weighted_average
from wheel.positions import discover_position_snapshots, latest_snapshot, latest_snapshot_per_account, load_snapshots

if TYPE_CHECKING:  # pragma: no cover
    pass

DEFAULT_ACCOUNT_ID = "default"
# The Combined-aggregate-view sentinel -- a real account id or number can
# never equal this (see _unique_account_id/build() below), the same
# reservation DEFAULT_ACCOUNT_ID already gets. A single shared constant
# rather than the raw string repeated at every comparison site (here, in
# wheel/serve.py, and in wheel/static/app.js) so a typo'd comparison fails
# fast (NameError/ReferenceError) instead of silently falling through to
# per-account lookup.
COMBINED_ACCOUNT_ID = "combined"
ACCOUNT_CONFIG_FILENAME = "accounts.json"

# Every preset wheel/static/app.js's #preset select understands: "all", "ytd",
# the fixed lookback windows, a specific calendar year ("year:2025"), or a
# bare day count. Validated here so a typo in the config produces a clear
# warning instead of the frontend computing an "Invalid Date" range from it.
_VALID_DEFAULT_RANGE_RE = re.compile(r"^(all|ytd|1y|3y|5y|year:\d{4}|\d+)$")


@dataclass(frozen=True)
class AccountDir:
    """One account's own directory and the files discovered in it."""

    id: str
    path: str
    history_paths: list[str] = field(default_factory=list)
    position_paths: list[str] = field(default_factory=list)


def discover_account_dirs(
    base_dir: str = "data", extra_dirs: Sequence[str] = (".",)
) -> list[AccountDir]:
    """Every account: the implicit default bucket, plus one per subfolder.

    Discovery within each directory is non-recursive -- a subfolder of ``data/``
    is scanned for its own files only, never into further nesting, so accounts
    can't accidentally absorb each other's files.
    """
    default_dirs = [directory for directory in (base_dir, *extra_dirs) if os.path.isdir(directory)]
    dirs = [
        AccountDir(
            id=DEFAULT_ACCOUNT_ID,
            path=base_dir,
            history_paths=discover_exports(default_dirs) if default_dirs else [],
            position_paths=discover_position_snapshots(default_dirs) if default_dirs else [],
        )
    ]

    if os.path.isdir(base_dir):
        for entry in sorted(os.listdir(base_dir)):
            path = os.path.join(base_dir, entry)
            if not os.path.isdir(path):
                continue
            history_paths = discover_exports([path])
            position_paths = discover_position_snapshots([path])
            if not history_paths and not position_paths:
                continue
            dirs.append(
                AccountDir(id=entry, path=path, history_paths=history_paths, position_paths=position_paths)
            )

    return dirs


def _lookup_ci(mapping: dict[str, Any], key: str) -> Any | None:
    """``mapping[key]``, matched case-insensitively -- exact match first (the
    common case, and the only one that's O(1)), then a lowercase-key scan.

    Shared by every case-insensitive config lookup in this module (folder
    names, and account ids that may equally be folder names) rather than each
    re-implementing the same "Windows treats filenames case-insensitively"
    exact-then-scan fallback by hand.
    """
    if key in mapping:
        return mapping[key]
    key_lower = key.lower()
    for candidate, value in mapping.items():
        if candidate.lower() == key_lower:
            return value
    return None


@dataclass(frozen=True)
class AccountConfig:
    """Parsed ``data/accounts.json``. Every field is optional and defaults empty."""

    folders: dict[str, str] = field(default_factory=dict)  # folder id -> account number
    ignore: list[str] = field(default_factory=list)  # account number or "Account name", verbatim
    default_account: str | None = None  # account id the UI should open to, instead of "combined"
    default_range: str | None = None  # date-range preset the UI should open to, instead of "All"
    opening_balances: dict[str, tuple[date, float]] = field(default_factory=dict)  # folder id -> (date, balance)

    def folder_account(self, folder_id: str) -> str | None:
        """``folders[folder_id]``, matched case-insensitively.

        ``data/<folder>/`` names are whatever casing the OS happened to
        create, and Windows (unlike the dict lookup this wraps) treats
        filenames case-insensitively -- a config entry like ``"JOINT"``
        should still find an actual folder named ``Joint`` rather than
        silently failing to match.
        """
        return _lookup_ci(self.folders, folder_id)

    def opening_balance(self, folder_id: str) -> tuple[date, float] | None:
        """``opening_balances[folder_id]``, matched case-insensitively -- see
        :meth:`folder_account` for why case-insensitive matching matters here.
        """
        return _lookup_ci(self.opening_balances, folder_id)


def load_account_config(base_dir: str) -> tuple[AccountConfig, list[str]]:
    """``data/accounts.json``, if present -- see the module docstring for the shape.

    Optional and purely additive: with no config file (the common case),
    every folder's account number is still inferred from its own Positions
    snapshot exactly as before (:func:`_resolve_account_number`'s heuristic),
    and no account is ever hidden. A malformed file is ignored (with a
    warning) rather than raised -- a typo in an optional config file should
    never take the whole dashboard down.
    """
    path = os.path.join(base_dir, ACCOUNT_CONFIG_FILENAME)
    if not os.path.isfile(path):
        return AccountConfig(), []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError) as error:
        return AccountConfig(), [f"{ACCOUNT_CONFIG_FILENAME}: could not read ({error}); ignoring"]
    if not isinstance(raw, dict):
        return AccountConfig(), [f"{ACCOUNT_CONFIG_FILENAME}: expected a JSON object; ignoring"]

    warnings: list[str] = []

    folders_raw = raw.get("folders", {})
    folders: dict[str, str] = {}
    if isinstance(folders_raw, dict):
        for folder, number in folders_raw.items():
            if not isinstance(folder, str) or not isinstance(number, (str, int)) or isinstance(number, bool):
                warnings.append(f"{ACCOUNT_CONFIG_FILENAME}: skipped invalid folders entry {folder!r}: {number!r}")
                continue
            folders[folder] = str(number).strip()
    elif "folders" in raw:
        warnings.append(f"{ACCOUNT_CONFIG_FILENAME}: 'folders' must be an object of folder -> account number; ignoring")

    ignore_raw = raw.get("ignore", [])
    ignore: list[str] = []
    if isinstance(ignore_raw, list):
        for entry in ignore_raw:
            if isinstance(entry, str) and entry.strip():
                ignore.append(entry.strip())
            else:
                warnings.append(f"{ACCOUNT_CONFIG_FILENAME}: skipped invalid ignore entry {entry!r}")
    elif "ignore" in raw:
        warnings.append(f"{ACCOUNT_CONFIG_FILENAME}: 'ignore' must be a list of account numbers/names; ignoring")

    default_account_raw = raw.get("default_account")
    default_account: str | None = None
    if default_account_raw is not None:
        if isinstance(default_account_raw, str) and default_account_raw.strip():
            default_account = default_account_raw.strip()
        else:
            warnings.append(f"{ACCOUNT_CONFIG_FILENAME}: 'default_account' must be a non-empty string; ignoring")

    default_range_raw = raw.get("default_range")
    default_range: str | None = None
    if default_range_raw is not None:
        if isinstance(default_range_raw, str) and _VALID_DEFAULT_RANGE_RE.match(default_range_raw.strip()):
            default_range = default_range_raw.strip()
        else:
            warnings.append(
                f"{ACCOUNT_CONFIG_FILENAME}: 'default_range' must be one of "
                "all/ytd/1y/3y/5y/year:YYYY/<day count>; ignoring"
            )

    opening_balances_raw = raw.get("opening_balances", {})
    opening_balances: dict[str, tuple[date, float]] = {}
    if isinstance(opening_balances_raw, dict):
        for folder, entry in opening_balances_raw.items():
            if not isinstance(folder, str):
                warnings.append(f"{ACCOUNT_CONFIG_FILENAME}: skipped invalid opening_balances entry {folder!r}")
                continue
            if not isinstance(entry, dict):
                warnings.append(
                    f"{ACCOUNT_CONFIG_FILENAME}: opening_balances['{folder}'] must be an object "
                    "with 'date' and 'balance'; ignoring"
                )
                continue
            entry_date_raw = entry.get("date")
            balance_raw = entry.get("balance")
            entry_date: date | None = None
            if isinstance(entry_date_raw, str):
                try:
                    entry_date = date.fromisoformat(entry_date_raw.strip())
                except ValueError:
                    entry_date = None
            balance_valid = isinstance(balance_raw, (int, float)) and not isinstance(balance_raw, bool)
            if entry_date is None or not balance_valid:
                warnings.append(
                    f"{ACCOUNT_CONFIG_FILENAME}: opening_balances['{folder}'] needs a 'date' in YYYY-MM-DD "
                    "form and a numeric 'balance'; ignoring"
                )
                continue
            opening_balances[folder] = (entry_date, float(balance_raw))
    elif "opening_balances" in raw:
        warnings.append(
            f"{ACCOUNT_CONFIG_FILENAME}: 'opening_balances' must be an object of "
            "folder -> {date, balance}; ignoring"
        )

    unknown_keys = set(raw) - {"folders", "ignore", "default_account", "default_range", "opening_balances"}
    if unknown_keys:
        warnings.append(f"{ACCOUNT_CONFIG_FILENAME}: ignoring unknown key(s) {', '.join(sorted(unknown_keys))}")

    return (
        AccountConfig(
            folders=folders,
            ignore=ignore,
            default_account=default_account,
            default_range=default_range,
            opening_balances=opening_balances,
        ),
        warnings,
    )


def _is_ignored(account_number: str | None, account_name: str | None, ignore: Sequence[str]) -> bool:
    """Does ``ignore`` (raw ``data/accounts.json`` entries) name this account?

    Matched against the account number exactly, or the account name
    case-insensitively -- Fidelity's ``Account name`` column is what a user
    can actually see and copy, so name matching has to tolerate casing they
    didn't think to preserve.
    """
    name_key = account_name.strip().lower() if account_name else None
    for entry in ignore:
        if entry == account_number:
            return True
        if name_key is not None and entry.lower() == name_key:
            return True
    return False


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text: str) -> str:
    slug = _SLUG_RE.sub("-", text.strip().lower()).strip("-")
    return slug or "account"


def _reserved_account_id(candidate: str) -> bool:
    """Is ``candidate`` a sentinel that a real account id must never equal?

    ``DEFAULT_ACCOUNT_ID`` is matched exactly (it is always lowercase and
    generated, never user-typed); ``COMBINED_ACCOUNT_ID`` is matched
    case-insensitively, the same casing tolerance ``build()`` itself applies
    when routing a request to the aggregate view -- a folder or auto-discovered
    account named ``Combined``/``COMBINED`` would otherwise be silently
    unreachable on its own tab forever.
    """
    return candidate == DEFAULT_ACCOUNT_ID or candidate.lower() == COMBINED_ACCOUNT_ID


def _unique_account_id(base: str, taken: dict) -> str:
    """``base``, or ``base-2``/``base-3``/... if it collides with an existing
    account id or a reserved sentinel (``"default"``, ``"combined"``).

    For auto-discovered, positions-only accounts (a Positions file naming a
    real account with no folder of its own -- see ``AccountRegistry.refresh``)
    slugified straight from the account's own name/number, so either sentinel
    is a genuine, if rare, possible collision -- unlike a folder's own id
    (:func:`_unique_folder_account_id`), which is allowed to legitimately
    equal ``"default"``.
    """
    if not _reserved_account_id(base) and base not in taken:
        return base
    n = 2
    candidate = f"{base}-{n}"
    while candidate in taken or _reserved_account_id(candidate):
        n += 1
        candidate = f"{base}-{n}"
    return candidate


def _unique_folder_account_id(base: str, taken: dict) -> str:
    """``base`` (a folder id), or ``base-2``/``base-3``/... if it collides
    with the reserved ``COMBINED_ACCOUNT_ID`` sentinel or an account already
    registered under that id.

    Unlike :func:`_unique_account_id`, a folder id is allowed to equal
    ``DEFAULT_ACCOUNT_ID`` unchanged -- that's the implicit default bucket's
    own, legitimate identity (``discover_account_dirs`` always creates one
    ``AccountDir`` with exactly that id), not a collision to rename away from.
    """
    if base.lower() != COMBINED_ACCOUNT_ID and base not in taken:
        return base
    n = 2
    candidate = f"{base}-{n}"
    while candidate in taken or candidate.lower() == COMBINED_ACCOUNT_ID:
        n += 1
        candidate = f"{base}-{n}"
    return candidate


def _dashboard_account_name(dashboard: Dashboard) -> str | None:
    if not dashboard.snapshots:
        return None
    latest = latest_snapshot(dashboard.snapshots)
    return latest.account_name if latest else None


def _resolve_account_number(account_dir: AccountDir, configured_number: str | None = None) -> str | None:
    """The real brokerage account number an ``AccountDir`` reports, if knowable.

    Folder identity (the folder name) is a convenience, not the ground truth --
    the ground truth is whatever a folder's own Positions snapshot says, unless
    ``data/accounts.json`` names this folder's account explicitly, which wins
    outright (see :func:`load_account_config`). A folder with no Positions file
    and no config entry has no verifiable identity and is treated as
    unresolved, never guessed at.
    """
    if configured_number:
        return configured_number
    if not account_dir.position_paths:
        return None
    try:
        snapshots, _ = load_snapshots(account_dir.position_paths)
    except Exception:
        return None
    latest = latest_snapshot(snapshots)
    return latest.account_number if latest else None


class AccountRegistry:
    """Discovers account folders and answers per-account or Combined queries."""

    def __init__(self, base_dir: str = "data", extra_dirs: Sequence[str] = (".",)):
        self.base_dir = base_dir
        self.extra_dirs = tuple(extra_dirs)
        self._lock = threading.Lock()
        self._default_dashboard: Dashboard | None = None  # overridden by serve.py's uploader
        self._accounts: dict[str, Dashboard] = {}
        self._build_warnings: list[str] = []
        self._stamp: tuple | None = None
        self.default_account_id: str | None = None  # data/accounts.json's "default_account", if valid
        self.default_range: str | None = None  # data/accounts.json's "default_range", if valid
        self.refresh()

    def set_default_dashboard(self, dashboard: Dashboard) -> None:
        """Let an externally managed Dashboard (e.g. one with upload/select
        support) own the ``"default"`` account instead of a freshly discovered one.

        A no-op when it's the same instance as last time (the common case,
        since ``DashboardState.get()`` itself only rebuilds on a file-mtime
        change), so this can be called on every request without forcing a
        rebuild of every other account each time.
        """
        with self._lock:
            if dashboard is self._default_dashboard:
                return
            self._default_dashboard = dashboard
        self.refresh(force=True)

    def _subfolder_fingerprint(self) -> tuple:
        """mtimes of every subfolder account's own CSV files, plus
        ``accounts.json``'s own mtime so editing the config alone (no CSV
        touched) still triggers a rebuild -- the default account's own
        staleness is handled by whatever supplies it via
        :meth:`set_default_dashboard`, not by this fingerprint.

        Deliberately cheap: every ``.csv``'s own name and mtime, from a plain
        directory listing -- never opened, so computing this fingerprint
        never re-runs the header-sniffing ``discover_exports()``/
        ``discover_position_snapshots()`` do (open + read the first 4KB of
        every candidate file), on every single request via ``refresh()``,
        just to answer "has anything changed since last time." That real,
        header-based discovery still runs, exactly once, on the "yes,
        something changed" branch below (``discover_account_dirs``) -- this
        fingerprint's only job is deciding whether that's needed at all.
        """
        parts = []
        if os.path.isdir(self.base_dir):
            for entry in sorted(os.listdir(self.base_dir)):
                path = os.path.join(self.base_dir, entry)
                if not os.path.isdir(path):
                    continue
                try:
                    files = sorted(name for name in os.listdir(path) if name.lower().endswith(".csv"))
                    parts.append(
                        (entry, tuple((name, os.path.getmtime(os.path.join(path, name))) for name in files))
                    )
                except OSError:
                    parts.append((entry, ()))
        config_path = os.path.join(self.base_dir, ACCOUNT_CONFIG_FILENAME)
        config_stamp = os.path.getmtime(config_path) if os.path.isfile(config_path) else None
        return (tuple(parts), config_stamp)

    def refresh(self, force: bool = False) -> None:
        with self._lock:
            stamp = self._subfolder_fingerprint()
            if not force and self._accounts and stamp == self._stamp:
                return

            account_dirs = discover_account_dirs(self.base_dir, self.extra_dirs)
            config, config_warnings = load_account_config(self.base_dir)
            all_position_paths = sorted({path for account_dir in account_dirs for path in account_dir.position_paths})

            accounts: dict[str, Dashboard] = {}
            warnings: list[str] = list(config_warnings)
            claimed_numbers: set[str] = set()
            number_to_id: dict[str, str] = {}  # lets "default_account" name an account number, not just an id

            # The externally managed "default" dashboard (serve.py's uploader,
            # via set_default_dashboard) owns that folder's identity outright
            # when present, and is handled once, up front -- never through the
            # number-based merge loop below, whose merge branch rebuilds a
            # fresh Dashboard from the union of every matching folder's files.
            # Running "default" through that when a live dashboard exists
            # would silently discard whatever upload/select state exists only
            # in memory (e.g. files chosen but not yet reflected on disk in
            # exactly the shape the merge expects) the moment its account
            # number happens to also match another folder's.
            default_dir = next((d for d in account_dirs if d.id == DEFAULT_ACCOUNT_ID), None)
            grouping_dirs = account_dirs
            if default_dir is not None and self._default_dashboard is not None:
                grouping_dirs = [d for d in account_dirs if d.id != DEFAULT_ACCOUNT_ID]
                default_number = _resolve_account_number(default_dir, config.folder_account(DEFAULT_ACCOUNT_ID))
                # A number resolved from nothing but a loose, shared "all
                # accounts" Positions file (no transaction history of its
                # own -- self._default_dashboard.csv_paths is empty) is weak
                # evidence: it just means some account's row in that shared
                # file happened to have the latest `as_of` timestamp, not
                # that this really is the default bucket's account. If a
                # real folder independently resolves to the same number,
                # that folder's own dedicated history must win -- otherwise
                # the empty default's claim silently drops the folder's
                # whole transaction history in the "already covered" branch
                # below instead of merging with or deferring to it.
                default_redundant = False
                if default_number and not self._default_dashboard.csv_paths:
                    other_numbers = {
                        _resolve_account_number(d, config.folder_account(d.id)) for d in grouping_dirs
                    }
                    if default_number in other_numbers:
                        # The colliding folder wins the number (see comment
                        # above); a history-less default now has nothing of
                        # its own to contribute for this account, since that
                        # folder's Dashboard -- resolved via a configured
                        # number -- already widens its own search to every
                        # Positions file on disk (see `position_paths =
                        # all_position_paths if configured_number else ...`
                        # below). Registering "default" here too would just
                        # report that same folder's net worth a second time
                        # under a different id, double-counting it in
                        # Combined -- so it's dropped rather than kept as a
                        # harmless-looking duplicate.
                        default_number = None
                        default_redundant = True
                if default_number:
                    claimed_numbers.add(default_number)
                    number_to_id[default_number] = DEFAULT_ACCOUNT_ID
                # A 'folders'/'opening_balances' entry for "default" has no
                # effect while a live dashboard owns it (that Dashboard was
                # already built by DashboardState, with no knowledge of this
                # config) -- surfaced as a warning rather than a silent no-op.
                if config.folder_account(DEFAULT_ACCOUNT_ID) or config.opening_balance(DEFAULT_ACCOUNT_ID):
                    warnings.append(
                        f"{ACCOUNT_CONFIG_FILENAME}: 'folders'/'opening_balances' entries for the "
                        "'default' account have no effect while an uploaded or selected dataset is "
                        "active for it"
                    )
                if not default_redundant and not (
                    default_number
                    and _is_ignored(default_number, _dashboard_account_name(self._default_dashboard), config.ignore)
                ):
                    accounts[DEFAULT_ACCOUNT_ID] = self._default_dashboard

            # Two folders can describe the SAME real brokerage account -- most
            # often stray files left in data/ root from before an account got
            # its own data/<account>/ folder (or an upload that landed in the
            # default bucket while a named account was only selected in the
            # UI). Group folders by the account number their own Positions
            # snapshot reports (data/accounts.json's "folders" wins over the
            # heuristic when a folder is named there), so that case collapses
            # into one account instead of silently double-counting real money
            # in Combined. A folder with no Positions snapshot yet has no
            # verifiable identity and stands alone, keyed by its own folder id.
            groups: dict[str, list[AccountDir]] = {}
            for account_dir in grouping_dirs:
                number = _resolve_account_number(account_dir, config.folder_account(account_dir.id))
                key = f"#{number}" if number else f"@{account_dir.id}"
                groups.setdefault(key, []).append(account_dir)

            # A multi-account export (e.g. an "Accounts_History.csv" download)
            # is deliberately excluded from discover_exports/looks_like_export
            # -- see wheel.api.looks_like_multi_account_export -- rather than
            # parsed with every dollar amount silently zeroed out. Surface
            # that exclusion here so it doesn't just vanish with no
            # explanation of why it was never imported.
            scan_dirs = sorted({account_dir.path for account_dir in account_dirs} | {d for d in self.extra_dirs if os.path.isdir(d)})
            for path in discover_multi_account_exports(scan_dirs):
                warnings.append(
                    f"{os.path.basename(path)}: looks like Fidelity's multi-account transaction history "
                    "export (separate 'Account'/'Account Number' columns) -- not supported yet, so it "
                    "was not imported"
                )

            for key, dirs in groups.items():
                resolved_number = key[1:] if key.startswith("#") else None
                if resolved_number and resolved_number in claimed_numbers:
                    # Already covered by the live "default" dashboard set aside
                    # above -- registering this folder separately too would
                    # double-count the same real account in Combined, exactly
                    # what the merge logic just below exists to prevent.
                    ids = sorted(d.id for d in dirs)
                    warnings.append(
                        f"account {resolved_number} in {', '.join(ids)} is already covered by the "
                        "active 'default' dataset -- not registered separately, to avoid "
                        "double-counting in Combined"
                    )
                    continue

                if len(dirs) == 1:
                    account_dir = dirs[0]
                else:
                    # A real account number resolved for more than one folder.
                    # Merge them into a single Dashboard, preferring a named
                    # (non-default) folder's identity over the default bucket's.
                    named = sorted((d for d in dirs if d.id != DEFAULT_ACCOUNT_ID), key=lambda d: d.id)
                    primary = named[0] if named else dirs[0]
                    account_dir = AccountDir(
                        id=primary.id,
                        path=primary.path,
                        history_paths=sorted({path for d in dirs for path in d.history_paths}),
                        position_paths=sorted({path for d in dirs for path in d.position_paths}),
                    )
                    other_ids = sorted(d.id for d in dirs if d.id != primary.id)
                    warnings.append(
                        f"account {key[1:]} appears in both '{primary.id}' and {', '.join(other_ids)} -- "
                        f"merged into '{primary.id}' rather than counted twice in Combined"
                    )

                configured_number = config.folder_account(account_dir.id)
                # A configured folder's own Positions data may live only in a
                # shared/"all accounts" download rather than a file inside its
                # own folder -- widen the search to every Positions file found
                # anywhere, trusting the account_number filter below to keep
                # only this folder's own rows out of whatever it finds.
                position_paths = all_position_paths if configured_number else account_dir.position_paths

                if not account_dir.history_paths and not position_paths:
                    continue
                try:
                    dashboard = Dashboard(
                        account_dir.history_paths,
                        position_paths=position_paths,
                        account_number=resolved_number,
                        opening_balance=config.opening_balance(account_dir.id),
                    )
                except ValueError as error:
                    warnings.append(f"account '{account_dir.id}': {error}")
                    continue

                # A folder can be named "combined" -- disambiguate rather than
                # register an account under a reserved id that build() would
                # silently route to the aggregate view forever.
                account_id = _unique_folder_account_id(account_dir.id, accounts)
                if account_id != account_dir.id:
                    warnings.append(
                        f"account '{account_dir.id}' collides with a reserved account id -- "
                        f"registered as '{account_id}' instead"
                    )
                if resolved_number:
                    claimed_numbers.add(resolved_number)
                    number_to_id[resolved_number] = account_id
                if _is_ignored(resolved_number, _dashboard_account_name(dashboard), config.ignore):
                    continue
                accounts[account_id] = dashboard

            # Every OTHER real account any Positions file mentions, with no
            # folder of its own -- e.g. a linked account that only ever shows
            # up in a shared "all accounts" download. Each gets its own
            # positions-only Dashboard (no transaction/cycle history -- there's
            # no column to attribute it by), scoped down to just its own rows
            # the same way a configured folder's is.
            if all_position_paths:
                try:
                    # Warnings from this scan (e.g. a duplicate snapshot) are
                    # dropped here, not appended to the registry's own -- each
                    # account discovered below builds its own Dashboard from
                    # the same files and surfaces them itself; this scan only
                    # exists to enumerate account numbers.
                    all_snapshots, _ = load_snapshots(all_position_paths)
                except Exception as error:
                    all_snapshots = []
                    warnings.append(f"could not scan Positions files for additional accounts: {error}")

                for account_number, snapshot in sorted(latest_snapshot_per_account(all_snapshots).items()):
                    if account_number in claimed_numbers:
                        continue
                    account_id = _unique_account_id(_slugify(snapshot.account_name or account_number), accounts)
                    number_to_id[account_number] = account_id
                    if _is_ignored(account_number, snapshot.account_name, config.ignore):
                        continue
                    try:
                        accounts[account_id] = Dashboard(
                            [], position_paths=all_position_paths, account_number=account_number
                        )
                    except ValueError as error:
                        warnings.append(f"account '{account_id}': {error}")

            # "default_account" may name either an account id directly (a
            # folder id, or an auto-discovered slug like "roth-ira") or the
            # account's own number -- the number is what a user copies
            # straight out of Fidelity, and shouldn't have to be translated
            # into whatever slug auto-discovery happened to generate.
            default_account_id = config.default_account
            if default_account_id and default_account_id not in accounts:
                default_account_id = number_to_id.get(default_account_id, default_account_id)
            if default_account_id and default_account_id not in accounts:
                # Case-insensitive fallback -- same reasoning as
                # AccountConfig.folder_account()/opening_balance(): a
                # data/<folder>/'s casing is whatever the OS happened to
                # create, and an id (as opposed to a number, already handled
                # above) shouldn't have to match it exactly either.
                by_lower = {account_id.lower(): account_id for account_id in accounts}
                default_account_id = by_lower.get(default_account_id.lower(), default_account_id)
            if (
                default_account_id
                and default_account_id not in accounts
                and default_account_id.lower() != COMBINED_ACCOUNT_ID
            ):
                warnings.append(
                    f"{ACCOUNT_CONFIG_FILENAME}: default_account '{config.default_account}' is not a known "
                    "account id or number; falling back to Combined"
                )
                default_account_id = None

            self._accounts = accounts
            self._build_warnings = warnings
            self._stamp = stamp
            self.default_account_id = default_account_id
            self.default_range = config.default_range

    # ---- accessors ----

    def get(self, account_id: str) -> Dashboard:
        if account_id not in self._accounts:
            raise KeyError(account_id)
        return self._accounts[account_id]

    def list_accounts(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for account_id, dashboard in self._accounts.items():
            account_number = account_name = None
            latest = latest_snapshot(dashboard.snapshots)
            if latest is not None:
                account_number, account_name = latest.account_number, latest.account_name
            label = account_name or ("Combined default" if account_id == DEFAULT_ACCOUNT_ID else account_id)
            rows.append(
                {
                    "id": account_id,
                    "label": label,
                    "account_number": account_number,
                    "account_name": account_name,
                    "transactions": len(dashboard.transactions),
                    "has_positions": bool(dashboard.snapshots),
                    "warnings": list(dashboard.position_warnings),
                }
            )
        rows.sort(key=lambda row: (row["id"] != DEFAULT_ACCOUNT_ID, row["label"]))
        return rows

    def build(self, account_id: str | None, filters: Filters | None = None) -> dict[str, Any]:
        if not account_id or account_id.lower() == COMBINED_ACCOUNT_ID:
            return self._build_combined(filters)
        payload = self.get(account_id).build(filters)
        payload["meta"] = {**payload["meta"], "account_id": account_id}
        return payload

    # ---- combined aggregation ----

    def _build_combined(self, filters: Filters | None) -> dict[str, Any]:
        if not self._accounts:
            raise ValueError("no accounts discovered under " + self.base_dir)

        payloads = {account_id: dashboard.build(filters) for account_id, dashboard in self._accounts.items()}
        labels = {row["id"]: row["label"] for row in self.list_accounts()}

        capital_series = _combine_capital_series(payloads)
        net_worth = _combine_net_worth(payloads)

        return {
            "meta": _combine_meta(payloads, labels, self._build_warnings),
            "portfolio": _combine_portfolio(payloads, capital_series),
            "cycles": _combine_cycles(payloads),
            "tickers": _combine_tickers(payloads),
            "capital_series": capital_series,
            "pnl_series": _combine_pnl_series(payloads),
            "cash_flow": _combine_cash_flow(payloads, capital_series),
            "wheel_state": _combine_wheel_state(payloads),
            "reconciliation": _combine_reconciliation(payloads),
            "net_worth": net_worth,
            "benchmark": _combine_benchmark(payloads),
            "wheel_return": _combine_wheel_return(payloads),
        }


# --------------------------------------------------------------------------
# Aggregation helpers (module-level, pure -- easy to unit test independent of
# discovery/I-O)
# --------------------------------------------------------------------------


def _combine_meta(
    payloads: dict[str, dict], labels: dict[str, str], registry_warnings: Sequence[str]
) -> dict[str, Any]:
    metas = [payload["meta"] for payload in payloads.values()]
    throughs = [meta["through"] for meta in metas if meta["through"]]
    first_dates = [meta["data_first_date"] for meta in metas if meta["data_first_date"]]
    last_dates = [meta["data_last_date"] for meta in metas if meta["data_last_date"]]
    return {
        "source": " + ".join(meta["source"] for meta in metas if meta["source"]),
        "sources": [source for meta in metas for source in meta["sources"]],
        "combined": True,
        "rows_parsed": sum(meta["rows_parsed"] for meta in metas),
        "rows_kept": sum(meta["rows_kept"] for meta in metas),
        "duplicates_removed": sum(meta["duplicates_removed"] for meta in metas),
        "generated_at": max((meta["generated_at"] for meta in metas), default=None),
        "through": max(throughs) if throughs else None,
        "data_first_date": min(first_dates) if first_dates else None,
        "data_last_date": max(last_dates) if last_dates else None,
        "available_tickers": sorted({ticker for meta in metas for ticker in meta["available_tickers"]}),
        "statuses": ["ACTIVE", "CLOSED", "ASSIGNED"],
        "transactions_in_slice": sum(meta["transactions_in_slice"] for meta in metas),
        "transactions_total": sum(meta["transactions_total"] for meta in metas),
        "columns_swapped": any(meta["columns_swapped"] for meta in metas),
        "parse_warnings": [f"[{account_id}] {w}" for account_id, p in payloads.items() for w in p["meta"]["parse_warnings"]],
        "engine_warnings": [f"[{account_id}] {w}" for account_id, p in payloads.items() for w in p["meta"]["engine_warnings"]]
        + [f"[registry] {w}" for w in registry_warnings],
        "unmatched_closes": [
            {**item, "account_id": account_id} for account_id, p in payloads.items() for item in p["meta"]["unmatched_closes"]
        ],
        "filters": next(iter(metas))["filters"] if metas else {},
        "account_id": COMBINED_ACCOUNT_ID,
        "account_labels": labels,
        "accounts_included": list(payloads.keys()),
    }


def _combine_cycles(payloads: dict[str, dict]) -> list[dict]:
    """Concatenate every account's cycles, never re-merge them.

    ``cycle_id`` is generated independently per account (e.g. both accounts'
    first MU cycle would be "MU-1"), and the frontend uses it as a Set key for
    expand/collapse state -- so it must be made unique across accounts here, or
    expanding one account's row would spuriously expand the other's identically
    named cycle too.
    """
    combined = []
    for account_id, payload in payloads.items():
        for cycle in payload["cycles"]:
            tagged_id = f"{account_id}:{cycle['cycle_id']}"
            tagged = {**cycle, "account_id": account_id, "cycle_id": tagged_id}
            if tagged.get("legs"):
                tagged["legs"] = [{**leg, "cycle_id": tagged_id} for leg in tagged["legs"]]
            combined.append(tagged)
    combined.sort(key=lambda cycle: (cycle["start_date"] or "", cycle["underlying"]))
    return combined


def _combine_tickers(payloads: dict[str, dict]) -> list[dict]:
    combined = [
        {**row, "account_id": account_id} for account_id, payload in payloads.items() for row in payload["tickers"]
    ]
    combined.sort(key=lambda row: row["net_realized_pl"], reverse=True)
    return combined


def _combine_capital_series(payloads: dict[str, dict]) -> list[dict]:
    buckets: dict[str, dict[str, float]] = {}
    for payload in payloads.values():
        for point in payload["capital_series"]:
            bucket = buckets.setdefault(
                point["date"],
                {"put": 0.0, "stock": 0.0, "call": 0.0, "long": 0.0, "spread": 0.0, "idle_stock": 0.0},
            )
            for field_name in ("put", "stock", "call", "long", "spread", "idle_stock"):
                bucket[field_name] += point.get(field_name) or 0.0

    series: list[dict] = []
    for day in sorted(buckets):
        values = buckets[day]
        total = round(
            values["put"] + values["stock"] + values["call"] + values["long"] + values["spread"], 2
        )
        series.append(
            {
                "date": day,
                "put": round(values["put"], 2),
                "stock": round(values["stock"], 2),
                "call": round(values["call"], 2),
                "long": round(values["long"], 2),
                "spread": round(values["spread"], 2),
                "total": total,
                "idle_stock": round(values["idle_stock"], 2),
            }
        )
    return series


def _combine_pnl_series(payloads: dict[str, dict]) -> list[dict]:
    """Sum each account's *daily* P/L by date, then recompute the running
    totals -- summing each account's own cumulative column by date would double
    count every day after the first account joins the series.
    """
    buckets: dict[str, dict[str, float]] = {}
    for payload in payloads.values():
        for point in payload["pnl_series"]:
            bucket = buckets.setdefault(point["date"], {"option_pl": 0.0, "stock_pl": 0.0})
            bucket["option_pl"] += point.get("option_pl") or 0.0
            bucket["stock_pl"] += point.get("stock_pl") or 0.0

    series: list[dict] = []
    cum_option = cum_stock = 0.0
    for day in sorted(buckets):
        option_pl = buckets[day]["option_pl"]
        stock_pl = buckets[day]["stock_pl"]
        cum_option += option_pl
        cum_stock += stock_pl
        series.append(
            {
                "date": day,
                "option_pl": round(option_pl, 2),
                "stock_pl": round(stock_pl, 2),
                "total_pl": round(option_pl + stock_pl, 2),
                "cum_option_pl": round(cum_option, 2),
                "cum_stock_pl": round(cum_stock, 2),
                "cum_total_pl": round(cum_option + cum_stock, 2),
            }
        )
    return series


def _combine_cash_flow(payloads: dict[str, dict], capital_series: list[dict]) -> dict[str, Any]:
    """Sum every account's monthly credits/debits/fees by calendar month, then
    recompute average collateral and monthly yield from the already-combined
    ``capital_series`` -- never by averaging each account's own monthly yield
    %, which would weight a small account the same as a large one. Same
    principle ``_combine_portfolio`` applies to ROI/ROC, one level down.
    """
    buckets: dict[str, dict[str, float]] = {}
    for payload in payloads.values():
        for row in payload["cash_flow"]["months"]:
            bucket = buckets.setdefault(
                row["period"], {"year": row["year"], "month": row["month"], "credits": 0.0, "debits": 0.0, "fees": 0.0}
            )
            bucket["credits"] += row["gross_credits"]
            bucket["debits"] += row["gross_debits"]
            bucket["fees"] += row["fees"]

    capital_points = [(date.fromisoformat(point["date"]), point["total"]) for point in capital_series]

    rows: list[dict] = []
    for period in sorted(buckets):
        bucket = buckets[period]
        avg_collateral = cf.month_average_collateral(capital_points, bucket["year"], bucket["month"])
        net = bucket["credits"] - bucket["debits"] - bucket["fees"]
        rows.append(
            {
                "year": bucket["year"],
                "month": bucket["month"],
                "period": period,
                "gross_credits": round(bucket["credits"], 2),
                "gross_debits": round(bucket["debits"], 2),
                "fees": round(bucket["fees"], 2),
                "net_cash_flow": round(net, 2),
                "avg_collateral": round(avg_collateral, 2),
                "monthly_yield_pct": round(100.0 * net / avg_collateral, 4) if avg_collateral > 1e-9 else None,
            }
        )

    throughs = [
        date.fromisoformat(payload["meta"]["through"]) for payload in payloads.values() if payload["meta"]["through"]
    ]
    through = max(throughs) if throughs else date.today()
    return {"months": rows, "trailing": cf.range_summary(rows, capital_points, through)}


def _combine_wheel_state(payloads: dict[str, dict]) -> dict[str, Any]:
    """Sum every account's own wheel-state buckets -- amounts and cycle
    counts add, ticker sets union, since each account's cycles are distinct
    positions (no cross-account collision to dedupe, unlike combined cycle
    ids elsewhere in this module). ``active_cycles`` sums the same way: each
    account already reports its own unique count, and accounts don't share
    cycles.
    """
    keys = ("puts", "calls", "holding", "other")
    buckets: dict[str, dict[str, Any]] = {
        key: {"amount": 0.0, "cycles": 0, "tickers": set()} for key in keys
    }
    active_cycles = 0
    for payload in payloads.values():
        state = payload.get("wheel_state") or {}
        for key in keys:
            bucket = (state.get("buckets") or {}).get(key) or {}
            buckets[key]["amount"] += bucket.get("amount", 0.0)
            buckets[key]["cycles"] += bucket.get("cycles", 0)
            buckets[key]["tickers"].update(bucket.get("tickers", []))
        active_cycles += state.get("active_cycles", 0)

    return {
        "buckets": {
            key: {
                "amount": round(bucket["amount"], 2),
                "cycles": bucket["cycles"],
                "tickers": sorted(bucket["tickers"]),
            }
            for key, bucket in buckets.items()
        },
        "active_cycles": active_cycles,
    }


def _combine_portfolio(payloads: dict[str, dict], capital_series: list[dict]) -> dict[str, Any]:
    portfolios = [payload["portfolio"] for payload in payloads.values()]
    first_dates = [p["first_date"] for p in portfolios if p["first_date"]]
    last_dates = [p["last_date"] for p in portfolios if p["last_date"]]

    combined: dict[str, Any] = {
        "cycles": sum(p["cycles"] for p in portfolios),
        "active_cycles": sum(p["active_cycles"] for p in portfolios),
        "tickers": len({row["underlying"] for payload in payloads.values() for row in payload["tickers"]}),
        "first_date": min(first_dates) if first_dates else None,
        "last_date": max(last_dates) if last_dates else None,
        "premium_received": sum(p["premium_received"] for p in portfolios),
        "premium_paid": sum(p["premium_paid"] for p in portfolios),
        "option_realized_pl": sum(p["option_realized_pl"] for p in portfolios),
        "wheel_core_realized_pl": sum(p["wheel_core_realized_pl"] for p in portfolios),
        "hedge_realized_pl": sum(p["hedge_realized_pl"] for p in portfolios),
        "stock_realized_pl": sum(p["stock_realized_pl"] for p in portfolios),
        "net_realized_pl": sum(p["net_realized_pl"] for p in portfolios),
        "open_premium": sum(p["open_premium"] for p in portfolios),
        "fees": sum(p["fees"] for p in portfolios),
        "total_legs": sum(p["total_legs"] for p in portfolios),
        "open_legs": sum(p["open_legs"] for p in portfolios),
        "rolls": sum(p["rolls"] for p in portfolios),
        "assignments": sum(p["assignments"] for p in portfolios),
        "wins": sum(p["wins"] for p in portfolios),
        "losses": sum(p["losses"] for p in portfolios),
        "total_initial_collateral": sum(p["total_initial_collateral"] for p in portfolios),
        "dividends_received": sum(p["dividends_received"] for p in portfolios),
        "stock_unrealized_pl": sum(p["stock_unrealized_pl"] for p in portfolios),
    }

    combined["days_span"] = (
        max((date.fromisoformat(combined["last_date"]) - date.fromisoformat(combined["first_date"])).days, 1)
        if combined["first_date"] and combined["last_date"]
        else 0
    )
    combined["profit_per_day"] = (
        combined["option_realized_pl"] / combined["days_span"] if combined["days_span"] else 0.0
    )
    decided = combined["wins"] + combined["losses"]
    combined["win_rate_pct"] = round(100.0 * combined["wins"] / decided, 2) if decided else None

    # Weighted by each account's own decided-leg count, the same principle
    # portfolio_metrics() applies one level down (weighting per-cycle averages
    # by trade count rather than treating every cycle/account as equally sized).
    weighted = [
        (p["avg_days_in_trade"], p["wins"] + p["losses"]) for p in portfolios if p["avg_days_in_trade"] is not None
    ]
    total_weight = sum(weight for _, weight in weighted)
    combined["avg_days_in_trade"] = (
        round(sum(days * weight for days, weight in weighted) / total_weight, 2) if total_weight else None
    )

    # ROI/ROC are recomputed from the combined capital series' own time-weighted
    # average -- never averaged from each account's own percentage. See module
    # docstring: averaging would weight a small account the same as a large one.
    avg_capital = time_weighted_average(capital_series, value=lambda point: point["total"])
    combined["capital_deployed_now"] = round(capital_series[-1]["total"], 2) if capital_series else 0.0
    combined["peak_capital"] = round(max((point["total"] for point in capital_series), default=0.0), 2)
    combined["avg_capital"] = round(avg_capital, 2)

    combined["roi_on_avg_wheel_pct"], combined["annualized_wheel_roc_pct"] = roi_and_annualized(
        combined["option_realized_pl"], avg_capital, combined["days_span"]
    )

    # Same figures again, narrowed to capital actually backing an open put or
    # covered call (see wheel/metrics.py's CapitalPoint.working_capital and
    # PortfolioMetrics.avg_active_capital) -- recomputed from the combined
    # series' own `total - idle_stock` per day, for the same averaging-bias
    # reason as avg_capital above.
    def active_capital(point: dict) -> float:
        return point["total"] - (point.get("idle_stock") or 0.0)

    avg_active_capital = time_weighted_average(capital_series, value=active_capital)
    combined["active_capital_deployed_now"] = round(active_capital(capital_series[-1]), 2) if capital_series else 0.0
    combined["peak_active_capital"] = round(
        max((active_capital(point) for point in capital_series), default=0.0), 2
    )
    combined["avg_active_capital"] = round(avg_active_capital, 2)

    combined["roi_on_avg_active_capital_pct"], combined["annualized_active_wheel_roc_pct"] = roi_and_annualized(
        combined["option_realized_pl"], avg_active_capital, combined["days_span"]
    )

    # Dual-track pair, quoted against total_initial_collateral -- the sum of
    # every account's own summed-per-cycle initial collateral, a different
    # denominator from avg_capital above -- and, like every other combined
    # figure here, recomputed from the combined absolutes rather than
    # averaging each account's own percentage.
    combined["net_option_yield_pct"], combined["annualized_net_option_yield_pct"] = roi_and_annualized(
        combined["option_realized_pl"], combined["total_initial_collateral"], combined["days_span"]
    )
    total_position_pl = (
        combined["option_realized_pl"]
        + combined["stock_realized_pl"]
        + combined["stock_unrealized_pl"]
        + combined["dividends_received"]
    )
    combined["total_position_roi_pct"], combined["annualized_total_position_roi_pct"] = roi_and_annualized(
        total_position_pl, combined["total_initial_collateral"], combined["days_span"]
    )

    for money_key in (
        "premium_received",
        "premium_paid",
        "option_realized_pl",
        "wheel_core_realized_pl",
        "hedge_realized_pl",
        "stock_realized_pl",
        "net_realized_pl",
        "open_premium",
        "fees",
        "total_initial_collateral",
        "dividends_received",
        "stock_unrealized_pl",
        "profit_per_day",
    ):
        combined[money_key] = round(combined[money_key], 2)

    return combined


def _combine_reconciliation(payloads: dict[str, dict]) -> dict[str, Any]:
    records = [payload["reconciliation"] for payload in payloads.values()]
    rows_checked_total = sum(r["rows_checked"] for r in records)
    checkable_total = sum(r["rows_checked"] + len(r["row_failures"]) for r in records)
    return {
        "file_cash_total": round(sum(r["file_cash_total"] or 0.0 for r in records), 2),
        "model_cash_total": round(sum(r["model_cash_total"] or 0.0 for r in records), 2),
        "unmatched_cash": round(sum(r["unmatched_cash"] or 0.0 for r in records), 2),
        "equity_cash_total": round(sum(r["equity_cash_total"] or 0.0 for r in records), 2),
        "equity_rows": sum(r["equity_rows"] for r in records),
        "non_trade_rows": sum(r["non_trade_rows"] for r in records),
        "delta": round(sum(r["delta"] for r in records), 6),
        "balanced": all(r["balanced"] for r in records),
        "rows_checked": rows_checked_total,
        "row_failures": [
            {**failure, "account_id": account_id}
            for account_id, payload in payloads.items()
            for failure in payload["reconciliation"]["row_failures"]
        ],
        "reconcile_rate_pct": round(100.0 * rows_checked_total / max(1, checkable_total), 4),
        "synthetic_assignment_cash": round(sum(r["synthetic_assignment_cash"] or 0.0 for r in records), 2),
        "note": "Combined across every account; open one account's view for its own detail.",
    }


def _combine_net_worth(payloads: dict[str, dict]) -> dict[str, Any]:
    entries = [(account_id, payload["net_worth"]) for account_id, payload in payloads.items()]
    warnings = [f"[{account_id}] {w}" for account_id, net_worth in entries for w in net_worth["warnings"]]
    available = [(account_id, net_worth) for account_id, net_worth in entries if net_worth["available"]]

    if not available:
        return {
            "available": False,
            "warnings": warnings or ["no account has a Portfolio Positions snapshot yet"],
            "accounts": [],
        }

    totals = {"total_value": 0.0, "cash_total": 0.0, "equity_value": 0.0, "option_value": 0.0, "wheel_capital_deployed": 0.0}
    accounts_out = []
    for account_id, net_worth in available:
        accounts_out.append({**net_worth, "account_id": account_id})
        for key in totals:
            totals[key] += net_worth[key] or 0.0

    return {
        "available": True,
        "warnings": warnings,
        "accounts": accounts_out,
        "combined": {key: round(value, 2) for key, value in totals.items()},
    }


def _sparse_series_by_date(series: Sequence[dict], key: str) -> list[tuple[date, float]]:
    """``(date, value)`` pairs from a per-account benchmark/net-worth series,
    sorted and with unknown (``None``) values dropped -- the raw material for
    :func:`_forward_fill_at`.
    """
    points = [(date.fromisoformat(row["as_of"]), row[key]) for row in series if row.get(key) is not None]
    points.sort(key=lambda item: item[0])
    return points


def _forward_fill_at(points: list[tuple[date, float]], as_of: date) -> float | None:
    """The last known value at or before ``as_of``, or ``None`` if ``points``
    has nothing that early yet.

    Used to combine two accounts' independently-sampled Positions-snapshot
    series: account A's own value on a date only account B happened to
    snapshot is not "unknown" (which would understate the combined total on
    every date the accounts' snapshots don't land on exactly the same day)
    -- it is A's own last real reading, carried forward, same as any other
    infrequently-sampled time series.
    """
    value = None
    for point_date, point_value in points:
        if point_date > as_of:
            break
        value = point_value
    return value


def _combine_benchmark(payloads: dict[str, dict]) -> dict[str, Any]:
    entries = [(account_id, payload["benchmark"]) for account_id, payload in payloads.items()]
    warnings = [f"[{account_id}] {w}" for account_id, benchmark_payload in entries for w in benchmark_payload["warnings"]]
    available = [(account_id, benchmark_payload) for account_id, benchmark_payload in entries if benchmark_payload["available"]]

    if not available:
        return {
            "available": False,
            "warnings": warnings or ["no account has enough snapshots yet for a benchmark comparison"],
        }

    pooled_events: list[bm.CashFlowEvent] = []
    for account_id, benchmark_payload in available:
        for event in benchmark_payload["cash_flow_events"]:
            pooled_events.append(
                bm.CashFlowEvent(
                    date=date.fromisoformat(event["date"]),
                    amount=event["amount"],
                    label=f"[{account_id}] {event['label']}",
                    source=account_id,
                    kind=event["kind"],
                )
            )

    as_of = max(date.fromisoformat(benchmark_payload["as_of"]) for _, benchmark_payload in available)
    actual_terminal_value = sum(benchmark_payload["actual"]["terminal_value"] for _, benchmark_payload in available)
    benchmark_terminals = [benchmark_payload["benchmark"]["terminal_value"] for _, benchmark_payload in available]
    benchmark_terminal_value = (
        sum(benchmark_terminals) if all(value is not None for value in benchmark_terminals) else None
    )

    result = bm.compare_to_benchmark(pooled_events, actual_terminal_value, benchmark_terminal_value, as_of)

    # Each account's own series only has a point on the dates *it* happened to
    # snapshot -- summing by exact date match would silently drop (or
    # understate) every day the accounts' Positions exports weren't taken on
    # the same day, which is the common case, not the exception. Forward-fill
    # each account's own last-known value across the *union* of every
    # account's snapshot dates instead, so the combined total on any given
    # day reflects every account's most recent real reading, not just
    # whichever accounts happened to snapshot that exact day.
    actual_by_account = {account_id: _sparse_series_by_date(bp["series"], "actual_value") for account_id, bp in available}
    benchmark_by_account = {
        account_id: _sparse_series_by_date(bp["series"], "benchmark_value") for account_id, bp in available
    }
    all_dates = sorted({date.fromisoformat(point["as_of"]) for _, bp in available for point in bp["series"]})

    series = []
    for day in all_dates:
        actual_total = 0.0
        actual_known = False
        benchmark_total = 0.0
        benchmark_known = True
        for account_id, _ in available:
            actual_value = _forward_fill_at(actual_by_account[account_id], day)
            if actual_value is not None:
                actual_total += actual_value
                actual_known = True
            benchmark_value = _forward_fill_at(benchmark_by_account[account_id], day)
            if benchmark_value is None:
                benchmark_known = False
            else:
                benchmark_total += benchmark_value
        if not actual_known:
            continue
        series.append(
            {
                "as_of": day.isoformat(),
                "actual_value": round(actual_total, 2),
                "benchmark_value": round(benchmark_total, 2) if benchmark_known else None,
            }
        )

    return {
        "available": True,
        "warnings": warnings,
        "as_of": as_of.isoformat(),
        "cash_flow_events": [
            {"date": event.date.isoformat(), "amount": round(event.amount, 2), "label": event.label, "kind": event.kind}
            for event in sorted(pooled_events, key=lambda event: event.date)
        ],
        "actual": {
            "terminal_value": round(actual_terminal_value, 2),
            "xirr_pct": round(result.actual_xirr_pct, 2) if result.actual_xirr_pct is not None else None,
        },
        "benchmark": {
            "name": "SPY",
            "terminal_value": round(benchmark_terminal_value, 2) if benchmark_terminal_value is not None else None,
            "xirr_pct": round(result.benchmark_xirr_pct, 2) if result.benchmark_xirr_pct is not None else None,
        },
        "value_added": round(result.value_added, 2) if result.value_added is not None else None,
        "series": series,
    }


def _combine_wheel_return(payloads: dict[str, dict]) -> dict[str, Any]:
    """Same pooling pattern as ``_combine_benchmark`` above, minus the SPY
    replay: every account's own wheel-only cash-flow events are pooled and
    the XIRR resolved once, over the combined timeline -- never averaged
    from each account's own percentage, which would weight a small account
    the same as a large one (see this module's own docstring).
    """
    entries = [(account_id, payload["wheel_return"]) for account_id, payload in payloads.items()]
    warnings = [f"[{account_id}] {w}" for account_id, wr in entries for w in wr["warnings"]]
    available = [(account_id, wr) for account_id, wr in entries if wr["available"]]

    if not available:
        return {
            "available": False,
            "warnings": warnings or ["no account has enough wheel activity yet to compute a return"],
        }

    pooled_events: list[bm.CashFlowEvent] = []
    for account_id, wr in available:
        for event in wr["cash_flow_events"]:
            pooled_events.append(
                bm.CashFlowEvent(
                    date=date.fromisoformat(event["date"]),
                    amount=event["amount"],
                    label=f"[{account_id}] {event['label']}",
                    source=account_id,
                    kind="WHEEL",
                )
            )

    as_of = max(date.fromisoformat(wr["as_of"]) for _, wr in available)
    terminal_value = sum(wr["terminal_value"] for _, wr in available)
    result = bm.compare_to_benchmark(pooled_events, terminal_value, None, as_of)

    if result.actual_xirr_pct is None:
        return {
            "available": False,
            "warnings": warnings
            + ["combined wheel cash flows don't yet have enough sign variation (money in AND money out) to solve for a rate"],
        }

    return {
        "available": True,
        "warnings": warnings,
        "as_of": as_of.isoformat(),
        "terminal_value": round(terminal_value, 2),
        "xirr_pct": round(result.actual_xirr_pct, 2),
        "cash_flow_events": [
            {"date": event.date.isoformat(), "amount": round(event.amount, 2), "label": event.label}
            for event in sorted(pooled_events, key=lambda event: event.date)
        ],
    }
