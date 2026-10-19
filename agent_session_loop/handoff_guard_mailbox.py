"""Refuse completion while the worker still owes a blocking mailbox reply."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import (
    durability,
    mailbox,
    mailbox_constants,
    mailbox_cursors,
    mailbox_models,
    mailbox_vocabulary,
    plans_models,
    plans_payloads,
    plans_snapshot,
    run_persistence,
    run_termination,
    turn_execution_commit,
)
from .handoff_guard_sessions import (
    HandoffNoteAppender,
    _reopened,
    _revert_session_status_to_pending,
)


def _mailbox_completion_guard_evidence(
    *,
    plan_dir: Path,
    state_dir_arg: str | None,
    run_id: str,
    session_id: str,
    turn_index: int,
) -> dict[str, Any] | None:
    """Return body-free evidence when a blocking worker question was ignored."""
    try:
        view = mailbox.read_mailbox(plan_dir, state_dir=state_dir_arg)
    except (OSError, mailbox_models.MailboxError) as error:
        return {
            "mailbox_path": str(
                mailbox_cursors.mailbox_path_for_plan(plan_dir, state_dir_arg)
            ),
            "mailbox_schema_version": mailbox_constants.MAILBOX_SCHEMA_VERSION,
            "projection_seq": None,
            "read_error": (
                error.reason
                if isinstance(error, mailbox_models.MailboxError)
                else mailbox_constants.ERROR_MAILBOX_READ
            ),
            "questions": [],
        }
    violations: list[dict[str, Any]] = []
    for question in view.messages:
        if (
            question.kind != mailbox_vocabulary.KIND_QUESTION
            or question.version < mailbox_constants.MAILBOX_WAIT_SCHEMA_VERSION
            or question.delivery_mode != mailbox_vocabulary.DELIVERY_MODE_WAIT
            or question.waiter_origin != mailbox_vocabulary.WAITER_ORIGIN_WORKER
            or question.run_id != run_id
            or question.session_id != session_id
            or question.turn_index != turn_index
        ):
            continue
        closure = view.closure_for(question.id)
        receipt = None if closure is None else view.receipt_for_closure(closure.id)
        if closure is None:
            violation = "unanswered"
        elif receipt is None:
            violation = "unconsumed"
        elif (
            closure.kind == mailbox_vocabulary.KIND_RESOLUTION
            and closure.action == mailbox_vocabulary.RESOLUTION_ACTION_PAUSE
        ):
            violation = "pause-resolved"
        else:
            continue
        record: dict[str, Any] = {
            "question_id": question.id,
            "question_seq": question.seq,
            "delivery_mode": question.delivery_mode,
            "violation": violation,
            "active_wait": view.active_wait_for(question.id) is not None,
        }
        if closure is not None:
            record.update(
                closure_id=closure.id,
                closure_seq=closure.seq,
                closure_kind=closure.kind,
                resolution_action=closure.action,
            )
        if receipt is not None:
            record.update(receipt_id=receipt.id, receipt_seq=receipt.seq)
        violations.append(record)
    if not violations:
        return None
    return {
        "mailbox_path": str(view.mailbox_path),
        "mailbox_schema_version": mailbox_constants.MAILBOX_SCHEMA_VERSION,
        "projection_seq": view.last_seq,
        "questions": violations,
    }


def _enforce_mailbox_completion_guard(
    *,
    event_path: Path,
    payload: dict[str, Any],
    persistence: run_persistence._RunPersistence,
    after: plans_models.PlanSnapshot,
    session_id: str,
    session_label: str,
    session_details: dict[str, Any],
    turn_index: int,
    state_dir_arg: str | None,
    turn_payload: dict[str, Any],
    paths: dict[str, Path],
    plan_cache: plans_snapshot.PlanSnapshotCache,
    append_handoff_note: HandoffNoteAppender,
    reinspect: run_termination.SnapshotReinspector,
) -> bool:
    """Reject completion that bypassed a v3 blocking-question lifecycle."""
    evidence = _mailbox_completion_guard_evidence(
        plan_dir=after.plan_dir,
        state_dir_arg=state_dir_arg,
        run_id=str(payload.get("run_id")),
        session_id=session_id,
        turn_index=turn_index,
    )
    if evidence is None:
        return False
    reverted = _revert_session_status_to_pending(
        after,
        session_id,
        failure_note=(
            "runner rejected completion: a blocking mailbox question was "
            "unanswered, unconsumed, or resolved with pause"
        ),
        append_handoff_note=append_handoff_note,
    )
    snapshot_after = reinspect(after.plan_dir, after, reverted, plan_cache)
    guard = {
        "passed": False,
        "reverted_to_pending": _reopened(reverted),
        **evidence,
    }
    with persistence.lock:
        payload["final_snapshot"] = plans_payloads.snapshot_payload(snapshot_after)
        turn_payload["mailbox_completion_guard"] = guard
        durability.write_json_file(paths["result"], turn_payload)
    turn_execution_commit.record_turn_rejected(
        event_path=event_path,
        payload=payload,
        persistence=persistence,
        turn_payload=turn_payload,
        session_id=session_id,
        guard="mailbox-completion-guard",
        reason="blocked",
        revert=reverted,
    )
    run_termination._pause_run(
        event_path,
        payload,
        reason="blocked",
        message=(
            f"{session_label} completed while a blocking mailbox question "
            "was not safely consumed"
        ),
        details={**session_details, "mailbox": evidence},
        persistence=persistence,
    )
    return True
