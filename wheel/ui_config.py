"""``data/config.json`` -- the dashboard's own remembered UI state.

Unlike ``data/accounts.json`` (a hand-written config the user edits to shape
account discovery), this file is machine-written: the frontend POSTs its
current settings (theme, selected account/date-range, chip filters, the
active tab, every toggle and number box the user can set) to ``/api/config``
on every change, and reads it back once on page load so a browser refresh
picks up exactly where the reader left off. It's created automatically the
first time anything is saved -- there is nothing to set up, and nothing here
is required for the dashboard to work; a missing or unreadable file just
means "no saved settings yet," the same as a fresh checkout.

The shape is intentionally opaque to the backend: this module does no
validation of what's inside, because the only writer is the app's own
frontend, which already knows what a valid value looks like for each field it
sets. That also means the file is safe to hand-edit or delete if you want to
reset to defaults -- worst case a bad edit is ignored (see :func:`read_config`)
and the next save overwrites it with good data again.
"""

from __future__ import annotations

import json
import os

from wheel.paths import DATA_DIR

CONFIG_FILENAME = "config.json"


def config_path(base_dir: str = DATA_DIR) -> str:
    return os.path.join(base_dir, CONFIG_FILENAME)


def read_config(base_dir: str = DATA_DIR) -> dict:
    """Whatever was last saved, or ``{}`` if there's nothing yet (or it's
    unreadable) -- a corrupt or hand-edited-wrong config file should never
    take the dashboard down, just fall back to defaults."""
    path = config_path(base_dir)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_config(data: dict, base_dir: str = DATA_DIR) -> None:
    """Replaces the whole file with `data` -- the frontend always sends its
    complete current settings, not a partial patch, so there's no merge to
    do here. Written to a temp file and renamed into place so a save that's
    interrupted mid-write (a killed process, a full disk) never leaves a
    half-written, unparsable config.json behind.
    """
    os.makedirs(base_dir, exist_ok=True)
    path = config_path(base_dir)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
    os.replace(tmp_path, path)
