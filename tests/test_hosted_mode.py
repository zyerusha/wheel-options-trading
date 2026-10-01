"""End-to-end hosted-mode tests.

Real HTTP round trips through a live ThreadingHTTPServer (the one thing this
suite needs a real server for -- everything else in the project tests objects
directly), using independent stdlib cookie jars to stand in for separate
browsers. Covers the multi-tenant security model hosted mode is built on:
per-session isolation, cookie lifecycle, TTL/quota enforcement, and forged- or
expired-cookie handling -- plus a smoke test that local/Docker-mount mode
never engages any of this.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wheel.serve import Handler, Workspace  # noqa: E402
from wheel.sessions import SESSION_COOKIE_NAME, SessionManager, is_valid_token  # noqa: E402

HISTORY_HEADER = (
    "Run Date,Action,Symbol,Description,Type,Quantity,Price ($),Commission ($),"
    "Fees ($),Accrued Interest ($),Amount ($),Cash Balance ($),Settlement Date"
)
TRADE_ROW = (
    '09/19/2025,"YOU SOLD OPENING TRANSACTION PUT (MU) ...",-MU250926P150,'
    '"PUT ...",Cash,-1,3.35,0,0,,335.00,10000.00,09/19/2025'
)
OTHER_TRADE_ROW = (
    '09/20/2025,"YOU SOLD OPENING TRANSACTION PUT (AAPL) ...",-AAPL250926P150,'
    '"PUT ...",Cash,-1,2.10,0,0,,210.00,9790.00,09/20/2025'
)


def _history_body(row: str = TRADE_ROW) -> bytes:
    return (HISTORY_HEADER + "\n" + row + "\n").encode("utf-8-sig")


def _opener_with_jar():
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar)), jar


def _token_from_jar(jar) -> str | None:
    for cookie in jar:
        if cookie.name == SESSION_COOKIE_NAME:
            return cookie.value
    return None


def _upload(opener, base_url, filename, body, headers=None):
    request = urllib.request.Request(base_url + "/api/upload", data=body, method="POST")
    request.add_header("X-Filename", filename)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    return opener.open(request)


def _get(opener, base_url, path):
    return opener.open(base_url + path)


def _get_json(opener, base_url, path) -> dict:
    with _get(opener, base_url, path) as response:
        return json.loads(response.read().decode("utf-8"))


class _RunningHostedServer:
    """A real hosted-mode ThreadingHTTPServer on an OS-assigned port, backed
    by ``session_manager``. Mutates the process-wide ``Handler`` class
    attributes for its lifetime -- restored on close()."""

    def __init__(self, session_manager: SessionManager):
        self.session_manager = session_manager
        self._prev_workspace = Handler.workspace
        self._prev_session_manager = Handler.session_manager
        self._prev_log_message = Handler.log_message
        Handler.workspace = None
        Handler.session_manager = session_manager
        Handler.log_message = lambda *a, **k: None  # keep test output quiet
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        Handler.workspace = self._prev_workspace
        Handler.session_manager = self._prev_session_manager
        Handler.log_message = self._prev_log_message


class HostedModeTestCase(unittest.TestCase):
    """Base class: a fresh sessions_root + SessionManager + running server per
    test, generous defaults unless a test overrides them for its own quota/TTL
    scenario."""

    ttl_seconds: float = 3600
    max_session_bytes: int = 250 * 1024 * 1024
    max_session_files: int = 100
    sweep_interval_seconds: float = 3600

    def setUp(self):
        self.sessions_root = tempfile.TemporaryDirectory()
        self.addCleanup(self.sessions_root.cleanup)
        self.manager = SessionManager(
            sessions_root=self.sessions_root.name,
            workspace_factory=lambda base_dir: Workspace.build([], base_dir=base_dir, extra_dirs=()),
            ttl_seconds=self.ttl_seconds,
            max_session_bytes=self.max_session_bytes,
            max_session_files=self.max_session_files,
            sweep_interval_seconds=self.sweep_interval_seconds,
        )
        self.server = _RunningHostedServer(self.manager)
        self.addCleanup(self.server.close)

    @property
    def base_url(self) -> str:
        return self.server.base_url


class TestSessionIsolation(HostedModeTestCase):
    def test_two_browsers_never_see_each_others_uploads(self):
        browser_a, jar_a = _opener_with_jar()
        browser_b, jar_b = _opener_with_jar()

        _upload(browser_a, self.base_url, "History_for_Account.csv", _history_body(TRADE_ROW))
        listing_a = _get_json(browser_a, self.base_url, "/api/datasets")
        self.assertEqual(listing_a["transactions"], 1)

        # Browser B has never uploaded anything -- must see an empty session,
        # never A's data, even though both hit the same running server.
        listing_b = _get_json(browser_b, self.base_url, "/api/datasets")
        self.assertEqual(listing_b["transactions"], 0)
        self.assertEqual(listing_b["datasets"], [])

        self.assertNotEqual(_token_from_jar(jar_a), _token_from_jar(jar_b))

    def test_a_new_session_never_reaches_a_closed_browsers_data(self):
        browser_a, _jar_a = _opener_with_jar()
        _upload(browser_a, self.base_url, "History_for_Account.csv", _history_body(TRADE_ROW))

        # "Browser A closes" == its cookie is simply never presented again.
        # A brand-new browser (no cookie at all) must start empty, not resume A.
        browser_c, _jar_c = _opener_with_jar()
        listing_c = _get_json(browser_c, self.base_url, "/api/datasets")
        self.assertEqual(listing_c["transactions"], 0)


class TestCookieLifecycle(HostedModeTestCase):
    def test_cookie_is_issued_and_reused_across_requests(self):
        browser, jar = _opener_with_jar()
        self.assertIsNone(_token_from_jar(jar))

        _get(browser, self.base_url, "/api/datasets")
        first_token = _token_from_jar(jar)
        self.assertTrue(is_valid_token(first_token))

        _upload(browser, self.base_url, "History_for_Account.csv", _history_body())
        self.assertEqual(_token_from_jar(jar), first_token)  # same session reused
        self.assertEqual(_get_json(browser, self.base_url, "/api/datasets")["transactions"], 1)

    def test_request_with_no_cookie_always_gets_a_fresh_session(self):
        browser_1, jar_1 = _opener_with_jar()
        browser_2, jar_2 = _opener_with_jar()
        _get(browser_1, self.base_url, "/api/datasets")
        _get(browser_2, self.base_url, "/api/datasets")
        self.assertNotEqual(_token_from_jar(jar_1), _token_from_jar(jar_2))


class TestForgedOrExpiredCookies(HostedModeTestCase):
    def test_forged_cookie_is_treated_as_no_session(self):
        for forged in ("../../etc/passwd", "' OR 1=1 --", "not-shaped-like-a-token!"):
            request = urllib.request.Request(self.base_url + "/api/datasets")
            request.add_header("Cookie", f"{SESSION_COOKIE_NAME}={forged}")
            with urllib.request.urlopen(request) as response:
                self.assertEqual(response.status, 200)
                new_token = None
                for header, value in response.getheaders():
                    if header.lower() == "set-cookie" and value.startswith(f"{SESSION_COOKIE_NAME}="):
                        new_token = value.split(";")[0].split("=", 1)[1]
                self.assertTrue(is_valid_token(new_token))
                self.assertNotEqual(new_token, forged)

    def test_expired_session_cookie_yields_a_new_empty_workspace(self):
        self.manager.ttl_seconds = 0.05
        browser, jar = _opener_with_jar()
        _upload(browser, self.base_url, "History_for_Account.csv", _history_body())
        old_token = _token_from_jar(jar)
        old_base_dir = self.manager._sessions[old_token].workspace.base_dir

        time.sleep(0.2)  # past the TTL

        listing = _get_json(browser, self.base_url, "/api/datasets")
        self.assertEqual(listing["transactions"], 0)  # a brand-new, empty session
        new_token = _token_from_jar(jar)
        self.assertNotEqual(new_token, old_token)
        self.assertFalse(os.path.isdir(old_base_dir))  # the old session's files are gone


class TestBackgroundSweep(HostedModeTestCase):
    ttl_seconds = 0.05
    sweep_interval_seconds = 0.05

    def test_idle_session_is_evicted_without_a_second_request(self):
        self.manager.start_background_sweep()
        browser, jar = _opener_with_jar()
        _upload(browser, self.base_url, "History_for_Account.csv", _history_body())
        token = _token_from_jar(jar)
        base_dir = self.manager._sessions[token].workspace.base_dir
        self.assertTrue(os.path.isdir(base_dir))

        time.sleep(0.5)  # several sweep intervals, no further request from anyone

        self.assertFalse(os.path.isdir(base_dir))
        self.assertNotIn(token, self.manager._sessions)


class TestSessionQuotas(HostedModeTestCase):
    max_session_bytes = 200  # smaller than one history export -- first upload must already fail

    def test_storage_quota_rejects_an_oversized_upload(self):
        browser, _jar = _opener_with_jar()
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            _upload(browser, self.base_url, "History_for_Account.csv", _history_body())
        self.assertEqual(ctx.exception.code, 400)


class TestSessionFileCountQuota(HostedModeTestCase):
    max_session_files = 1

    def test_file_count_quota_rejects_a_second_upload(self):
        browser, _jar = _opener_with_jar()
        _upload(browser, self.base_url, "History_for_Account.csv", _history_body(TRADE_ROW))
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            _upload(browser, self.base_url, "Another_Account.csv", _history_body(OTHER_TRADE_ROW))
        self.assertEqual(ctx.exception.code, 400)


class TestUploadSafetyInsideASession(HostedModeTestCase):
    def test_path_traversal_filename_is_confined_to_the_sessions_own_dir(self):
        browser, jar = _opener_with_jar()
        _upload(browser, self.base_url, "../../evil.csv", _history_body())
        token = _token_from_jar(jar)
        base_dir = self.manager._sessions[token].workspace.base_dir

        for root, _dirs, files in os.walk(base_dir):
            for name in files:
                self.assertTrue(os.path.commonpath([base_dir, os.path.join(root, name)]) == base_dir)
        self.assertFalse(os.path.isfile(os.path.join(self.sessions_root.name, "evil.csv")))

    def test_unrecognized_file_is_rejected_and_leaves_nothing_in_the_session_dir(self):
        browser, jar = _opener_with_jar()
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            _upload(browser, self.base_url, "junk.csv", b"not,a,real,export\n1,2,3,4\n")
        self.assertEqual(ctx.exception.code, 400)

        token = _token_from_jar(jar)
        base_dir = self.manager._sessions[token].workspace.base_dir
        self.assertEqual(os.listdir(base_dir), [])


class TestContainerRestartOrphansOldSessions(unittest.TestCase):
    """A container restart/recreation starts a brand-new process with an
    empty in-memory session map -- constructing a fresh SessionManager against
    the same on-disk sessions_root models exactly that. No token from before
    is valid against it, per the documented "hosted mode is disposable"
    design (see docker-compose.hosted.yml)."""

    def test_a_token_from_before_a_restart_is_never_honored(self):
        sessions_root = tempfile.TemporaryDirectory()
        self.addCleanup(sessions_root.cleanup)
        factory = lambda base_dir: Workspace.build([], base_dir=base_dir, extra_dirs=())

        old_manager = SessionManager(sessions_root=sessions_root.name, workspace_factory=factory)
        old_session = old_manager.get_or_create(None)
        old_token = old_session.token

        # A new process, same directory on disk -- nothing in memory survives.
        new_manager = SessionManager(sessions_root=sessions_root.name, workspace_factory=factory)
        new_session = new_manager.get_or_create(old_token)

        self.assertNotEqual(new_session.token, old_token)
        self.assertNotEqual(new_session.workspace.base_dir, old_session.workspace.base_dir)


class TestOptionBUnaffectedByHostedMode(unittest.TestCase):
    """Local/Docker-mount mode must never engage any session logic."""

    def test_resolve_workspace_bypasses_sessions_when_session_manager_is_none(self):
        with tempfile.TemporaryDirectory() as base_dir:
            workspace = Workspace.build([], base_dir=base_dir, extra_dirs=())

            class _NoSocketHandler(Handler):
                def __init__(self):  # skip BaseHTTPRequestHandler's socket-driven __init__
                    pass

            prev_workspace, prev_manager = Handler.workspace, Handler.session_manager
            try:
                Handler.workspace = workspace
                Handler.session_manager = None
                handler = _NoSocketHandler()
                resolved = handler._resolve_workspace()
                self.assertIs(resolved, workspace)
                self.assertIsNone(handler._session)
            finally:
                Handler.workspace, Handler.session_manager = prev_workspace, prev_manager


if __name__ == "__main__":
    unittest.main()
