"""Keep remote-ref observations continuous across worker turn boundaries."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import clock, repository_evidence_transitions
from .agent_mux_delivery import StatePersistence


def record_boundary(
    snapshot: Mapping[str, Any],
    *,
    payload: dict[str, Any],
    persistence: StatePersistence,
    event_path: Path,
    phase: str,
    session_id: str,
    turn_index: int,
) -> None:
    """Record snapshots already captured by the guard; never infer a writer."""
    repositories = {
        str(key): {"head": row.get("head"), "refs": row["refs"]}
        for key, row in snapshot.items()
        if isinstance(row, Mapping)
        and isinstance(row.get("refs"), (list, tuple))
        and not row.get("unavailable")
    }
    with persistence.lock:
        previous = payload.get("last_ref_observation")
        if (
            phase == "turn-start"
            and isinstance(previous, Mapping)
            and previous.get("run_id") == payload.get("run_id")
            and previous.get("phase") == "turn-end"
        ):
            old = previous.get("repositories")
            if isinstance(old, Mapping):
                drift = {
                    key: changes
                    for key, current in repositories.items()
                    if isinstance(old.get(key), Mapping)
                    and (
                        changes := repository_evidence_transitions.remote_ref_drift(
                            old[key], current
                        )
                    )
                }
                if drift:
                    persistence.append_event(
                        event_path,
                        "inter_turn_ref_drift",
                        {
                            "run_id": payload.get("run_id"),
                            "session_id": session_id,
                            "turn_index": turn_index,
                            "previous_session_id": previous.get("session_id"),
                            "previous_turn_index": previous.get("turn_index"),
                            "writer": "unknown",
                            "repositories": drift,
                            "window": "between-worker-turns",
                        },
                    )
        payload["last_ref_observation"] = {
            "run_id": payload.get("run_id"),
            "phase": phase,
            "session_id": session_id,
            "turn_index": turn_index,
            "observed_at": clock.isoformat(clock.utc_now()),
            "repositories": repositories,
        }
        persistence.write_state(payload)
