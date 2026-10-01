"""Zero-dependency dashboard server.

    python -m wheel.serve [--csv FILE] [--port 8765] [--no-browser] [--reopen-browser]
    python -m wheel.serve --hosted   # multi-tenant browser mode, see wheel.sessions

Routes
------
``GET  /``                 the dashboard page
``GET  /api/dashboard``    filtered JSON  (?tickers=MU,QQQ&start=&end=&status=)
``GET  /api/health``       liveness plus the cash reconciliation verdict
``GET  /api/datasets``     exports available to load, and which one is active
``POST /api/upload``       accept a new Fidelity CSV (``X-Account`` targets one
                           account's own folder; default is the default account)
``POST /api/select``       switch to an export already on disk (default account)

The active CSV is parsed once and re-parsed only when its mtime changes, so
editing the export and refreshing the page is enough to pick it up.

Two run modes, one shared processing pipeline (see ``Workspace``):

* Local/Docker-mount mode (default) -- one shared ``Workspace`` for the whole
  process, scoped to ``WHEEL_DATA_DIR``/the project root, exactly as before.
  Binds 127.0.0.1 unless told otherwise. A single trusted user, so uploads are
  still treated as untrusted input (filename reduced to a bare basename, body
  size-capped, unrecognized files removed again) but not isolated from one
  another.
* Hosted mode (``WHEEL_MODE=hosted`` / ``--hosted``) -- one isolated
  ``Workspace`` per browser, identified by an anonymous ``wheel_session``
  cookie (see ``wheel.sessions.SessionManager``), each with its own directory,
  own quota, and its own idle expiry. Meant to run behind an HTTPS-terminating
  reverse proxy; the app itself never terminates TLS.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import threading
import time
import webbrowser
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wheel.accounts import COMBINED_ACCOUNT_ID, DEFAULT_ACCOUNT_ID, AccountRegistry  # noqa: E402
from wheel.api import (  # noqa: E402
    Dashboard,
    Filters,
    discover_exports,
    discover_multi_account_exports,
    looks_like_export,
    looks_like_multi_account_export,
)
from wheel import exporter  # noqa: E402
from wheel.closed_lots import looks_like_closed_lots  # noqa: E402
from wheel.paths import DATA_DIR  # noqa: E402
from wheel.positions import discover_position_snapshots, looks_like_position_snapshot  # noqa: E402
from wheel.sessions import (  # noqa: E402
    DEFAULT_SESSION_MAX_BYTES,
    DEFAULT_SESSION_MAX_FILES,
    DEFAULT_SESSION_TTL_SECONDS,
    SESSION_COOKIE_NAME,
    Session,
    SessionManager,
    SessionQuotaError,
)
from wheel.ui_config import read_config, write_config  # noqa: E402

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(PACKAGE_DIR, "static")
PROJECT_ROOT = os.path.dirname(PACKAGE_DIR)
# Absolute: <repo>/data by default, or $WHEEL_DATA_DIR when set. All uploads and
# market-data caches are written here; it is the volume mount point in Docker.
UPLOAD_DIR = DATA_DIR

MAX_UPLOAD_BYTES = 32 * 1024 * 1024
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]")


class DatasetError(ValueError):
    """An upload or selection that must not become the active dataset."""


def safe_filename(raw: str) -> str:
    """Reduce a client-supplied name to a bare, harmless .csv basename."""
    name = os.path.basename((raw or "").strip().replace("\\", "/"))
    name = _SAFE_NAME.sub("_", name).strip(". ")
    if not name:
        name = f"upload-{datetime.now():%Y%m%d-%H%M%S}.csv"
    if not name.lower().endswith(".csv"):
        name += ".csv"
    return name


def safe_account_name(raw: str) -> str:
    """Reduce a client-supplied account id to a bare, harmless folder name.

    Unlike ``safe_filename`` this has no safe fallback for an empty/unusable
    result -- callers must reject that case rather than silently inventing a
    folder name, since a wrong guess would scatter a user's upload into a
    folder they never asked for.
    """
    name = os.path.basename((raw or "").strip().replace("\\", "/"))
    return _SAFE_NAME.sub("_", name).strip(". ")


def _find_existing_duplicate(
    body: bytes,
    *,
    walk_dirs: Sequence[str] | None = None,
    flat_dirs: Sequence[str] | None = None,
) -> str | None:
    """A CSV already on disk under ``walk_dirs`` (scanned recursively -- any
    account subfolder) or ``flat_dirs`` (scanned shallowly) whose content is
    byte-identical to ``body``, if any.

    Defaults (both omitted) are the current ``UPLOAD_DIR``/``PROJECT_ROOT``
    globals, read at call time -- the local/Docker-mount mode's own dedup
    scope, unchanged from before this function grew a session-scoped variant.
    A hosted session passes its own single ``base_dir`` as ``walk_dirs`` and
    no ``flat_dirs``, so one session never dedups against -- or thereby
    reveals the existence of -- another session's files.

    The browser's file picker has no notion of "this file is already on
    disk" -- it only ever hands the server bytes and a name -- so re-selecting
    a file that already lives in ``data/<account>/`` would otherwise get a
    second, identical copy written into the upload target every time Load is
    pressed. Comparing content rather than name/path catches that regardless
    of which folder the picker happened to browse into.
    """
    if walk_dirs is None:
        walk_dirs = (UPLOAD_DIR,)
    if flat_dirs is None:
        flat_dirs = (PROJECT_ROOT,)

    candidates: list[str] = []
    for directory in walk_dirs:
        if not os.path.isdir(directory):
            continue
        for root, _dirs, entries in os.walk(directory):
            candidates.extend(
                os.path.join(root, entry) for entry in entries if entry.lower().endswith(".csv")
            )
    for directory in flat_dirs:
        if not os.path.isdir(directory):
            continue
        candidates.extend(
            os.path.join(directory, entry)
            for entry in os.listdir(directory)
            if entry.lower().endswith(".csv") and os.path.isfile(os.path.join(directory, entry))
        )

    for path in candidates:
        try:
            if os.path.getsize(path) != len(body):
                continue
            with open(path, "rb") as handle:
                same = handle.read() == body
        except OSError:
            continue
        if same:
            return path
    return None


def _write_uploads(
    target_dir: str,
    files: list[tuple[str, bytes]],
    written: list[str],
    *,
    walk_dirs: Sequence[str] | None = None,
    flat_dirs: Sequence[str] | None = None,
) -> list[str]:
    """Write ``files`` into ``target_dir`` (deduping against files already on
    disk under ``walk_dirs``/``flat_dirs`` -- see
    :func:`_find_existing_duplicate`, whose same defaults apply here), then
    reject the batch as a whole if it contains a multi-account export or
    anything unrecognized.

    Shared by :meth:`DashboardState.accept_uploads` (the default bucket) and
    :meth:`Handler._accept_account_upload` (a named account's own folder) --
    the two differ only in which directory this writes into and what happens
    to ``resolved`` afterward (one activates it as the transaction-history
    dataset; the other just needs the registry to notice new files exist).

    ``written`` is populated in place with every freshly created path (not
    ones reused from an existing duplicate) as this function goes, rather
    than returned alongside ``resolved`` -- so it stays populated for the
    caller to roll back even when this function raises partway through (this
    function does not roll anything back itself, since a caller that goes on
    to run its own validation after this returns needs those same files
    rolled back on *that* validation's failure too; see
    :func:`_rollback_uploads`).
    """
    if not files:
        raise DatasetError("no file content was sent")

    os.makedirs(target_dir, exist_ok=True)
    resolved: list[str] = []  # this upload's files, written or reused from an existing duplicate
    for filename, body in files:
        if not body:
            raise DatasetError(f"{filename or 'upload'} was empty")
        duplicate = _find_existing_duplicate(body, walk_dirs=walk_dirs, flat_dirs=flat_dirs)
        if duplicate:
            resolved.append(duplicate)
            continue
        name = safe_filename(filename)
        target = os.path.join(target_dir, name)
        if os.path.exists(target):
            stem, extension = os.path.splitext(name)
            target = os.path.join(target_dir, f"{stem}-{datetime.now():%Y%m%d-%H%M%S%f}{extension}")
        with open(target, "wb") as handle:
            handle.write(body)
        written.append(target)
        resolved.append(target)

    multi_account = [path for path in resolved if looks_like_multi_account_export(path)]
    if multi_account:
        names = ", ".join(os.path.basename(path) for path in multi_account)
        raise DatasetError(
            f"{names}: this looks like Fidelity's multi-account transaction history export "
            "(separate 'Account'/'Account Number' columns); not supported yet. "
            "Download a per-account History_for_Account_*.csv export instead."
        )

    unrecognized = [
        path
        for path in resolved
        if not looks_like_export(path)
        and not looks_like_position_snapshot(path)
        and not looks_like_closed_lots(path)
    ]
    if unrecognized:
        names = ", ".join(os.path.basename(path) for path in unrecognized)
        raise DatasetError(f"not a recognized Fidelity export: {names}")

    return resolved


def _rollback_uploads(written: list[str]) -> None:
    """Remove every freshly written file in ``written`` -- best-effort, since
    a file that's already gone (or was never writable to begin with) leaves
    nothing to roll back.
    """
    for path in written:
        try:
            os.remove(path)
        except OSError:
            pass


class DashboardState:
    """Holds the "default" account's active transaction-history dataset.

    Position snapshots are deliberately not part of this class's own state --
    ``Dashboard(csv_paths)`` is always called with the same directories this
    instance itself scans (see ``_scan_dirs``), so it auto-discovers every
    Positions export sitting there on its own (see
    ``wheel.api.discover_position_snapshots``). That is what lets an uploaded
    or hand-edited Positions file take effect without needing its own
    activate/select step -- there is nothing to choose between, unlike
    transaction-history exports, which really can overlap and need combining.

    ``upload_dir``/``extra_dirs`` scope every discovery/dedup this instance
    does. Omitted (the local/Docker-mount default), they resolve to the
    module's own ``UPLOAD_DIR``/``PROJECT_ROOT`` globals -- read here, at
    construction time, not bound as literal defaults, so this keeps working
    unchanged for the single shared instance ``serve()`` builds, and for any
    existing test that patches those globals before constructing one. A
    hosted session instead passes its own isolated directory and no
    ``extra_dirs``, so it never discovers (or dedups against) anything outside
    its own workspace.
    """

    def __init__(
        self,
        csv_path: str | list[str],
        *,
        upload_dir: str | None = None,
        extra_dirs: Sequence[str] | None = None,
    ):
        paths = [csv_path] if isinstance(csv_path, str) else list(csv_path)
        self.csv_paths = [os.path.abspath(path) for path in paths]
        self._upload_dir = upload_dir if upload_dir is not None else UPLOAD_DIR
        self._extra_dirs = tuple(extra_dirs) if extra_dirs is not None else (PROJECT_ROOT,)
        self._lock = threading.Lock()
        self._stamp: tuple | None = None
        self._dashboard: Dashboard | None = None

    @property
    def csv_path(self) -> str | None:
        # None, not IndexError, when the default account is genuinely empty --
        # e.g. every account lives in its own data/<account>/ subfolder and
        # nothing is left loose in the project root or data/.
        return self.csv_paths[0] if self.csv_paths else None

    def _scan_dirs(self) -> tuple[str, ...]:
        return (self._upload_dir, *self._extra_dirs)

    def _fingerprint(self) -> tuple:
        # Position snapshots are auto-discovered, not tracked in csv_paths, so
        # their own mtimes have to be watched here too -- otherwise editing or
        # replacing one on disk would never trigger a rebuild.
        return (
            tuple((path, os.path.getmtime(path)) for path in self.csv_paths),
            tuple(
                (path, os.path.getmtime(path))
                for path in discover_position_snapshots(self._scan_dirs())
            ),
        )

    def get(self) -> Dashboard:
        with self._lock:
            stamp = self._fingerprint()
            if self._dashboard is None or stamp != self._stamp:
                self._dashboard = Dashboard(
                    self.csv_paths, position_paths=discover_position_snapshots(self._scan_dirs())
                )
                self._stamp = stamp
            return self._dashboard

    # ---- dataset management ----

    def search_paths(self) -> list[str]:
        """Every CSV the UI is allowed to switch to."""
        found: list[str] = []
        for directory in self._scan_dirs():
            if not os.path.isdir(directory):
                continue
            for entry in sorted(os.listdir(directory)):
                path = os.path.join(directory, entry)
                if entry.lower().endswith(".csv") and looks_like_export(path):
                    found.append(path)
        found.extend(path for path in self.csv_paths if os.path.isfile(path))
        # Preserve order while removing duplicates (a file in both scans).
        return list(dict.fromkeys(os.path.abspath(path) for path in found))

    def datasets(self) -> list[dict]:
        rows = []
        for path in self.search_paths():
            try:
                stat = os.stat(path)
            except OSError:
                continue
            rows.append(
                {
                    "path": path,
                    "name": os.path.basename(path),
                    "folder": "data" if os.path.dirname(path) == self._upload_dir else ".",
                    "size_kb": round(stat.st_size / 1024, 1),
                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                    "active": path in self.csv_paths,
                }
            )
        return rows

    def unsupported_datasets(self) -> list[dict]:
        """Multi-account exports (e.g. an ``Accounts_History.csv`` download)
        found alongside the normal ones -- surfaced so a file that's silently
        excluded from :meth:`search_paths` doesn't just vanish with no
        explanation of why it never shows up as loadable.
        """
        rows = []
        for path in discover_multi_account_exports(self._scan_dirs()):
            try:
                stat = os.stat(path)
            except OSError:
                continue
            rows.append(
                {
                    "name": os.path.basename(path),
                    "folder": "data" if os.path.dirname(path) == self._upload_dir else ".",
                    "size_kb": round(stat.st_size / 1024, 1),
                    "reason": "multi-account transaction history export; not supported yet",
                }
            )
        return rows

    @staticmethod
    def _validate(paths: list[str]) -> Dashboard:
        """Parse candidate transaction-history files, rejecting anything unusable.

        ``paths`` must already be transaction-history files only -- callers
        (``switch``, ``accept_uploads``) are responsible for keeping Positions
        exports out of it, since those are never "activated" the same way (see
        the class docstring). ``position_paths=None`` still auto-discovers
        whatever Positions exports already exist on disk.

        An empty ``paths`` means "no transaction-history change requested" --
        the legitimate positions-only-upload case (see ``accept_uploads``'s
        own docstring), where there is nothing transaction-shaped to
        validate. A *non-empty* ``paths`` that parses to zero transactions is
        always rejected, regardless of what Positions snapshots happen to
        already exist elsewhere in the project: ``Dashboard(paths)`` auto-
        discovers every Positions file on disk project-wide (not just ones
        related to this selection), so checking ``dashboard.snapshots`` here
        would validate an unrelated fact rather than this actual upload.
        """
        try:
            dashboard = Dashboard(paths)
        except Exception as error:
            raise DatasetError(f"{type(error).__name__}: {error}") from error
        if paths and not dashboard.transactions:
            raise DatasetError("parsed successfully but contains no transactions")
        return dashboard

    def _activate(self, paths: list[str], dashboard: Dashboard) -> None:
        with self._lock:
            self.csv_paths = paths
            self._dashboard = dashboard
            self._stamp = self._fingerprint()

    def switch(self, paths: list[str]) -> Dashboard:
        """Make a set of existing files the active dataset, after validating it."""
        if not paths:
            raise DatasetError("no exports selected")

        allowed = set(self.search_paths())
        targets: list[str] = []
        for path in paths:
            target = os.path.abspath(path)
            if target not in allowed:
                raise DatasetError(f"{os.path.basename(target)} is not in the project or data folder")
            if not os.path.isfile(target):
                raise DatasetError(f"{os.path.basename(target)} no longer exists")
            targets.append(target)
        targets = list(dict.fromkeys(targets))

        dashboard = self._validate(targets)
        self._activate(targets, dashboard)
        return dashboard

    def accept_uploads(self, files: list[tuple[str, bytes]], keep_current: bool = False) -> Dashboard:
        """Save uploaded CSVs and activate them, or leave the current set alone.

        Validation happens on the combined set *before* it replaces anything, and
        files that fail are removed rather than left to clutter the picker.

        A Positions export needs no activation step: it is written to disk like
        any other upload, but never added to ``targets`` (the transaction-history
        activation list), because ``Dashboard`` auto-discovers every Positions
        file already on disk on its own. So uploading one, alone, neither
        requires ``keep_current`` nor disturbs whatever transaction-history
        exports are currently active.
        """
        written: list[str] = []  # freshly created files -- rolled back on failure
        try:
            resolved = _write_uploads(
                self._upload_dir, files, written, walk_dirs=(self._upload_dir,), flat_dirs=self._extra_dirs
            )

            new_history = [path for path in resolved if looks_like_export(path)]
            if keep_current:
                targets = self.csv_paths + new_history
            else:
                # A positions-only upload (new_history empty) leaves the active
                # transaction-history set alone rather than wiping it out --
                # only a new *history* file is meant to replace it.
                targets = new_history or self.csv_paths
            targets = list(dict.fromkeys(targets))
            dashboard = self._validate(targets)
        except DatasetError:
            _rollback_uploads(written)
            raise

        self._activate(targets, dashboard)
        return dashboard


@dataclass
class Workspace:
    """Everything one tenant's requests are served from: a directory, an
    active-dataset ``DashboardState``, and an ``AccountRegistry`` over that
    same directory.

    Local/Docker-mount mode builds exactly one of these at startup, scoped to
    ``UPLOAD_DIR``/``PROJECT_ROOT`` -- the same directories this module has
    always scanned, so its behavior is unchanged. Hosted mode builds one per
    browser session (see ``wheel.sessions.SessionManager``), scoped to that
    session's own directory only, with no project-root merge -- see
    :func:`serve`. Either way, both modes construct a ``Workspace`` the same
    way and every route handler reaches its data through ``Handler.workspace``
    (or the ``state``/``registry`` properties), so no processing code differs
    between the two.
    """

    base_dir: str
    walk_dirs: tuple[str, ...]
    flat_dirs: tuple[str, ...]
    state: DashboardState
    registry: AccountRegistry

    @classmethod
    def build(cls, paths: list[str], *, base_dir: str, extra_dirs: Sequence[str] = ()) -> "Workspace":
        state = DashboardState(paths, upload_dir=base_dir, extra_dirs=extra_dirs)
        registry = AccountRegistry(base_dir=base_dir, extra_dirs=extra_dirs)
        if paths:
            registry.set_default_dashboard(state.get())  # fail fast on a bad file, before serving anything
        return cls(
            base_dir=base_dir,
            walk_dirs=(base_dir,),
            flat_dirs=tuple(extra_dirs),
            state=state,
            registry=registry,
        )


# The browser closing a tab, navigating away, or (per resetSelectionState's
# own account-switch dedupe note) superseding an in-flight fetch with a newer
# one all abort the socket mid-response. That's a client hanging up, not a
# server fault: nothing to fix, and the socket is already dead so there is no
# response left to send. Caught separately from `except Exception` so it gets
# one quiet log line instead of a traceback plus a doomed second write
# attempt at an error body (which is what used to happen: `_send_json`'s own
# `self.wfile.write` inside the generic handler's error path would raise the
# same exception again, this time unhandled).
_CLIENT_DISCONNECTED = (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)


class Handler(BaseHTTPRequestHandler):
    # Local/Docker-mount mode: injected once by serve() -- the one shared
    # Workspace for the whole process. Hosted mode: left None; each request
    # resolves its own session's Workspace instead (see _resolve_workspace).
    workspace: Workspace | None = None
    session_manager: SessionManager | None = None  # injected by serve() only in hosted mode
    _session: Session | None = None  # this request's session, if session_manager is set
    server_version = "WheelDashboard/1.0"

    # ---- plumbing ----

    @property
    def state(self) -> DashboardState:
        return self.workspace.state

    @property
    def registry(self) -> AccountRegistry:
        return self.workspace.registry

    def _session_token_from_cookie(self) -> str | None:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        jar = SimpleCookie()
        try:
            jar.load(raw)
        except Exception:
            return None
        morsel = jar.get(SESSION_COOKIE_NAME)
        return morsel.value if morsel else None

    def _resolve_workspace(self) -> Workspace:
        """The Workspace this request should be served from.

        Local/Docker-mount mode (``session_manager is None``): always the one
        shared instance ``serve()`` built -- unchanged from before hosted mode
        existed. Hosted mode: the Workspace belonging to whatever
        ``wheel_session`` cookie this request presented, or a brand-new,
        empty one if that cookie is missing, unknown, expired, or forged --
        never another session's. See ``wheel.sessions.SessionManager``.
        """
        if self.session_manager is None:
            self._session = None
            return self.workspace
        self._session = self.session_manager.get_or_create(self._session_token_from_cookie())
        return self._session.workspace

    def _apply_session_cookie(self) -> None:
        """Re-issue the session cookie on every response, in this one place,
        so every route gets a sliding-TTL cookie without any route-specific
        code. A no-op in local/Docker-mount mode (``self._session is None``).
        """
        if self._session is None:
            return
        cookie = SimpleCookie()
        cookie[SESSION_COOKIE_NAME] = self._session.token
        morsel = cookie[SESSION_COOKIE_NAME]
        morsel["path"] = "/"
        morsel["httponly"] = True
        morsel["samesite"] = "Lax"
        # Floored at 1 -- Max-Age=0 tells the browser to delete the cookie
        # immediately, which a sub-second TTL would otherwise round down to.
        morsel["max-age"] = str(max(1, int(self.session_manager.ttl_seconds)))
        # The app itself never terminates TLS (hosted mode expects an
        # HTTPS-terminating reverse proxy in front of it), so this is the only
        # signal available for whether the connection the browser actually
        # made was secure.
        if self.headers.get("X-Forwarded-Proto", "").lower() == "https":
            morsel["secure"] = True
        self.send_header("Set-Cookie", morsel.OutputString())

    def _check_session_quota(self, added_bytes: int, added_files: int) -> None:
        """Reject (as a normal, user-facing DatasetError) an upload that would
        push this request's session over its configured storage/file-count
        quota. A no-op in local/Docker-mount mode. Checked against the raw
        incoming batch size, before de-dup -- a conservative upper bound, so a
        session can never be under-counted, only occasionally asked to retry
        a batch that would have partly de-duped away.
        """
        if self._session is None:
            return
        try:
            self._session.check_quota(
                added_bytes,
                added_files,
                max_bytes=self.session_manager.max_session_bytes,
                max_files=self.session_manager.max_session_files,
            )
        except SessionQuotaError as error:
            raise DatasetError(str(error)) from error

    def _record_session_usage(self, added_bytes: int, added_files: int) -> None:
        if self._session is not None:
            self._session.record_usage(added_bytes, added_files)

    def _sync_registry(self) -> None:
        """Keep the multi-account registry's "default" account pointed at
        whatever ``DashboardState`` currently has active, so an upload or a
        dataset switch is reflected in ``/api/dashboard``/``/api/accounts``
        without a second, redundant file scan -- then rescan every account
        folder for anything new.

        A ``ValueError`` from ``state.get()`` means the default account (loose
        files in the project root/``data``) is genuinely empty -- a user who
        keeps every account in its own ``data/<account>/`` subfolder, say.
        That's fine; the registry's own discovery already omits "default" from
        the account list in that case, so there's simply nothing to point it at.

        ``set_default_dashboard`` only forces a full ``refresh()`` when the
        default bucket's own dashboard identity changes -- a new or edited file
        directly in the project root/``data``. A new file dropped into an
        *existing* ``data/<account>/`` subfolder, or a brand-new subfolder,
        never touches that identity, so it would otherwise stay invisible
        until the process restarts. ``refresh()`` is called unconditionally
        here to close that gap; it is cheap when nothing changed (its own
        fingerprint is just file mtimes) and only re-parses the accounts whose
        files actually moved, so paying for a stat pass on every request
        (dashboard load, account switch, a plain page refresh) is worth always
        seeing whatever is on disk right now.
        """
        try:
            dashboard = self.state.get()
        except ValueError:
            dashboard = None
        if dashboard is not None:
            self.registry.set_default_dashboard(dashboard)
        self.registry.refresh()

    def log_message(self, fmt: str, *args) -> None:
        # One tidy line per request instead of BaseHTTPRequestHandler's noise.
        sys.stderr.write(f"  {self.command} {self.path} -> {args[1] if len(args) > 1 else ''}\n")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self._apply_session_cookie()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: dict, status: int = 200) -> None:
        self._send(status, json.dumps(payload, default=str).encode("utf-8"), "application/json; charset=utf-8")

    def _send_file(self, filename: str, content_type: str) -> None:
        path = os.path.join(STATIC_DIR, filename)
        if not os.path.isfile(path):
            self._send_json({"error": f"{filename} not found"}, 404)
            return
        with open(path, "rb") as handle:
            self._send(200, handle.read(), content_type)

    def _send_csv(self, text: str, filename: str) -> None:
        body = text.encode("utf-8")
        self.send_response(200)
        self._apply_session_cookie()
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ---- routes ----

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        parsed = urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"

        try:
            self.workspace = self._resolve_workspace()
            if route == "/":
                self._send_file("index.html", "text/html; charset=utf-8")
            elif route == "/app.js":
                self._send_file("app.js", "application/javascript; charset=utf-8")
            elif route == "/styles.css":
                self._send_file("styles.css", "text/css; charset=utf-8")
            elif route == "/wheel-strategy.png":
                self._send_file("wheel-strategy.png", "image/png")
            elif route == "/logo.svg":
                self._send_file("logo.svg", "image/svg+xml")
            elif route == "/api/dashboard":
                self._sync_registry()
                query = parse_qs(parsed.query)
                filters = Filters.from_query(query)
                account_id = (query.get("account") or [None])[0]
                try:
                    self._send_json(self.registry.build(account_id, filters))
                except KeyError:
                    self._send_json({"error": f"unknown account {account_id!r}"}, 404)
            elif route == "/api/health":
                self._sync_registry()
                try:
                    payload = self.registry.build(_preferred_account(self.registry))
                except KeyError:
                    # A concurrent refresh() (another thread, between
                    # _preferred_account()'s own read of list_accounts() and
                    # this build()) can remove or rename the account it just
                    # picked -- Combined always exists once any account does,
                    # so this is a liveness check, not a specific account's
                    # data, and always has an answer to fall back to.
                    payload = self.registry.build(COMBINED_ACCOUNT_ID)
                self._send_json(
                    {
                        "status": "ok",
                        "source": payload["meta"]["source"],
                        "transactions": payload["meta"]["transactions_total"],
                        "cycles": len(payload["cycles"]),
                        "reconciliation": payload["reconciliation"],
                    }
                )
            elif route.startswith("/api/export/") and route.endswith(".csv"):
                self._handle_export(route[len("/api/export/") : -len(".csv")], parse_qs(parsed.query))
            elif route == "/api/datasets":
                self._send_json(self._dataset_listing())
            elif route == "/api/accounts":
                self._sync_registry()
                self._send_json({"accounts": self.registry.list_accounts()})
            elif route == "/api/config":
                self._send_json(read_config(self.workspace.base_dir))
            else:
                self._send_json({"error": "not found", "path": route}, 404)
        except _CLIENT_DISCONNECTED:
            sys.stderr.write(f"  {self.command} {route} -> client disconnected\n")
        except Exception as error:  # pragma: no cover - surfaced to the browser
            import traceback

            traceback.print_exc()
            self._send_json({"error": str(error), "type": type(error).__name__}, 500)

    def do_POST(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        route = urlparse(self.path).path.rstrip("/") or "/"
        try:
            self.workspace = self._resolve_workspace()
            if route == "/api/upload":
                self._handle_upload()
            elif route == "/api/select":
                self._handle_select()
            elif route == "/api/config":
                self._handle_config()
            else:
                self._send_json({"error": "not found", "path": route}, 404)
        except DatasetError as error:
            # A rejected dataset is a user-fixable problem, not a server fault.
            # This write has the same client-gone-mid-response risk as the
            # generic handler below, but a raise here wouldn't reach that
            # sibling `except` clause (a new exception from inside one
            # `except` block isn't matched against the others), hence its own
            # guard.
            try:
                self._send_json({"error": str(error), "active": self.state.csv_path}, 400)
            except _CLIENT_DISCONNECTED:
                sys.stderr.write(f"  {self.command} {route} -> client disconnected\n")
        except _CLIENT_DISCONNECTED:
            sys.stderr.write(f"  {self.command} {route} -> client disconnected\n")
        except Exception as error:  # pragma: no cover - surfaced to the browser
            import traceback

            traceback.print_exc()
            self._send_json({"error": str(error), "type": type(error).__name__}, 500)

    # ---- CSV export ----

    def _handle_export(self, name: str, query: dict) -> None:
        """``/api/export/<name>.csv`` -- flat CSV of one payload list, honouring
        the same ``?account=&tickers=&start=&end=&status=`` filters as
        ``/api/dashboard`` (except the trade log, which is filter-independent)."""
        self._sync_registry()
        filters = Filters.from_query(query)
        account_id = (query.get("account") or [None])[0]
        try:
            payload = self.registry.build(account_id, filters)
        except KeyError:
            self._send_json({"error": f"unknown account {account_id!r}"}, 404)
            return

        if name == "cycles":
            text = exporter.rows_to_csv(payload.get("cycles") or [], exporter.CYCLE_COLUMNS)
        elif name == "tickers":
            text = exporter.rows_to_csv(payload.get("tickers") or [], exporter.TICKER_COLUMNS)
        elif name == "trade-log":
            only = (query.get("wheel") or [None])[0]
            wheels = (payload.get("trade_log") or {}).get("wheels") or []
            text = exporter.rows_to_csv(
                exporter.flatten_trade_log(wheels, only), exporter.TRADE_LOG_COLUMNS
            )
        elif name == "closed-lots":
            lots = (payload.get("realized_gains") or {}).get("lots")
            if not lots:
                self._send_json({"error": "no closed-lots export is loaded"}, 404)
                return
            text = exporter.rows_to_csv(lots, exporter.CLOSED_LOT_COLUMNS)
        else:
            self._send_json({"error": f"unknown export {name!r}"}, 404)
            return

        self._send_csv(text, f"wheel-{name}.csv")

    # ---- dataset endpoints ----

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise DatasetError("no file content was sent")
        if length > MAX_UPLOAD_BYTES:
            raise DatasetError(f"file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
        return self.rfile.read(length)

    def _dataset_listing(self, message: str | None = None) -> dict:
        # The default account can be genuinely empty (every account tucked
        # into its own data/<account>/ folder, nothing loose left over) --
        # that's a valid, supported layout, not an error, so this reports an
        # empty listing rather than raising.
        try:
            dashboard = self.state.get()
        except ValueError:
            dashboard = None
        merge = dashboard.merge if dashboard else None
        payload = {
            "active": list(self.state.csv_paths),
            "active_name": " + ".join(os.path.basename(p) for p in self.state.csv_paths) or "(none)",
            "datasets": self.state.datasets(),
            "unsupported_datasets": self.state.unsupported_datasets(),
            "transactions": len(dashboard.transactions) if dashboard else 0,
            "rows_parsed": merge.rows_parsed if merge else 0,
            "duplicates_removed": merge.duplicates_removed if merge else 0,
            "first_date": merge.first_date.isoformat() if merge and merge.first_date else None,
            "last_date": merge.last_date.isoformat() if merge and merge.last_date else None,
            "upload_dir": self.workspace.base_dir,
        }
        if message:
            payload["message"] = message
        return payload

    def _loaded_message(self, verb: str) -> str:
        dashboard = self.state.get()
        merge = dashboard.merge
        parts = [f"{verb} {len(self.state.csv_paths)} export(s) — {merge.rows_kept} transactions"]
        if merge.duplicates_removed:
            parts.append(f"{merge.duplicates_removed} duplicate rows merged away")
        if merge.first_date and merge.last_date:
            parts.append(f"{merge.first_date} to {merge.last_date}")
        if dashboard.snapshots:
            parts.append(f"{len(dashboard.snapshots)} position snapshot(s)")
        return " · ".join(parts)

    def _read_uploads(self) -> list[tuple[str, bytes]]:
        """Read one or more CSVs from the request body.

        A single file is sent raw with its name in ``X-Filename``.  Several are
        sent as a length-prefixed bundle described by ``X-Files``, which avoids
        pulling in a multipart parser for what is a two-field payload.
        """
        body = self._read_body()
        manifest = self.headers.get("X-Files")
        if not manifest:
            return [(self.headers.get("X-Filename") or "upload.csv", body)]

        try:
            entries = json.loads(manifest)
        except ValueError as error:
            raise DatasetError(f"could not read the file manifest: {error}") from error

        files: list[tuple[str, bytes]] = []
        offset = 0
        for entry in entries:
            size = int(entry.get("size", 0))
            if size < 0 or offset + size > len(body):
                raise DatasetError("file manifest does not match the uploaded data")
            files.append((entry.get("name") or "upload.csv", body[offset : offset + size]))
            offset += size
        if offset != len(body):
            raise DatasetError("file manifest does not match the uploaded data")
        return files

    def _handle_upload(self) -> None:
        account_id = safe_account_name(self.headers.get("X-Account") or "")
        files = self._read_uploads()

        # No account, or explicitly "default"/"combined": today's behavior --
        # goes through DashboardState, which owns the default account's active
        # transaction-history selection. A real named account instead writes
        # straight into its own folder under data/ and never touches
        # DashboardState at all, since a named account has no "active
        # selection" concept -- every file found in its folder is always
        # included (see AccountRegistry).
        if not account_id or account_id.lower() in {DEFAULT_ACCOUNT_ID, COMBINED_ACCOUNT_ID}:
            self._check_session_quota(sum(len(body) for _, body in files), len(files))
            keep = (self.headers.get("X-Keep-Current") or "").lower() in {"1", "true", "yes"}
            self.state.accept_uploads(files, keep_current=keep)
            self._record_session_usage(sum(len(body) for _, body in files), len(files))
            self._send_json(self._dataset_listing(self._loaded_message("Loaded")))
            return

        message = self._accept_account_upload(account_id, files)
        self._sync_registry()
        self._send_json({"message": message, "account": account_id, "accounts": self.registry.list_accounts()})

    def _accept_account_upload(self, account_id: str, files: list[tuple[str, bytes]]) -> str:
        """Write uploaded files directly into one named account's own folder.

        Never lands in ``data/`` root -- each account keeps its own files in
        its own folder (see ``wheel/accounts.py``), so an upload while a
        specific account is selected has to land there too, not in the
        default bucket. The folder is created if this is the first file for a
        brand-new account name.
        """
        self._check_session_quota(sum(len(body) for _, body in files), len(files))
        target_dir = os.path.join(self.workspace.base_dir, account_id)
        written: list[str] = []  # freshly created files -- rolled back on failure
        try:
            resolved = _write_uploads(
                target_dir, files, written, walk_dirs=self.workspace.walk_dirs, flat_dirs=self.workspace.flat_dirs
            )
        except DatasetError:
            _rollback_uploads(written)
            raise
        self._record_session_usage(sum(len(body) for _, body in files), len(files))

        self.registry.refresh(force=True)
        history_count = sum(1 for path in resolved if looks_like_export(path))
        position_count = sum(1 for path in resolved if looks_like_position_snapshot(path))
        closed_lot_count = sum(1 for path in resolved if looks_like_closed_lots(path))
        parts = [f"Loaded into '{account_id}'"]
        if history_count:
            parts.append(f"{history_count} transaction export(s)")
        if position_count:
            parts.append(f"{position_count} position snapshot(s)")
        if closed_lot_count:
            parts.append(f"{closed_lot_count} closed-lots export(s)")
        return " · ".join(parts)

    def _handle_select(self) -> None:
        try:
            request = json.loads(self._read_body().decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as error:
            raise DatasetError(f"could not read request: {error}") from error

        paths = request.get("paths")
        if not paths:
            single = request.get("path")
            paths = [single] if single else []
        if not paths:
            raise DatasetError("no dataset selected")

        self.state.switch(list(paths))
        self._send_json(self._dataset_listing(self._loaded_message("Combined")))

    def _handle_config(self) -> None:
        """``POST /api/config`` -- overwrite ``data/config.json`` with the
        frontend's current settings snapshot. Unlike the dataset endpoints
        above, a bad request here (an empty or unparsable body) is just
        rejected with a 400 -- there's no partial/"active" state to report
        back, and it never touches whatever was saved last."""
        try:
            payload = json.loads(self._read_body().decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as error:  # ValueError also catches DatasetError
            self._send_json({"error": f"could not read request: {error}"}, 400)
            return
        if not isinstance(payload, dict):
            self._send_json({"error": "config must be a JSON object"}, 400)
            return
        write_config(payload, self.workspace.base_dir)
        self._send_json({"ok": True})


def _preferred_account(registry: AccountRegistry) -> str:
    """The account to show by default: "default" if it has anything, else
    "combined" -- which still works when every account lives in its own named
    subfolder and the project root/``data`` has nothing loose in it.
    """
    ids = {row["id"] for row in registry.list_accounts()}
    return DEFAULT_ACCOUNT_ID if DEFAULT_ACCOUNT_ID in ids else COMBINED_ACCOUNT_ID


# How long a "the browser tab is probably still open" marker stays valid.
# Restarting the dev server (edit -> Ctrl-C -> rerun) is the common case
# during development, and popping a fresh tab on every restart -- with the
# old one now just a dead "connection refused" page until the new server
# binds the port -- clutters the browser for no reason: the same tab starts
# working again the moment it does, so there's nothing a new tab offers that
# a refresh doesn't. The marker expires rather than lasting forever so a
# genuinely new session (the next day, after a reboot) still gets a tab.
_BROWSER_MARKER_TTL = 6 * 3600


def _browser_marker_path(port: int) -> str:
    return os.path.join(tempfile.gettempdir(), f"wheel-dashboard-{port}.opened")


def _recently_opened(port: int) -> bool:
    try:
        return time.time() - os.path.getmtime(_browser_marker_path(port)) < _BROWSER_MARKER_TTL
    except OSError:
        return False


def _mark_opened(port: int) -> None:
    try:
        with open(_browser_marker_path(port), "w", encoding="utf-8") as handle:
            handle.write(repr(time.time()))
    except OSError:
        pass  # best-effort -- a marker that fails to write just means the next restart opens a tab again


def serve(
    csv_path: str | list[str] | None = None,
    port: int = 8765,
    open_browser: bool = True,
    reopen_browser: bool = False,
    host: str = "127.0.0.1",
    hosted: bool = False,
    session_ttl_seconds: float = DEFAULT_SESSION_TTL_SECONDS,
    session_max_bytes: int = DEFAULT_SESSION_MAX_BYTES,
    session_max_files: int = DEFAULT_SESSION_MAX_FILES,
) -> None:
    display_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    url = f"http://{display_host}:{port}/"

    if hosted:
        # Multi-tenant browser mode: no single active dataset to auto-discover
        # or validate up front -- each browser gets its own empty Workspace on
        # first contact instead (see wheel.sessions.SessionManager).
        if csv_path:
            print("  --csv is ignored in --hosted mode -- each browser session starts empty\n", file=sys.stderr)
        sessions_root = os.path.join(UPLOAD_DIR, "sessions")
        session_manager = SessionManager(
            sessions_root=sessions_root,
            workspace_factory=lambda base_dir: Workspace.build([], base_dir=base_dir, extra_dirs=()),
            ttl_seconds=session_ttl_seconds,
            max_session_bytes=session_max_bytes,
            max_session_files=session_max_files,
        )
        session_manager.start_background_sweep()
        Handler.workspace = None
        Handler.session_manager = session_manager
        httpd = ThreadingHTTPServer((host, port), Handler)

        print("  mode          hosted (multi-tenant browser upload)")
        print(f"  sessions      {sessions_root}")
        print(
            f"  session TTL   {session_ttl_seconds / 3600:.1f}h idle -- quota "
            f"{session_max_bytes // (1024 * 1024)} MB / {session_max_files} files per session"
        )
        print("  Each browser gets its own isolated, cookie-scoped workspace; nothing")
        print("  persists past its idle TTL or a restart -- see docker-compose.hosted.yml.")
        print(f"\n  Dashboard on  {url}\n  Ctrl-C to stop.\n")
    else:
        if csv_path is None:
            csv_path = discover_exports()
        paths = [csv_path] if isinstance(csv_path, str) else list(csv_path)
        for path in paths:
            if not os.path.isfile(path):
                raise SystemExit(f"CSV not found: {path}")

        # `paths` covers only the project root and data/ directly -- a user who
        # keeps every account in its own data/<account>/ subfolder can legitimately
        # have nothing there at all, so readiness is judged from every discovered
        # account, not just the default bucket.
        workspace = Workspace.build(paths, base_dir=UPLOAD_DIR, extra_dirs=(PROJECT_ROOT,))
        accounts = workspace.registry.list_accounts()

        Handler.workspace = workspace
        Handler.session_manager = None
        httpd = ThreadingHTTPServer((host, port), Handler)

        # No broker export anywhere yet -- a brand-new checkout, or a fresh
        # Docker volume with nothing uploaded. The old behavior was to refuse to
        # even bind the port; now the server starts anyway and the dashboard
        # itself walks the user through exporting from Fidelity and adding the
        # first CSV (see the #no-data-banner in index.html / refreshAccounts()
        # in app.js) -- registry.build() has nothing to build yet, so it's
        # skipped rather than raising.
        if not accounts:
            print(f"  source        (none yet -- {UPLOAD_DIR})")
            print("  No broker export or Portfolio Positions file found. The dashboard")
            print("  will walk you through adding your first Fidelity CSV once it's open.")
            print(f"\n  Dashboard on  {url}\n  Ctrl-C to stop.\n")
        else:
            payload = workspace.registry.build(_preferred_account(workspace.registry))
            reconciliation = payload["reconciliation"]
            meta = payload["meta"]
            print(f"  source        {meta['source'] or '(none in project root/data -- see accounts below)'}")
            if meta["combined"]:
                print(
                    f"  combined      {meta['rows_parsed']} rows -> {meta['rows_kept']} "
                    f"({meta['duplicates_removed']} duplicates merged)"
                )
            if len(accounts) > 1 or DEFAULT_ACCOUNT_ID not in {row["id"] for row in accounts}:
                print(f"  accounts      {len(accounts)}: {', '.join(row['label'] for row in accounts)}")
            print(f"  transactions  {meta['transactions_total']}")
            print(f"  cycles        {len(payload['cycles'])} across {payload['portfolio']['tickers']} tickers")
            print(
                f"  cash check    {'BALANCED' if reconciliation['balanced'] else 'MISMATCH'} "
                f"(delta {reconciliation['delta']})"
            )
            print(f"\n  Dashboard on  {url}\n  Ctrl-C to stop.\n")

    if open_browser and (reopen_browser or not _recently_opened(port)):
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
        _mark_opened(port)
    elif open_browser:
        print(f"  (tab already open from a recent run -- refresh {url}, or pass --reopen-browser)\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped.")
        httpd.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the options wheel dashboard.")
    parser.add_argument(
        "--csv",
        nargs="+",
        default=None,
        help="one or more broker exports; several are combined into one timeline. "
        "Defaults to every export found in . and data/",
    )
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="interface to bind (default 127.0.0.1; use 0.0.0.0 to accept "
        "connections from other machines or containers)",
    )
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    parser.add_argument(
        "--reopen-browser",
        action="store_true",
        help="open a browser tab even if this dashboard was already opened recently "
        "(by default, restarting within a few hours skips it -- see --no-browser to skip always)",
    )
    parser.add_argument(
        "--hosted",
        action="store_true",
        help="multi-tenant browser mode: every visitor gets their own isolated, cookie-scoped "
        "session instead of one shared dataset (same as WHEEL_MODE=hosted)",
    )
    parser.add_argument(
        "--session-ttl-seconds",
        type=float,
        default=None,
        help=f"hosted mode: idle session lifetime (default {DEFAULT_SESSION_TTL_SECONDS:.0f}s, "
        "same as WHEEL_SESSION_TTL_SECONDS)",
    )
    parser.add_argument(
        "--session-max-bytes",
        type=int,
        default=None,
        help=f"hosted mode: max cumulative upload bytes per session (default {DEFAULT_SESSION_MAX_BYTES}, "
        "same as WHEEL_SESSION_MAX_BYTES)",
    )
    parser.add_argument(
        "--session-max-files",
        type=int,
        default=None,
        help=f"hosted mode: max uploaded files per session (default {DEFAULT_SESSION_MAX_FILES}, "
        "same as WHEEL_SESSION_MAX_FILES)",
    )
    args = parser.parse_args()

    hosted = args.hosted or os.environ.get("WHEEL_MODE", "").strip().lower() == "hosted"
    session_ttl_seconds = (
        args.session_ttl_seconds
        if args.session_ttl_seconds is not None
        else float(os.environ.get("WHEEL_SESSION_TTL_SECONDS", DEFAULT_SESSION_TTL_SECONDS))
    )
    session_max_bytes = (
        args.session_max_bytes
        if args.session_max_bytes is not None
        else int(os.environ.get("WHEEL_SESSION_MAX_BYTES", DEFAULT_SESSION_MAX_BYTES))
    )
    session_max_files = (
        args.session_max_files
        if args.session_max_files is not None
        else int(os.environ.get("WHEEL_SESSION_MAX_FILES", DEFAULT_SESSION_MAX_FILES))
    )

    serve(
        args.csv,
        args.port,
        open_browser=not args.no_browser,
        reopen_browser=args.reopen_browser,
        host=args.host,
        hosted=hosted,
        session_ttl_seconds=session_ttl_seconds,
        session_max_bytes=session_max_bytes,
        session_max_files=session_max_files,
    )


if __name__ == "__main__":
    main()
