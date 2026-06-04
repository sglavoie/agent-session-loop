"""Shared command JSON response helpers."""

from __future__ import annotations

import json
from typing import Any


def success_payload(command: str, **fields: Any) -> dict[str, Any]:
    """Build a successful command response while preserving command-specific keys."""
    payload: dict[str, Any] = {
        "ok": True,
        "command": command,
    }
    payload.update(fields)
    return payload


def error_payload(
    command: str,
    *,
    reason: str,
    error: str,
    paused: bool = False,
    details: dict[str, Any] | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """Build a failed command response with the common scriptable contract."""
    payload: dict[str, Any] = {
        "ok": False,
        "command": command,
        "reason": reason,
        "error": error,
        "paused": paused,
        "details": {} if details is None else details,
    }
    payload.update(fields)
    return payload


def print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True))
