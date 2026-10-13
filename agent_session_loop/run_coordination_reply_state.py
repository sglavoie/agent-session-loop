"""Rollback of paused Q&A state when a reply attempt fails before consumption."""

from __future__ import annotations

import contextlib
from pathlib import Path

from . import (
    durability,
)


def read_optional_bytes(path: Path) -> bytes | None:
    """Return ``path``'s bytes, or ``None`` when it does not exist."""
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def restore_optional_bytes(path: Path, payload: bytes | None) -> None:
    """Put ``path`` back to ``payload``, removing it when that was absence."""
    if payload is None:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        return
    durability.write_bytes_file(path, payload)


class ReplyStatePreserver:
    """Restore paused Q&A state when a reply attempt fails before consumption.

    A reply is delivered by replaying the paused run, which rewrites both
    ``state.json`` and ``events.jsonl`` before the harness has accepted
    anything. If the attempt dies short of consumption those two files describe
    a continuation that never happened, so the pre-attempt bytes are captured up
    front and put back unless :meth:`mark_consumed` says the reply really landed.
    """

    def __init__(
        self,
        *,
        state_path: Path,
        event_path: Path,
        enabled: bool,
    ) -> None:
        self._enabled = enabled
        self._consumed = False
        self._state_path = state_path
        self._event_path = event_path
        self._state_bytes = read_optional_bytes(state_path) if enabled else None
        self._event_bytes = read_optional_bytes(event_path) if enabled else None

    @property
    def consumed(self) -> bool:
        """Whether the reply reached the harness and must not be rolled back."""
        return self._consumed

    def mark_consumed(self) -> None:
        self._consumed = True

    def restore_if_unconsumed(self) -> None:
        if not self._enabled or self._consumed:
            return
        restore_optional_bytes(self._state_path, self._state_bytes)
        restore_optional_bytes(self._event_path, self._event_bytes)
