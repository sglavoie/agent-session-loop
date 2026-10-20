"""Shared fixtures for test_asl_agent_mux_projection."""

from __future__ import annotations

from typing import Any

from agent_session_loop import agent_mux_contract as contract
from agent_session_loop import (
    agent_mux_delivery,
    verification_transitions,
)

PROJECTION_SCHEMA = contract.load_schema(contract.COMPONENT_PROJECTION)


HOOK_SCHEMA = contract.load_schema(contract.COMPONENT_LIFECYCLE_HOOK)


RUN_STARTED_AT = "2026-06-19T14:00:00+00:00"


UPDATED_AT = "2026-06-19T14:31:02.481900+00:00"


#: Read at its owning module rather than spelled inline: the reason is 62
#: characters and the fixture payloads that carry it nest four levels deep.
DEFERRED_REASON = verification_transitions.REVIEW_BROAD_FAILURES_DEFERRED_REASON


def _base_payload(**overrides: Any) -> dict[str, Any]:
    """A minimally projectable run payload, before any phase-specific state."""
    payload: dict[str, Any] = {
        "run_id": "20260619T140000Z-1",
        "plan_dir": "/fixtures/plans/plan-alpha",
        "workspace_root": "/fixtures/workspaces/alpha",
        "registry_digest": "3398ee413bd2a03b",
        "kitty_window_id": 17,
        "run_started_at": RUN_STARTED_AT,
        "harness": "claude",
        "turns": [],
    }
    payload.update(overrides)
    return payload


def _turn(**overrides: Any) -> dict[str, Any]:
    turn: dict[str, Any] = {
        "turn_index": 4,
        "session_id": "3",
        "harness": "claude",
    }
    turn.update(overrides)
    return turn


def _strings(document: Any) -> list[str]:
    """Every string anywhere in ``document``, keys included."""
    found: list[str] = []
    if isinstance(document, dict):
        for key, value in document.items():
            found.append(key)
            found.extend(_strings(value))
    elif isinstance(document, list):
        for item in document:
            found.extend(_strings(item))
    elif isinstance(document, str):
        found.append(document)
    return found


_REASON_BY_EVENT = {
    agent_mux_delivery.START_EVENT: agent_mux_delivery.START_REASON,
    agent_mux_delivery.TURN_START_EVENT: agent_mux_delivery.TURN_START_REASON,
    agent_mux_delivery.TURN_HEARTBEAT_EVENT: agent_mux_delivery.TURN_HEARTBEAT_REASON,
    agent_mux_delivery.TURN_FINISH_EVENT: agent_mux_delivery.TURN_FINISH_REASON,
    agent_mux_delivery.TURN_HANG_SUSPECTED_EVENT: (
        agent_mux_delivery.TURN_HANG_SUSPECTED_REASON
    ),
    agent_mux_delivery.TURN_HANG_CLEARED_EVENT: (
        agent_mux_delivery.TURN_HANG_CLEARED_REASON
    ),
    agent_mux_delivery.VERIFICATION_START_EVENT: (
        agent_mux_delivery.VERIFICATION_START_REASON
    ),
    agent_mux_delivery.VERIFICATION_HEARTBEAT_EVENT: (
        agent_mux_delivery.VERIFICATION_HEARTBEAT_REASON
    ),
    agent_mux_delivery.VERIFICATION_FINISH_EVENT: (
        agent_mux_delivery.VERIFICATION_FINISH_REASON
    ),
    agent_mux_delivery.WAIT_START_EVENT: agent_mux_delivery.WAIT_START_REASON,
    agent_mux_delivery.WAIT_END_EVENT: agent_mux_delivery.WAIT_END_REASON,
    agent_mux_delivery.MAILBOX_QUESTION_EVENT: (
        agent_mux_delivery.MAILBOX_QUESTION_REASON
    ),
    agent_mux_delivery.MAILBOX_REPLY_EVENT: agent_mux_delivery.MAILBOX_REPLY_REASON,
    agent_mux_delivery.MAILBOX_RESOLUTION_EVENT: (
        agent_mux_delivery.MAILBOX_RESOLUTION_REASON
    ),
    agent_mux_delivery.PAUSE_EVENT: agent_mux_delivery.PAUSE_REASON,
    agent_mux_delivery.FINISH_EVENT: agent_mux_delivery.FINISH_REASON,
}


#: The turn record ASL threads into a hook payload, at its widest.
_ACTIVE_TURN = {
    "turn_index": 4,
    "session": "3",
    "session_id": "3",
    "session_number": 3,
    "total_sessions": 14,
    "harness": "claude",
    "artifact_dir": "/fixtures/state/8f14e45fceea167a/turns/4",
    "stdout_path": "/fixtures/state/8f14e45fceea167a/turns/4/stdout.log",
    "stderr_path": "/fixtures/state/8f14e45fceea167a/turns/4/stderr.log",
    "last_message_path": "/fixtures/state/8f14e45fceea167a/turns/4/last.txt",
    "result_path": "/fixtures/state/8f14e45fceea167a/turns/4/result.json",
    "started_at": "2026-06-19T14:29:00+00:00",
    "last_activity_at": "2026-06-19T14:30:41+00:00",
    "heartbeat_count": 3,
}


#: The verification identity every verification boundary carries.
_VERIFICATION_IDENTITY = {
    "run_id": "20260619T140000Z-1",
    "turn_index": 4,
    "session": "3",
    "session_id": "3",
    "total_sessions": 14,
}


def _emitted_hook_payloads(hook: Any) -> dict[str, dict[str, Any]]:
    """One payload per shape ASL actually pipes to ``amx hook-indicator``.

    Restated here rather than captured from a live run so the schema is checked
    against every emitter, including the CLI-side mailbox boundaries that no
    single run exercises.
    """
    return {
        "started": hook(agent_mux_delivery.START_EVENT),
        "turn-started": hook(
            agent_mux_delivery.TURN_START_EVENT, active_turn=_ACTIVE_TURN
        ),
        "turn-heartbeat": hook(
            agent_mux_delivery.TURN_HEARTBEAT_EVENT, active_turn=_ACTIVE_TURN
        ),
        "turn-finished": hook(
            agent_mux_delivery.TURN_FINISH_EVENT, active_turn=_ACTIVE_TURN
        ),
        "turn-hang-suspected": hook(
            agent_mux_delivery.TURN_HANG_SUSPECTED_EVENT,
            active_turn=_ACTIVE_TURN,
            extra={
                "hang_reason": "no_output",
                "hang_idle_seconds": 903.5,
                "harness_pid": 58971,
            },
        ),
        "turn-hang-cleared": hook(
            agent_mux_delivery.TURN_HANG_CLEARED_EVENT,
            active_turn=_ACTIVE_TURN,
            extra={
                "hang_reason": "no_output",
                "hang_idle_seconds": None,
                "harness_pid": 58971,
            },
        ),
        "verification-started": hook(
            agent_mux_delivery.VERIFICATION_START_EVENT,
            extra=_VERIFICATION_IDENTITY
            | {"check_count": 5, "started_at": "2026-06-19T14:31:02+00:00"},
        ),
        "verification-heartbeat": hook(
            agent_mux_delivery.VERIFICATION_HEARTBEAT_EVENT,
            extra=_VERIFICATION_IDENTITY
            | {
                "check_index": 1,
                "check_count": 5,
                "check_name": "asl-tests",
                "check_kind": "command",
                "heartbeat_count": 4,
                "last_heartbeat_at": "2026-06-19T14:35:02+00:00",
            },
        ),
        "verification-heartbeat-between-checks": hook(
            # ``_record_heartbeat`` substitutes an empty name and kind when no
            # check is current, so the schema has to accept that spelling.
            agent_mux_delivery.VERIFICATION_HEARTBEAT_EVENT,
            extra=_VERIFICATION_IDENTITY
            | {
                "check_index": 0,
                "check_count": 5,
                "check_name": "",
                "check_kind": "",
                "heartbeat_count": 1,
                "last_heartbeat_at": "2026-06-19T14:32:02+00:00",
            },
        ),
        "verification-finished": hook(
            agent_mux_delivery.VERIFICATION_FINISH_EVENT,
            extra=_VERIFICATION_IDENTITY
            | {
                "check_count": 5,
                "executed_count": 5,
                "passed_count": 4,
                "skipped_count": 0,
                "gate_count": 4,
                "diagnostic_count": 1,
                "passed": False,
                "heartbeat_count": 6,
                "acceptance": {
                    "accepted": True,
                    "reason": DEFERRED_REASON,
                    "review_outcome": "findings",
                    "deferred_failure_count": 1,
                    "deferred_failure_names": ["review-broad:correctness"],
                },
            },
        ),
        "wait-started": hook(
            agent_mux_delivery.WAIT_START_EVENT,
            extra={"question_id": "q-7", "question_preview": "which alias owns this?"},
        ),
        "wait-ended-consumed": hook(
            agent_mux_delivery.WAIT_END_EVENT,
            extra={
                "question_id": "q-7",
                "wait_reason": "reply",
                "delivery_state": "consumed",
                "closure_consumed": True,
                "closure_id": "closure-3",
            },
        ),
        "wait-ended-unconsumed": hook(
            agent_mux_delivery.WAIT_END_EVENT,
            extra={
                "question_id": "q-7",
                "wait_reason": "timeout",
                "delivery_state": "unconsumed",
                "closure_consumed": False,
            },
        ),
        "mailbox-question": hook(
            agent_mux_delivery.MAILBOX_QUESTION_EVENT,
            extra={
                "question_id": "q-7",
                "question_created_at": "2026-06-19T15:11:40+00:00",
                "unanswered_questions": 1,
                "delivery_state": "open",
                "mailbox_projection_sequence": "31",
                "source_event_id": "20260619T140000Z-1:mailbox-question:31",
            },
        ),
        "mailbox-reply": hook(
            agent_mux_delivery.MAILBOX_REPLY_EVENT,
            extra={
                "question_id": "q-7",
                "delivery_state": "accepted",
                "consumed": False,
                "mailbox_projection_sequence": "32",
                "source_event_id": "20260619T140000Z-1:mailbox-reply:32",
            },
        ),
        "mailbox-resolution": hook(
            agent_mux_delivery.MAILBOX_RESOLUTION_EVENT,
            extra={
                "question_id": "q-7",
                "resolution_action": "withdraw",
                "delivery_state": "accepted",
                "consumed": False,
                "mailbox_projection_sequence": "33",
                "source_event_id": "20260619T140000Z-1:mailbox-resolution:33",
            },
        ),
        "paused": hook(
            agent_mux_delivery.PAUSE_EVENT,
            payload={
                "pause_reason": "blocked",
                "pause_message": "blocked - a mailbox question is unanswered",
            },
            extra={
                "pause_reason": "blocked",
                "pause_message": "blocked - a mailbox question is unanswered",
            },
        ),
        "finished": hook(
            agent_mux_delivery.FINISH_EVENT,
            payload={"run_finished_at": "2026-06-19T16:40:00+00:00"},
        ),
    }
