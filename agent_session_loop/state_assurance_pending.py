"""Pending-audit records left behind by runs that died owing an assurance verdict."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import artifacts, events
from .state import RUNS_DIR_NAME, _nonempty_str

# Run-relative record naming a run whose own process died owing an audit. It
# lives in the run directory rather than in `state.json` because the next run
# of the same plan overwrites `state.json`, and it carries its own copy of the
# run configuration so the audit role can still be resolved afterwards.
ASSURANCE_PENDING_FILE_NAME = "assurance-pending.json"


ASSURANCE_PENDING_SCHEMA_VERSION = 1


# The run wrote no assurance object at all, so it comes from an ASL that had no
# audit. Catch-up reports it rather than inventing a verdict for history.
ASSURANCE_SKIP_PREDATES = "predates-assurance"


# The run already reached a settled assurance state (a verdict, or an opt-out).
ASSURANCE_SKIP_SETTLED = "audit-already-settled"


# Assurance states that still owe an audit. Mirrors
# ``run_coordination_terminal.AUDITABLE_ASSURANCE_STATES``; kept as a literal here
# because state sits below that boundary.
_ASSURANCE_OWED_STATES = frozenset({"pending", "unavailable"})


def _assurance_skip_reason(state_obj: dict[str, Any]) -> str | None:
    """Say why catch-up must not audit this run, or ``None`` when it should.

    A run that recorded no assurance object at all predates auditing entirely,
    and silently auditing it would flood the shared inbox with history nobody
    asked about. A run that already reached a verdict, or opted out, has
    already been decided. Only a run that was eligible and still owed an audit
    is catch-up's to run.
    """
    assurance = state_obj.get("assurance")
    if not isinstance(assurance, dict):
        return ASSURANCE_SKIP_PREDATES
    if not assurance.get("eligible"):
        return ASSURANCE_SKIP_SETTLED
    if assurance.get("state") not in _ASSURANCE_OWED_STATES:
        return ASSURANCE_SKIP_SETTLED
    return None


def _assurance_pending_record(
    state_obj: dict[str, Any],
    *,
    plan_state_dir_path: Path,
    run_id: str,
    trigger: str,
    detected_at: str,
) -> dict[str, Any]:
    """Describe the audit an abandoned run still owes, self-contained.

    Everything an audit needs is copied in rather than referenced, because the
    next run of this plan rewrites ``state.json`` and would otherwise take the
    dead run's harness selection and workspace binding with it.
    """
    raw_config = state_obj.get("run_config")
    run_config = raw_config if isinstance(raw_config, dict) else {}
    # A workspace run binds its root explicitly; a legacy single-repository run
    # only ever recorded the working directory it audited from.
    workspace_root = next(
        (
            value
            for value in (
                _nonempty_str(state_obj.get("workspace_root")),
                _nonempty_str(run_config.get("workspace_root")),
                _nonempty_str(state_obj.get("cwd")),
                _nonempty_str(run_config.get("cwd")),
            )
            if value is not None
        ),
        None,
    )
    return {
        "schema_version": ASSURANCE_PENDING_SCHEMA_VERSION,
        "run_id": run_id,
        "recorded_at": detected_at,
        # When the dead run started, as opposed to when its death was noticed:
        # only the start places the run in a code generation.
        "run_started_at": _nonempty_str(state_obj.get("run_started_at")),
        "trigger": trigger,
        "outcome": events.RUN_OUTCOME_ABANDONED,
        "skip": _assurance_skip_reason(state_obj),
        "plan_dir": _nonempty_str(state_obj.get("plan_dir")),
        "state_dir": str(plan_state_dir_path),
        "run_dir": str(plan_state_dir_path / RUNS_DIR_NAME / run_id),
        "workspace_root": workspace_root,
        "run_config": run_config or None,
    }


def _write_assurance_pending_record(
    plan_state_dir_path: Path,
    payload: dict[str, Any],
    *,
    run_id: str,
    trigger: str,
    detected_at: str,
) -> dict[str, Any] | None:
    """Write the pending-audit record beside the abandoned run's artifacts.

    Best effort like everything else on this path: an unwritable run directory
    costs the catch-up audit, not the terminal record that proves the run
    ended. Returns the record so the same facts ride the terminal state and
    event rather than being derived twice.
    """
    record = _assurance_pending_record(
        payload,
        plan_state_dir_path=plan_state_dir_path,
        run_id=run_id,
        trigger=trigger,
        detected_at=detected_at,
    )
    try:
        run_dir = Path(record["run_dir"])
        run_dir.mkdir(parents=True, exist_ok=True)
        artifacts.atomic_write_bytes(
            run_dir / ASSURANCE_PENDING_FILE_NAME,
            json.dumps(record, sort_keys=True).encode("utf-8") + b"\n",
            durable=True,
        )
    except OSError:
        return None
    return record
