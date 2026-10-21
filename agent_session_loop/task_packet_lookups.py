"""Plan lookups and resolution identity the task packet's callers need."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import (
    mailbox_vocabulary,
    plans_errors,
    plans_models,
    plans_session_ids,
    run_prompts,
)


def _predecessor_session_id(
    snapshot: plans_models.PlanSnapshot, session_id: str
) -> str | None:
    """Return the id of the session immediately before ``session_id``.

    Uses the plan's declared session order (not integer arithmetic) so dotted
    ids resolve correctly. Returns ``None`` for the first session or when the
    session is not found in the snapshot.
    """
    index = next(
        (
            position
            for position, session in enumerate(snapshot.sessions)
            if session.id == session_id
        ),
        None,
    )
    if index is None or index == 0:
        return None
    return snapshot.sessions[index - 1].id


def _session_has_expansion(plan_dir: Path, session_id: str) -> bool:
    """Return True when ``session-<id>.expand.md`` exists in ``plan_dir``."""
    try:
        selected = plans_session_ids.parse_session_id(session_id)
    except plans_errors.PlanError:
        return False
    return (plan_dir / selected.expansion_filename).is_file()


def _resolution_source_metadata(
    pending: run_prompts._PendingResolution,
) -> dict[str, Any]:
    """Return fixed-shape, body-free identity for a pending resolution."""
    if pending.action not in mailbox_vocabulary.RESOLUTION_ACTIONS:
        raise ValueError("pending resolution has an invalid action")
    if pending.note is not None and not isinstance(pending.note, str):
        raise ValueError("pending resolution note must be a string or None")
    return {
        "run_id": pending.context.run_id,
        "session_id": pending.context.session_id,
        "turn_index": pending.context.turn_index,
        "question_id": pending.question_id,
        "resolution_message_id": pending.resolution_message_id,
        "resolution_seq": pending.resolution_seq,
        "action": pending.action,
        "note_length": (len(pending.note) if pending.note is not None else None),
    }
