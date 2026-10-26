"""The one reading of ASL's ISO-8601 timestamps this package does.

ASL writes its timestamps in UTC, and some of them carry no offset. A naive
value is therefore UTC, never the reader's local time: the Mac and the Pi do
not share a timezone, and reading the same record as two different moments on
the two hosts is how one plan sorts, settles and expires differently depending
on which machine is looking at it. Every module that turns one of those strings
into a moment comes through here, so that reading cannot drift per caller.

Portable: standard library only, no import from :mod:`agent_session_loop`.
"""

from __future__ import annotations

from datetime import UTC, datetime


def parse_iso(value: object) -> datetime | None:
    """Read an ISO-8601 timestamp as an aware moment, or ``None``.

    A naive value is read as UTC. ``None`` rather than an exception, because
    every caller has its own honest answer for a moment it cannot place, and
    none of them is a crash.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment


def iso_epoch(value: object) -> float | None:
    """:func:`parse_iso` as a Unix epoch, or ``None`` when it cannot be one."""
    moment = parse_iso(value)
    if moment is None:
        return None
    try:
        return moment.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


__all__ = ["iso_epoch", "parse_iso"]
