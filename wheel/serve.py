"""Zero-dependency dashboard server.

    python -m wheel.serve [--csv FILE] [--port 8765] [--no-browser]

Routes
------
``GET  /``                 the dashboard page
``GET  /api/dashboard``    filtered JSON  (?tickers=MU,QQQ&start=&end=&status=)
``GET  /api/health``       liveness plus the cash reconciliation verdict
``GET  /api/datasets``     exports available to load, and which one is active
``POST /api/upload``       accept a new Fidelity CSV and make it active
``POST /api/select``       switch to an export already on disk

The active CSV is parsed once and re-parsed only when its mtime changes, so
editing the export and refreshing the page is enough to pick it up.

The server binds to 127.0.0.1 only. Uploads are still treated as untrusted: the
filename is reduced to a bare basename, the body is size-capped, and the file has
to parse as a Fidelity export before it is allowed to become the active dataset.
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

from wheel.api import Dashboard, Filters, discover_exports, looks_like_export  # noqa: E402

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


class DashboardState:
    """Holds the active dataset -- one or more exports combined."""

    def __init__(self, csv_path: str | list[str]):
        paths = [csv_path] if isinstance(csv_path, str) else list(csv_path)
        self.csv_paths = [os.path.abspath(path) for path in paths]
        self._lock = threading.Lock()
        self._stamp: tuple | None = None
        self._dashboard: Dashboard | None = None

    @property
    def csv_path(self) -> str:
        return self.csv_paths[0]

    def _fingerprint(self) -> tuple:
        return tuple((path, os.path.getmtime(path)) for path in self.csv_paths)

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

    @staticmethod
    def _validate(paths: list[str]) -> Dashboard:
        """Parse candidate files together, rejecting anything unusable."""
        try:
            dashboard = Dashboard(paths)
        except Exception as error:
            raise DatasetError(f"{type(error).__name__}: {error}") from error
        if not dashboard.transactions:
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
        """
        if not files:
            raise DatasetError("no file content was sent")

        os.makedirs(UPLOAD_DIR, exist_ok=True)
        written: list[str] = []
        try:
            for filename, body in files:
                if not body:
                    raise DatasetError(f"{filename or 'upload'} was empty")
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

            targets = (self.csv_paths + written) if keep_current else written
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
    state: DashboardState = None  # injected by serve()
    server_version = "WheelDashboard/1.0"

    # ---- plumbing ----

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
                filters = Filters.from_query(parse_qs(parsed.query))
                self._send_json(self.state.get().build(filters))
            elif route == "/api/health":
                payload = self.state.get().build()
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
        dashboard = self.state.get()
        merge = dashboard.merge
        payload = {
            "active": list(self.state.csv_paths),
            "active_name": " + ".join(os.path.basename(p) for p in self.state.csv_paths),
            "datasets": self.state.datasets(),
            "transactions": len(dashboard.transactions),
            "rows_parsed": merge.rows_parsed,
            "duplicates_removed": merge.duplicates_removed,
            "first_date": merge.first_date.isoformat() if merge.first_date else None,
            "last_date": merge.last_date.isoformat() if merge.last_date else None,
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
        keep = (self.headers.get("X-Keep-Current") or "").lower() in {"1", "true", "yes"}
        self.state.accept_uploads(self._read_uploads(), keep_current=keep)
        self._send_json(self._dataset_listing(self._loaded_message("Loaded")))

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


def serve(
    csv_path: str | list[str] | None = None, port: int = 8765, open_browser: bool = True
) -> None:
    if csv_path is None:
        csv_path = discover_exports()
    paths = [csv_path] if isinstance(csv_path, str) else list(csv_path)
    if not paths:
        raise SystemExit(
            "No broker export found in . or data/. Put your CSV in this folder, "
            "or pass one with --csv."
        )
    for path in paths:
        if not os.path.isfile(path):
            raise SystemExit(f"CSV not found: {path}")

    state = DashboardState(paths)
    dashboard = state.get()  # fail fast on a bad file, before binding the port
    payload = dashboard.build()

    Handler.state = state
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"

    reconciliation = payload["reconciliation"]
    merge = dashboard.merge
    print(f"  source        {', '.join(os.path.basename(p) for p in paths)}")
    if merge.combined:
        print(
            f"  combined      {merge.rows_parsed} rows -> {merge.rows_kept} "
            f"({merge.duplicates_removed} duplicates merged)"
        )
    print(f"  transactions  {payload['meta']['transactions_total']}")
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
