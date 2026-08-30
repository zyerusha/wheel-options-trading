"""Tiny, dependency-free file-reading helpers shared by the CSV parsers.

Kept in their own module (no other ``wheel.*`` imports) purely to avoid
duplicating the same "open utf-8-sig, tolerate bad bytes" idiom across
:mod:`wheel.api` (broker export discovery) and :mod:`wheel.positions`
(Positions snapshot discovery) -- both peek at a file's header the same way,
just to decide what kind of export it is, before any real parsing happens.
"""

from __future__ import annotations


def peek_text(path: str, max_bytes: int = 4096) -> str | None:
    """The first ``max_bytes`` of ``path``, decoded as ``utf-8-sig`` with bad
    bytes replaced rather than raising -- or ``None`` if the file can't be
    opened at all (missing, a permissions error, ...).

    A cheap "does this look like format X" header peek never needs the whole
    file, and never needs to fail loudly on a read error -- the caller's own
    answer is simply "no, it doesn't look like X."
    """
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as handle:
            return handle.read(max_bytes)
    except OSError:
        return None


def find_line(lines: list[str], matches) -> int | None:
    """Index of the first line in ``lines`` for which ``matches(line)`` is
    true, or ``None`` if no line matches.

    The shared shape behind "find this CSV's header row" -- used with a
    different ``matches`` predicate (and a different header key) by
    :func:`wheel.parser._read_rows` and
    :func:`wheel.positions.parse_position_snapshot`, both of which read a
    broker export with an unpredictable amount of preamble/boilerplate before
    the real header row.
    """
    return next((i for i, line in enumerate(lines) if matches(line)), None)
