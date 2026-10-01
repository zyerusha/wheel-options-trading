"""Per-browser session identity, quotas, and idle eviction for hosted mode.

Local/Docker-mount mode (the default) never touches this module -- one process
serves one shared ``Workspace`` for a single trusted user, exactly as before.
Hosted mode (``WHEEL_MODE=hosted`` / ``--hosted``) uses a :class:`SessionManager`
to give each browser its own isolated, disk-backed workspace, keyed by an
anonymous cookie -- no login, no accounts.

Kept free of any import from ``wheel.serve`` (which builds the actual
``Workspace`` objects this module stores) to avoid a circular import; callers
hand in a ``workspace_factory(base_dir) -> Any`` closure instead.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

SESSION_COOKIE_NAME = "wheel_session"

DEFAULT_SESSION_TTL_SECONDS = 2 * 3600
DEFAULT_SESSION_MAX_BYTES = 250 * 1024 * 1024
DEFAULT_SESSION_MAX_FILES = 100
_SWEEP_INTERVAL_SECONDS = 5 * 60

# Shaped like secrets.token_urlsafe()'s output -- anything else (a forged or
# garbage cookie value) is rejected before it is ever used as a dict lookup
# key, let alone a path component.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


def is_valid_token(token: str | None) -> bool:
    return bool(token) and _TOKEN_RE.match(token) is not None


def new_token() -> str:
    return secrets.token_urlsafe(32)


class SessionQuotaError(ValueError):
    """An upload that would push a session over its storage/file-count quota."""


@dataclass
class Session:
    token: str
    workspace: object
    last_seen: float
    bytes_used: int = 0
    file_count: int = 0

    def check_quota(self, added_bytes: int, added_files: int, *, max_bytes: int, max_files: int) -> None:
        if self.bytes_used + added_bytes > max_bytes:
            raise SessionQuotaError(
                f"this session's storage limit ({max_bytes // (1024 * 1024)} MB) would be exceeded"
            )
        if self.file_count + added_files > max_files:
            raise SessionQuotaError(f"this session's file limit ({max_files} files) would be exceeded")

    def record_usage(self, added_bytes: int, added_files: int) -> None:
        self.bytes_used += added_bytes
        self.file_count += added_files


@dataclass
class SessionManager:
    """Maps an opaque, server-issued token to an isolated per-session workspace.

    Every session's own directory lives at ``sessions_root/<token>/`` and is
    deleted in full -- via :func:`shutil.rmtree` -- the moment the session is
    considered expired, whether that eviction happens lazily (the next call to
    :meth:`get_or_create`, from any session) or from the background sweep
    thread started by :meth:`start_background_sweep`. A token the manager does
    not recognize (expired, forged, or simply never issued) is never an error
    and never reaches another session's data -- it just mints a fresh, empty
    one, which is what makes an invalid cookie harmless rather than a way to
    guess your way into someone else's workspace.
    """

    sessions_root: str
    workspace_factory: Callable[[str], object]
    ttl_seconds: float = DEFAULT_SESSION_TTL_SECONDS
    max_session_bytes: int = DEFAULT_SESSION_MAX_BYTES
    max_session_files: int = DEFAULT_SESSION_MAX_FILES
    sweep_interval_seconds: float = _SWEEP_INTERVAL_SECONDS
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _sessions: dict[str, Session] = field(default_factory=dict, init=False, repr=False)
    _stop_sweep: threading.Event | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        os.makedirs(self.sessions_root, exist_ok=True)

    def get_or_create(self, token: str | None) -> Session:
        now = time.time()
        with self._lock:
            self._evict_expired_locked(now)
            session = self._sessions.get(token) if is_valid_token(token) else None
            if session is not None:
                session.last_seen = now
                return session
            return self._create_locked(now)

    def _create_locked(self, now: float) -> Session:
        token = new_token()
        base_dir = os.path.join(self.sessions_root, token)
        os.makedirs(base_dir, exist_ok=True)
        session = Session(token=token, workspace=self.workspace_factory(base_dir), last_seen=now)
        self._sessions[token] = session
        return session

    def _evict_expired_locked(self, now: float) -> None:
        expired = [token for token, session in self._sessions.items() if now - session.last_seen > self.ttl_seconds]
        for token in expired:
            session = self._sessions.pop(token)
            shutil.rmtree(session.workspace.base_dir, ignore_errors=True)

    def sweep(self) -> None:
        with self._lock:
            self._evict_expired_locked(time.time())

    def start_background_sweep(self) -> threading.Event:
        """Evict idle sessions even when nobody ever visits again to trigger
        the lazy sweep in :meth:`get_or_create`. Runs as a daemon thread so it
        never blocks process shutdown; returns the ``Event`` that stops it."""
        stop = threading.Event()

        def _loop() -> None:
            while not stop.wait(self.sweep_interval_seconds):
                self.sweep()

        threading.Thread(target=_loop, daemon=True, name="wheel-session-sweep").start()
        self._stop_sweep = stop
        return stop
