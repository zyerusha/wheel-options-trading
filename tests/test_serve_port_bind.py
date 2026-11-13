"""Regression tests for the dashboard server's port binding.

On Windows SO_REUSEADDR lets a second server listen on a port that is already
taken, so two dashboards could run on one port and requests reached either.
A second server must fail with a clear message instead, while a quick
restart on the same port keeps working.
"""

from __future__ import annotations

import os
import sys
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wheel.serve import DashboardHTTPServer, _bind_dashboard_server  # noqa: E402


class _OkHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


class PortBindTest(unittest.TestCase):
    def _start(self, host: str = "127.0.0.1", port: int = 0) -> DashboardHTTPServer:
        server = DashboardHTTPServer((host, port), _OkHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            server.server_close()
            thread.join(5)

        self.addCleanup(stop)
        return server

    def test_second_server_on_busy_port_is_refused(self):
        port = self._start().server_address[1]
        with self.assertRaises(OSError):
            DashboardHTTPServer(("127.0.0.1", port), _OkHandler).server_close()

    @unittest.skipUnless(sys.platform == "win32", "Windows-only bind rules")
    def test_wildcard_server_cannot_share_a_busy_loopback_port(self):
        port = self._start().server_address[1]
        with self.assertRaises(OSError):
            DashboardHTTPServer(("0.0.0.0", port), _OkHandler).server_close()

    def test_busy_port_exits_with_clear_message(self):
        port = self._start().server_address[1]
        with self.assertRaises(SystemExit) as raised:
            _bind_dashboard_server("127.0.0.1", port, _OkHandler)
        self.assertIn(f"Port {port} is already in use", str(raised.exception))
        self.assertIn("--port", str(raised.exception))

    def test_quick_restart_on_same_port_still_works(self):
        first = DashboardHTTPServer(("127.0.0.1", 0), _OkHandler)
        port = first.server_address[1]
        thread = threading.Thread(target=first.serve_forever, daemon=True)
        thread.start()
        # Server-closed connections leave the port's sockets in TIME_WAIT.
        for _ in range(3):
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as response:
                self.assertEqual(response.read(), b"ok")
        first.shutdown()
        first.server_close()
        thread.join(5)

        restarted = self._start(port=port)
        self.assertEqual(restarted.server_address[1], port)


if __name__ == "__main__":
    unittest.main()
