"""Where the app looks for broker exports, config JSON, and market-data caches.

There is exactly one data directory: ``DATA_DIR``, an absolute path.

* unset ``WHEEL_DATA_DIR``  -> ``<repo>/data`` (unchanged legacy default)
* set   ``WHEEL_DATA_DIR``  -> that path, expanded and made absolute

``DISCOVERY_DIRS`` additionally keeps ``"."`` (the current working directory)
as a discovery location, purely for backward compatibility with the documented
habit of ``cd``-ing into a folder of exports and running ``python -m wheel.serve``
there. Every *write* (uploads, caches) goes to ``DATA_DIR`` only.
"""

from __future__ import annotations

import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _resolve_data_dir() -> str:
    override = os.environ.get("WHEEL_DATA_DIR", "").strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(PROJECT_ROOT, "data")


DATA_DIR = _resolve_data_dir()  # always absolute

# Created eagerly, at import time: a fresh Docker bind mount or a first-ever
# run of the app has nothing here yet, and both the upload endpoint and a
# plain directory listing need it to already exist rather than erroring or
# silently finding nothing. exist_ok -- every other run just confirms it's
# already there.
os.makedirs(DATA_DIR, exist_ok=True)


def _discovery_dirs() -> tuple[str, ...]:
    # "." first (back-compat with running from a folder of exports), then the
    # real data dir. Drop "." if it already resolves to DATA_DIR so the same
    # folder isn't scanned twice (harmless -- results are merged/de-duped by
    # transaction content downstream -- but noisy).
    dirs: list[str] = []
    seen: set[str] = set()
    for d in (".", DATA_DIR):
        rp = os.path.realpath(d)
        if rp not in seen:
            seen.add(rp)
            dirs.append(d)
    return tuple(dirs)


DISCOVERY_DIRS: tuple[str, ...] = _discovery_dirs()
