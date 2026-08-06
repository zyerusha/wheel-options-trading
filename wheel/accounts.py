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
from wheel.api import Dashboard, Filters, discover_exports, discover_multi_account_exports
from wheel.positions import discover_position_snapshots, latest_snapshot_per_account, load_snapshots

if TYPE_CHECKING:  # pragma: no cover
    pass

DEFAULT_ACCOUNT_ID = "default"
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


@dataclass(frozen=True)
class AccountConfig:
    """Parsed ``data/accounts.json``. Every field is optional and defaults empty."""

    folders: dict[str, str] = field(default_factory=dict)  # folder id -> account number
    ignore: list[str] = field(default_factory=list)  # account number or "Account name", verbatim
    default_account: str | None = None  # account id the UI should open to, instead of "combined"
    default_range: str | None = None  # date-range preset the UI should open to, instead of "All"

    def folder_account(self, folder_id: str) -> str | None:
        """``folders[folder_id]``, matched case-insensitively.

        ``data/<folder>/`` names are whatever casing the OS happened to
        create, and Windows (unlike the dict lookup this wraps) treats
        filenames case-insensitively -- a config entry like ``"JOINT"``
        should still find an actual folder named ``Joint`` rather than
        silently failing to match.
        """
        if folder_id in self.folders:
            return self.folders[folder_id]
        folder_id_lower = folder_id.lower()
        for key, value in self.folders.items():
            if key.lower() == folder_id_lower:
                return value
        return None


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

    unknown_keys = set(raw) - {"folders", "ignore", "default_account", "default_range"}
    if unknown_keys:
        warnings.append(f"{ACCOUNT_CONFIG_FILENAME}: ignoring unknown key(s) {', '.join(sorted(unknown_keys))}")

    return (
        AccountConfig(folders=folders, ignore=ignore, default_account=default_account, default_range=default_range),
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


def _unique_account_id(base: str, taken: dict) -> str:
    """``base``, or ``base-2``/``base-3``/... if it collides with an existing
    account id (including the reserved ``"default"``).
    """
    if base != DEFAULT_ACCOUNT_ID and base not in taken:
        return base
    n = 2
    candidate = f"{base}-{n}"
    while candidate in taken or candidate == DEFAULT_ACCOUNT_ID:
        n += 1
        candidate = f"{base}-{n}"
    return candidate


def _dashboard_account_name(dashboard: Dashboard) -> str | None:
    if not dashboard.snapshots:
        return None
    latest = max(latest_snapshot_per_account(dashboard.snapshots).values(), key=lambda snapshot: snapshot.as_of)
    return latest.account_name


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
    if not snapshots:
        return None
    return max(latest_snapshot_per_account(snapshots).values(), key=lambda snapshot: snapshot.as_of).account_number


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
        """mtimes of every subfolder account's files, plus ``accounts.json``'s
        own mtime so editing the config alone (no CSV touched) still triggers
        a rebuild -- the default account's own staleness is handled by
        whatever supplies it via :meth:`set_default_dashboard`, not by this
        fingerprint.
        """
        parts = []
        if os.path.isdir(self.base_dir):
            for entry in sorted(os.listdir(self.base_dir)):
                path = os.path.join(self.base_dir, entry)
                if not os.path.isdir(path):
                    continue
                files = discover_exports([path]) + discover_position_snapshots([path])
                parts.append((entry, tuple((f, os.path.getmtime(f)) for f in files)))
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
            for account_dir in account_dirs:
                number = _resolve_account_number(account_dir, config.folder_account(account_dir.id))
                key = f"#{number}" if number else f"@{account_dir.id}"
                groups.setdefault(key, []).append(account_dir)

            accounts: dict[str, Dashboard] = {}
            warnings: list[str] = list(config_warnings)
            claimed_numbers: set[str] = set()
            number_to_id: dict[str, str] = {}  # lets "default_account" name an account number, not just an id

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

                resolved_number = key[1:] if key.startswith("#") else None
                configured_number = config.folder_account(account_dir.id)
                # A configured folder's own Positions data may live only in a
                # shared/"all accounts" download rather than a file inside its
                # own folder -- widen the search to every Positions file found
                # anywhere, trusting the account_number filter below to keep
                # only this folder's own rows out of whatever it finds.
                position_paths = all_position_paths if configured_number else account_dir.position_paths

                if account_dir.id == DEFAULT_ACCOUNT_ID and self._default_dashboard is not None:
                    if resolved_number:
                        claimed_numbers.add(resolved_number)
                        number_to_id[resolved_number] = DEFAULT_ACCOUNT_ID
                        if _is_ignored(resolved_number, _dashboard_account_name(self._default_dashboard), config.ignore):
                            continue
                    accounts[DEFAULT_ACCOUNT_ID] = self._default_dashboard
                    continue
                if not account_dir.history_paths and not position_paths:
                    continue
                try:
                    dashboard = Dashboard(
                        account_dir.history_paths,
                        position_paths=position_paths,
                        account_number=resolved_number,
                    )
                except ValueError as error:
                    warnings.append(f"account '{account_dir.id}': {error}")
                    continue

                if resolved_number:
                    claimed_numbers.add(resolved_number)
                    number_to_id[resolved_number] = account_dir.id
                if _is_ignored(resolved_number, _dashboard_account_name(dashboard), config.ignore):
                    continue
                accounts[account_dir.id] = dashboard

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
            if default_account_id and default_account_id not in accounts and default_account_id != "combined":
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
            if dashboard.snapshots:
                latest = max(
                    latest_snapshot_per_account(dashboard.snapshots).values(),
                    key=lambda snapshot: snapshot.as_of,
                )
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
        if not account_id or account_id.lower() == "combined":
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
            "reconciliation": _combine_reconciliation(payloads),
            "net_worth": net_worth,
            "benchmark": _combine_benchmark(payloads),
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
        "account_id": "combined",
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
            bucket = buckets.setdefault(point["date"], {"put": 0.0, "stock": 0.0, "call": 0.0, "long": 0.0})
            for field_name in ("put", "stock", "call", "long"):
                bucket[field_name] += point.get(field_name) or 0.0

    series: list[dict] = []
    for day in sorted(buckets):
        values = buckets[day]
        total = round(values["put"] + values["stock"] + values["call"] + values["long"], 2)
        series.append(
            {
                "date": day,
                "put": round(values["put"], 2),
                "stock": round(values["stock"], 2),
                "call": round(values["call"], 2),
                "long": round(values["long"], 2),
                "total": total,
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
    }

    combined["days_span"] = (
        max((date.fromisoformat(combined["last_date"]) - date.fromisoformat(combined["first_date"])).days, 1)
        if combined["first_date"] and combined["last_date"]
        else 0
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
    engaged = [point["total"] for point in capital_series if point["total"] and point["total"] > 1e-9]
    avg_capital = sum(engaged) / len(engaged) if engaged else 0.0
    combined["capital_deployed_now"] = round(capital_series[-1]["total"], 2) if capital_series else 0.0
    combined["peak_capital"] = round(max((point["total"] for point in capital_series), default=0.0), 2)
    combined["avg_capital"] = round(avg_capital, 2)

    if avg_capital > 1e-9 and combined["days_span"]:
        roi_wheel = 100.0 * combined["option_realized_pl"] / avg_capital
        scale = 365.0 / combined["days_span"]
        combined["roi_on_avg_wheel_pct"] = round(roi_wheel, 2)
        combined["annualized_wheel_roc_pct"] = round(roi_wheel * scale, 2)
    else:
        combined["roi_on_avg_wheel_pct"] = None
        combined["annualized_wheel_roc_pct"] = None

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

    series_by_date: dict[str, dict[str, Any]] = {}
    for account_id, benchmark_payload in available:
        for point in benchmark_payload["series"]:
            bucket = series_by_date.setdefault(point["as_of"], {"actual": 0.0, "benchmark": 0.0, "benchmark_known": True})
            bucket["actual"] += point["actual_value"] or 0.0
            if point["benchmark_value"] is None:
                bucket["benchmark_known"] = False
            else:
                bucket["benchmark"] += point["benchmark_value"]

    series = [
        {
            "as_of": day,
            "actual_value": round(values["actual"], 2),
            "benchmark_value": round(values["benchmark"], 2) if values["benchmark_known"] else None,
        }
        for day, values in sorted(series_by_date.items())
    ]

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
