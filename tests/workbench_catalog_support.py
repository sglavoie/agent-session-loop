"""Workbench catalog support."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from asl_workbench import catalog
from tests.support import _WORKBENCH_DEAD_PID

REPO_ROOT = Path(__file__).resolve().parent.parent


#: A pid this host can prove is not running, which is what makes a lock
#: reclaimable and therefore makes "the read did not reclaim it" a real claim.
DEAD_PID = _WORKBENCH_DEAD_PID


def _paused_state_payload(
    plan_dir: Path,
    *,
    run_id: str = "20260619T140000Z-1",
) -> dict[str, Any]:
    """A runner state that classifies as ``paused-for-reply``.

    The state payload names its own plan directory because that is what makes
    the plan discoverable from the state root at all: ASL's state source reads
    the recorded ``plan_dir`` rather than reversing the digest its state
    directory is named after.
    """
    return {
        "plan_dir": str(plan_dir),
        "ok": False,
        "paused": True,
        "reason": "agent-question",
        "pause_reason": "agent-question",
        "pause_message": "Awaiting confirmation on the v1 column",
        "run_id": run_id,
        "harness": "claude",
        "run_started_at": "2026-06-19T14:00:00+00:00",
        "active_turn": None,
        "active_verification": None,
    }


def _tree_fingerprint(root: Path) -> dict[str, tuple[int, bytes]]:
    """Every file under ``root`` by relative path, with its size and bytes.

    Content rather than mtime: a read that rewrote a file with identical bytes
    would still be a write, but a read that left the bytes alone and touched an
    mtime is not the failure this program cares about, and mtime granularity
    makes that check flaky on a fast machine.
    """
    fingerprint: dict[str, tuple[int, bytes]] = {}
    if not root.is_dir():
        return fingerprint
    for path in sorted(root.rglob("*")):
        if path.is_file():
            payload = path.read_bytes()
            fingerprint[str(path.relative_to(root))] = (len(payload), payload)
    return fingerprint


def _minimal_summary_payload(plan_dir: str, **overrides: Any) -> dict[str, Any]:
    """A summary payload carrying every classified field and nothing else."""
    payload: dict[str, Any] = {name: None for name in catalog.PRIVATE_FIELDS}
    payload.update({name: None for name in catalog.CACHE_SAFE_FIELDS})
    payload.update(
        {
            "plan_dir": plan_dir,
            "exists": True,
            "primary_state": "ready",
            "recommended_action": "run",
            "urgency_rank": 80,
            "urgency_group": "ready",
            "activity_label": "Ready to run",
            "counts": {"pending": 1, "complete": 0, "empty": 0, "unknown": 0},
            "sources": ["state"],
            "repositories": [],
            "repository_count": 0,
            "warnings": [],
        }
    )
    payload.update(overrides)
    return payload
