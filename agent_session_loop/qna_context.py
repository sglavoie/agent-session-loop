"""Validated pause and live-wait Q&A context loading."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import events, plans_models, plans_snapshot, state, state_locks, state_payloads
from .qna_models import (
    REASON_MISSING_QUESTION_CONTEXT,
    REASON_NO_PAUSED_RUN,
    REASON_REPLY_CONTEXT_MISMATCH,
    QnAContext,
    QnAError,
)
from .qna_values import (
    _coerce_dict,
    _coerce_path,
    _expired_wait_recovery_payload,
    _is_paused,
    _latest_completed_turn,
    _optional_int,
    _optional_str,
    _read_last_message,
    _read_state_payload,
    _state_dir_override_from_payload,
    _turn_session_id,
    _validate_waiting_identity,
    _wait_ceiling_details,
)


def load_qna_context(
    plan_dir: str | Path,
    *,
    state_dir: str | Path | None = None,
    reclaim: bool = True,
) -> QnAContext:
    """Load and validate the latest paused or live-wait question context.

    ``reclaim=False`` classifies the lock without ever removing it, so a caller
    that has promised to write nothing observes the corpus rather than changing
    it.

    Raises :class:`QnAError` with a stable ``reason`` on every recognized
    failure path:

    - ``no-paused-run`` when the state file is missing, unreadable, neither
      paused nor waiting, or has no completed turn that recorded a session id.
    - ``missing-question-context`` when the selected turn or live-wait record
      is incomplete, or its ``last-message`` artifact is not readable/nonempty.
    - ``reply-context-mismatch`` when the selected turn's session id no longer
      matches the plan's current ``next_pending_id``.
    """
    snapshot = plans_snapshot.inspect_plan(plan_dir)
    plan_state = state.plan_state_dir(snapshot.plan_dir, state_dir)
    state_path = plan_state / events.STATE_FILE_NAME
    payload = _read_state_payload(state_path)

    waiting_for_reply: dict[str, Any] | None = None
    if not _is_paused(payload):
        raw_waiting = payload.get("waiting_for_reply")
        if isinstance(raw_waiting, dict):
            waiting_for_reply = dict(raw_waiting)
        else:
            raise QnAError(
                REASON_NO_PAUSED_RUN,
                "latest runner state is not paused",
                details={
                    "state_path": str(state_path),
                    "paused": payload.get("paused"),
                },
            )

    return build_qna_context(
        snapshot,
        payload,
        plan_state=plan_state,
        state_path=state_path,
        waiting_for_reply=waiting_for_reply,
        reclaim=reclaim,
    )


def build_qna_context(
    snapshot: plans_models.PlanSnapshot,
    payload: dict[str, Any],
    *,
    plan_state: Path,
    state_path: Path,
    waiting_for_reply: dict[str, Any] | None = None,
    reclaim: bool = True,
) -> QnAContext:
    """Build validated latest-turn context for a pause or proposed/live wait.

    Passing ``waiting_for_reply`` selects the nonterminal path and permits the
    caller to validate a proposed wait before it is persisted. Omitting it
    preserves the terminal-pause contract and requires ``paused is True``.
    ``reclaim=False`` keeps the wait classification free of the lock-hygiene
    side effect.
    """
    if waiting_for_reply is not None:
        lock_info = state_locks._read_lock_path(
            plan_state / state.LOCK_FILE_NAME,
            snapshot.plan_dir,
            reclaim=reclaim,
        )
        wait_status = state_payloads.waiting_for_reply_status(
            waiting_for_reply,
            lock_info=lock_info,
        )
        if wait_status == state_payloads.WAITING_FOR_REPLY_STATUS_INVALID:
            raise QnAError(
                REASON_MISSING_QUESTION_CONTEXT,
                "waiting-for-reply state has an invalid unsupervised ceiling",
                details={
                    "state_path": str(state_path),
                    "question_id": waiting_for_reply.get("question_id"),
                    **_wait_ceiling_details(waiting_for_reply),
                },
            )
        if wait_status == state_payloads.WAITING_FOR_REPLY_STATUS_EXPIRED:
            payload = _expired_wait_recovery_payload(
                payload,
                waiting_for_reply,
            )
            waiting_for_reply = None

    next_pending = snapshot.next_pending
    if next_pending is None:
        raise QnAError(
            REASON_NO_PAUSED_RUN,
            "plan has no pending session waiting on a reply",
            details={
                "plan_dir": str(snapshot.plan_dir),
                "state_path": str(state_path),
            },
        )

    context_kind = "waiting" if waiting_for_reply is not None else "pause"
    if context_kind == "pause" and not _is_paused(payload):
        raise QnAError(
            REASON_NO_PAUSED_RUN,
            "latest runner state is not paused",
            details={
                "state_path": str(state_path),
                "paused": payload.get("paused"),
            },
        )

    turn = _latest_completed_turn(payload, state_path=state_path)

    session_id = _turn_session_id(turn, state_path=state_path)
    question_id: str | None = None
    if waiting_for_reply is not None:
        question_id = _validate_waiting_identity(
            waiting_for_reply,
            turn=turn,
            turn_session_id=session_id,
            state_path=state_path,
        )
    if session_id != next_pending.id:
        source_label = "waiting" if context_kind == "waiting" else "paused"
        session_detail_key = (
            "waiting_session_id" if context_kind == "waiting" else "paused_session_id"
        )
        raise QnAError(
            REASON_REPLY_CONTEXT_MISMATCH,
            (
                f"{source_label} session {session_id!r} no longer matches the "
                f"current pending session {next_pending.id!r}"
            ),
            details={
                session_detail_key: session_id,
                "next_pending_id": next_pending.id,
                "state_path": str(state_path),
            },
        )

    last_message_path = _coerce_path(turn.get("last_message_path"))
    if last_message_path is None:
        source_label = "waiting" if context_kind == "waiting" else "paused"
        raise QnAError(
            REASON_MISSING_QUESTION_CONTEXT,
            (
                f"latest {source_label} turn does not record a "
                "last-message artifact path"
            ),
            details={
                "state_path": str(state_path),
                "session_id": session_id,
            },
        )

    last_message = _read_last_message(last_message_path, session_id=session_id)

    return QnAContext(
        plan_dir=snapshot.plan_dir,
        state_dir=plan_state,
        state_path=state_path,
        run_id=_optional_str(payload.get("run_id")),
        run_dir=_coerce_path(payload.get("run_dir")),
        pause_reason=(
            _optional_str(payload.get("pause_reason") or payload.get("reason"))
            if context_kind == "pause"
            else None
        ),
        pause_message=(
            _optional_str(payload.get("pause_message"))
            if context_kind == "pause"
            else None
        ),
        pause_details=(
            _coerce_dict(payload.get("pause_details") or payload.get("details"))
            if context_kind == "pause"
            else {}
        ),
        harness=_optional_str(turn.get("harness") or payload.get("harness")),
        model=_optional_str(turn.get("model") or payload.get("model")),
        model_source=_optional_str(
            turn.get("model_source") or payload.get("model_source")
        ),
        effort=_optional_str(turn.get("effort") or payload.get("effort")),
        effort_source=_optional_str(
            turn.get("effort_source") or payload.get("effort_source")
        ),
        session_id=session_id,
        next_pending_id=next_pending.id,
        turn_index=_optional_int(turn.get("turn_index")),
        artifact_dir=_coerce_path(turn.get("artifact_dir")),
        stdout_path=_coerce_path(turn.get("stdout_path")),
        stderr_path=_coerce_path(turn.get("stderr_path")),
        last_message_path=last_message_path,
        result_path=_coerce_path(turn.get("result_path")),
        last_message=last_message,
        state_dir_override=_state_dir_override_from_payload(payload),
        context_kind=context_kind,
        question_id=question_id,
        waiting_for_reply=(
            dict(waiting_for_reply) if waiting_for_reply is not None else None
        ),
    )


def qna_context_payload(context: QnAContext) -> dict[str, Any]:
    """Render a :class:`QnAContext` as a JSON-friendly payload."""
    payload = {
        "plan_dir": str(context.plan_dir),
        "state_dir": str(context.state_dir),
        "state_path": str(context.state_path),
        "run_id": context.run_id,
        "run_dir": str(context.run_dir) if context.run_dir is not None else None,
        "pause_reason": context.pause_reason,
        "pause_message": context.pause_message,
        "pause_details": dict(context.pause_details),
        "harness": context.harness,
        "model": context.model,
        "model_source": context.model_source,
        "effort": context.effort,
        "effort_source": context.effort_source,
        "session_id": context.session_id,
        "next_pending_id": context.next_pending_id,
        "turn_index": context.turn_index,
        "artifact_dir": (
            str(context.artifact_dir) if context.artifact_dir is not None else None
        ),
        "stdout_path": (
            str(context.stdout_path) if context.stdout_path is not None else None
        ),
        "stderr_path": (
            str(context.stderr_path) if context.stderr_path is not None else None
        ),
        "last_message_path": str(context.last_message_path),
        "result_path": (
            str(context.result_path) if context.result_path is not None else None
        ),
        "last_message": context.last_message,
    }
    if context.context_kind == "waiting":
        payload.update(
            {
                "context_kind": "waiting",
                "question_id": context.question_id,
                "waiting_for_reply": (
                    dict(context.waiting_for_reply)
                    if context.waiting_for_reply is not None
                    else None
                ),
            }
        )
    return payload


def qna_error_payload(error: QnAError) -> dict[str, Any]:
    """Render a :class:`QnAError` as a JSON-friendly payload."""
    return {
        "reason": error.reason,
        "error": error.error,
        "details": dict(error.details),
    }
