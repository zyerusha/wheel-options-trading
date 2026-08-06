"""Zero-dependency dashboard server.

    python -m wheel.serve [--csv FILE] [--port 8765] [--no-browser]

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

The server binds to 127.0.0.1 only. Uploads are still treated as untrusted: the
filename is reduced to a bare basename, the body is size-capped, and a file that
doesn't look like a Fidelity transaction-history or Positions export is removed
again rather than left on disk.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wheel.accounts import DEFAULT_ACCOUNT_ID, AccountRegistry  # noqa: E402
from wheel.api import (  # noqa: E402
    Dashboard,
    Filters,
    discover_exports,
    discover_multi_account_exports,
    looks_like_export,
    looks_like_multi_account_export,
)
from wheel.positions import discover_position_snapshots, looks_like_position_snapshot  # noqa: E402

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(PACKAGE_DIR, "static")
PROJECT_ROOT = os.path.dirname(PACKAGE_DIR)
UPLOAD_DIR = os.path.join(PROJECT_ROOT, "data")

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


def _find_existing_duplicate(body: bytes) -> str | None:
    """A CSV already on disk under ``data/`` (any account subfolder) or the
    project root whose content is byte-identical to ``body``, if any.

    The browser's file picker has no notion of "this file is already on
    disk" -- it only ever hands the server bytes and a name -- so re-selecting
    a file that already lives in ``data/<account>/`` would otherwise get a
    second, identical copy written into the upload target every time Load is
    pressed. Comparing content rather than name/path catches that regardless
    of which folder the picker happened to browse into.
    """
    candidates: list[str] = []
    if os.path.isdir(UPLOAD_DIR):
        for root, _dirs, entries in os.walk(UPLOAD_DIR):
            candidates.extend(
                os.path.join(root, entry) for entry in entries if entry.lower().endswith(".csv")
            )
    if os.path.isdir(PROJECT_ROOT):
        candidates.extend(
            os.path.join(PROJECT_ROOT, entry)
            for entry in os.listdir(PROJECT_ROOT)
            if entry.lower().endswith(".csv") and os.path.isfile(os.path.join(PROJECT_ROOT, entry))
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


class DashboardState:
    """Holds the "default" account's active transaction-history dataset.

    Position snapshots are deliberately not part of this class's own state --
    ``Dashboard(csv_paths)`` is always called with ``position_paths=None``, so
    it auto-discovers every Positions export sitting in the project root/``data``
    on its own (see ``wheel.api.discover_position_snapshots``). That is what
    lets an uploaded or hand-edited Positions file take effect without needing
    its own activate/select step -- there is nothing to choose between, unlike
    transaction-history exports, which really can overlap and need combining.
    """

    def __init__(self, csv_path: str | list[str]):
        paths = [csv_path] if isinstance(csv_path, str) else list(csv_path)
        self.csv_paths = [os.path.abspath(path) for path in paths]
        self._lock = threading.Lock()
        self._stamp: tuple | None = None
        self._dashboard: Dashboard | None = None

    @property
    def csv_path(self) -> str | None:
        # None, not IndexError, when the default account is genuinely empty --
        # e.g. every account lives in its own data/<account>/ subfolder and
        # nothing is left loose in the project root or data/.
        return self.csv_paths[0] if self.csv_paths else None

    def _fingerprint(self) -> tuple:
        # Position snapshots are auto-discovered, not tracked in csv_paths, so
        # their own mtimes have to be watched here too -- otherwise editing or
        # replacing one on disk would never trigger a rebuild.
        return (
            tuple((path, os.path.getmtime(path)) for path in self.csv_paths),
            tuple(
                (path, os.path.getmtime(path))
                for path in discover_position_snapshots((PROJECT_ROOT, UPLOAD_DIR))
            ),
        )

    def get(self) -> Dashboard:
        with self._lock:
            stamp = self._fingerprint()
            if self._dashboard is None or stamp != self._stamp:
                self._dashboard = Dashboard(self.csv_paths)
                self._stamp = stamp
            return self._dashboard

    # ---- dataset management ----

    def search_paths(self) -> list[str]:
        """Every CSV the UI is allowed to switch to."""
        found: list[str] = []
        for directory in (PROJECT_ROOT, UPLOAD_DIR):
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
                    "folder": "data" if os.path.dirname(path) == UPLOAD_DIR else ".",
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
        for path in discover_multi_account_exports((PROJECT_ROOT, UPLOAD_DIR)):
            try:
                stat = os.stat(path)
            except OSError:
                continue
            rows.append(
                {
                    "name": os.path.basename(path),
                    "folder": "data" if os.path.dirname(path) == UPLOAD_DIR else ".",
                    "size_kb": round(stat.st_size / 1024, 1),
                    "reason": "multi-account transaction history export -- not supported yet",
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
        """
        try:
            dashboard = Dashboard(paths)
        except Exception as error:
            raise DatasetError(f"{type(error).__name__}: {error}") from error
        if not dashboard.transactions and not dashboard.snapshots:
            raise DatasetError("parsed successfully but contains no transactions or positions")
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
        if not files:
            raise DatasetError("no file content was sent")

        os.makedirs(UPLOAD_DIR, exist_ok=True)
        written: list[str] = []  # freshly created files -- rolled back on failure
        resolved: list[str] = []  # this upload's files, written or reused from an existing duplicate
        try:
            for filename, body in files:
                if not body:
                    raise DatasetError(f"{filename or 'upload'} was empty")
                duplicate = _find_existing_duplicate(body)
                if duplicate:
                    resolved.append(duplicate)
                    continue
                name = safe_filename(filename)
                target = os.path.join(UPLOAD_DIR, name)
                if os.path.exists(target):
                    stem, extension = os.path.splitext(name)
                    target = os.path.join(
                        UPLOAD_DIR, f"{stem}-{datetime.now():%Y%m%d-%H%M%S%f}{extension}"
                    )
                with open(target, "wb") as handle:
                    handle.write(body)
                written.append(target)
                resolved.append(target)

            multi_account = [path for path in resolved if looks_like_multi_account_export(path)]
            if multi_account:
                names = ", ".join(os.path.basename(path) for path in multi_account)
                raise DatasetError(
                    f"{names}: this looks like Fidelity's multi-account transaction history export "
                    "(separate 'Account'/'Account Number' columns) -- not supported yet. "
                    "Download a per-account History_for_Account_*.csv export instead."
                )

            unrecognized = [
                path for path in resolved if not looks_like_export(path) and not looks_like_position_snapshot(path)
            ]
            if unrecognized:
                names = ", ".join(os.path.basename(path) for path in unrecognized)
                raise DatasetError(f"not a recognized Fidelity export: {names}")

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
            for path in written:
                try:
                    os.remove(path)
                except OSError:
                    pass
            raise

        self._activate(targets, dashboard)
        return dashboard


class Handler(BaseHTTPRequestHandler):
    state: DashboardState = None  # injected by serve() -- owns the "default" account's active files
    registry: AccountRegistry = None  # injected by serve() -- every account, "default" included
    server_version = "WheelDashboard/1.0"

    # ---- plumbing ----

    def _sync_registry(self) -> None:
        """Keep the multi-account registry's "default" account pointed at
        whatever ``DashboardState`` currently has active, so an upload or a
        dataset switch is reflected in ``/api/dashboard``/``/api/accounts``
        without a second, redundant file scan.

        A ``ValueError`` here means the default account (loose files in the
        project root/``data``) is genuinely empty -- a user who keeps every
        account in its own ``data/<account>/`` subfolder, say. That's fine;
        the registry's own discovery already omits "default" from the account
        list in that case, so there's simply nothing to sync.
        """
        try:
            dashboard = self.state.get()
        except ValueError:
            return
        self.registry.set_default_dashboard(dashboard)

    def log_message(self, fmt: str, *args) -> None:
        # One tidy line per request instead of BaseHTTPRequestHandler's noise.
        sys.stderr.write(f"  {self.command} {self.path} -> {args[1] if len(args) > 1 else ''}\n")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
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

    # ---- routes ----

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        parsed = urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"

        try:
            if route == "/":
                self._send_file("index.html", "text/html; charset=utf-8")
            elif route == "/app.js":
                self._send_file("app.js", "application/javascript; charset=utf-8")
            elif route == "/styles.css":
                self._send_file("styles.css", "text/css; charset=utf-8")
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
                payload = self.registry.build(_preferred_account(self.registry))
                self._send_json(
                    {
                        "status": "ok",
                        "source": payload["meta"]["source"],
                        "transactions": payload["meta"]["transactions_total"],
                        "cycles": len(payload["cycles"]),
                        "reconciliation": payload["reconciliation"],
                    }
                )
            elif route == "/api/datasets":
                self._send_json(self._dataset_listing())
            elif route == "/api/accounts":
                self._sync_registry()
                self._send_json(
                    {
                        "accounts": self.registry.list_accounts(),
                        "default_account": self.registry.default_account_id,
                        "default_range": self.registry.default_range,
                    }
                )
            else:
                self._send_json({"error": "not found", "path": route}, 404)
        except Exception as error:  # pragma: no cover - surfaced to the browser
            import traceback

            traceback.print_exc()
            self._send_json({"error": str(error), "type": type(error).__name__}, 500)

    def do_POST(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        route = urlparse(self.path).path.rstrip("/") or "/"
        try:
            if route == "/api/upload":
                self._handle_upload()
            elif route == "/api/select":
                self._handle_select()
            else:
                self._send_json({"error": "not found", "path": route}, 404)
        except DatasetError as error:
            # A rejected dataset is a user-fixable problem, not a server fault.
            self._send_json({"error": str(error), "active": self.state.csv_path}, 400)
        except Exception as error:  # pragma: no cover - surfaced to the browser
            import traceback

            traceback.print_exc()
            self._send_json({"error": str(error), "type": type(error).__name__}, 500)

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
            "upload_dir": UPLOAD_DIR,
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

        # No account, or explicitly "default"/"combined": today's behavior --
        # goes through DashboardState, which owns the default account's active
        # transaction-history selection. A real named account instead writes
        # straight into its own folder under data/ and never touches
        # DashboardState at all, since a named account has no "active
        # selection" concept -- every file found in its folder is always
        # included (see AccountRegistry).
        if not account_id or account_id.lower() in {DEFAULT_ACCOUNT_ID, "combined"}:
            keep = (self.headers.get("X-Keep-Current") or "").lower() in {"1", "true", "yes"}
            self.state.accept_uploads(self._read_uploads(), keep_current=keep)
            self._send_json(self._dataset_listing(self._loaded_message("Loaded")))
            return

        message = self._accept_account_upload(account_id, self._read_uploads())
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
        if not files:
            raise DatasetError("no file content was sent")

        target_dir = os.path.join(UPLOAD_DIR, account_id)
        os.makedirs(target_dir, exist_ok=True)

        written: list[str] = []  # freshly created files -- rolled back on failure
        resolved: list[str] = []  # this upload's files, written or reused from an existing duplicate
        try:
            for filename, body in files:
                if not body:
                    raise DatasetError(f"{filename or 'upload'} was empty")
                duplicate = _find_existing_duplicate(body)
                if duplicate:
                    resolved.append(duplicate)
                    continue
                name = safe_filename(filename)
                target = os.path.join(target_dir, name)
                if os.path.exists(target):
                    stem, extension = os.path.splitext(name)
                    target = os.path.join(
                        target_dir, f"{stem}-{datetime.now():%Y%m%d-%H%M%S%f}{extension}"
                    )
                with open(target, "wb") as handle:
                    handle.write(body)
                written.append(target)
                resolved.append(target)

            multi_account = [path for path in resolved if looks_like_multi_account_export(path)]
            if multi_account:
                names = ", ".join(os.path.basename(path) for path in multi_account)
                raise DatasetError(
                    f"{names}: this looks like Fidelity's multi-account transaction history export "
                    "(separate 'Account'/'Account Number' columns) -- not supported yet. "
                    "Download a per-account History_for_Account_*.csv export instead."
                )

            unrecognized = [
                path for path in resolved if not looks_like_export(path) and not looks_like_position_snapshot(path)
            ]
            if unrecognized:
                names = ", ".join(os.path.basename(path) for path in unrecognized)
                raise DatasetError(f"not a recognized Fidelity export: {names}")
        except DatasetError:
            for path in written:
                try:
                    os.remove(path)
                except OSError:
                    pass
            raise

        self.registry.refresh(force=True)
        history_count = sum(1 for path in resolved if looks_like_export(path))
        position_count = sum(1 for path in resolved if looks_like_position_snapshot(path))
        parts = [f"Loaded into '{account_id}'"]
        if history_count:
            parts.append(f"{history_count} transaction export(s)")
        if position_count:
            parts.append(f"{position_count} position snapshot(s)")
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


def _preferred_account(registry: AccountRegistry) -> str:
    """The account to show by default: "default" if it has anything, else
    "combined" -- which still works when every account lives in its own named
    subfolder and the project root/``data`` has nothing loose in it.
    """
    ids = {row["id"] for row in registry.list_accounts()}
    return DEFAULT_ACCOUNT_ID if DEFAULT_ACCOUNT_ID in ids else "combined"


def serve(
    csv_path: str | list[str] | None = None, port: int = 8765, open_browser: bool = True
) -> None:
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
    state = DashboardState(paths)
    registry = AccountRegistry(base_dir=UPLOAD_DIR, extra_dirs=(PROJECT_ROOT,))
    if paths:
        registry.set_default_dashboard(state.get())  # fail fast on a bad file, before binding the port

    accounts = registry.list_accounts()
    if not accounts:
        raise SystemExit(
            "No broker export or Portfolio Positions file found in this folder, data/, "
            "or any data/<account>/ subfolder. Put a CSV there, or pass one with --csv."
        )

    payload = registry.build(_preferred_account(registry))

    Handler.state = state
    Handler.registry = registry
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"

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

    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
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
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    args = parser.parse_args()
    serve(args.csv, args.port, open_browser=not args.no_browser)


if __name__ == "__main__":
    main()
