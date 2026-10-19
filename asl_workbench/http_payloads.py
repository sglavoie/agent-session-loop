"""Http payloads."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# ---------------------------------------------------------------------------
# Envelopes
# ---------------------------------------------------------------------------


def success_payload(command: str, **fields: Any) -> dict[str, Any]:
    """Build a successful command response, preserving command-specific keys.

    Identical to ``agent_session_loop.responses.success_payload`` by contract
    rather than by coincidence: ``tests/test_workbench_http.py`` pins the two
    against each other, so the portable half can answer a CLI-shaped envelope
    without importing the CLI.
    """
    payload: dict[str, Any] = {"ok": True, "command": command}
    payload.update(fields)
    return payload


def error_payload(
    command: str,
    *,
    reason: str,
    error: str,
    paused: bool = False,
    details: Mapping[str, Any] | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """Build a failed command response with the common scriptable contract."""
    payload: dict[str, Any] = {
        "ok": False,
        "command": command,
        "reason": reason,
        "error": error,
        "paused": paused,
        "details": {} if details is None else dict(details),
    }
    payload.update(fields)
    return payload
