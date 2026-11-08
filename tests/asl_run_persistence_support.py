"""Shared fixtures for test_asl_run_persistence."""

from __future__ import annotations

from typing import Any


def _identified_payload(**overrides: Any) -> dict[str, Any]:
    """A payload carrying the identity the projection contract requires."""
    payload: dict[str, Any] = {
        "run_id": "run-1",
        "plan_dir": "/tmp/plan",
        "run_started_at": "2026-01-01T00:00:00+00:00",
    }
    payload.update(overrides)
    return payload
