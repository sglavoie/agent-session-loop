"""What a command says about its own failure, for the CLI trace to record.

A leaf module with no imports from this package, so any handler or response
helper can note a reason without creating an import cycle through
``cli_trace``. A noted reason is code vocabulary only: a short kebab-case slug
such as ``plan-not-found`` or ``verify-failed``. Anything else (a message, a
path, free text) is dropped, which keeps the trace as content-free as the
rest of its fields.
"""

from __future__ import annotations

import re

_REASON_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")

_reason: str | None = None
_paused: bool = False


def note_reason(reason: object, *, paused: bool = False) -> None:
    """Remember why this process is about to exit nonzero; the last note wins.

    ``paused`` marks a run that stopped at a pause rather than failing, so the
    trace can tell the two apart while the exit status stays the same.
    """
    global _reason, _paused
    if isinstance(reason, str) and _REASON_PATTERN.fullmatch(reason):
        _reason = reason
        _paused = bool(paused)


def noted_reason() -> str | None:
    return _reason


def noted_paused() -> bool:
    return _paused


def reset() -> None:
    """Forget any note; one process may run several commands under test."""
    global _reason, _paused
    _reason = None
    _paused = False
