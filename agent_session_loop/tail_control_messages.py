"""Format run starts, control actions, and mailbox events for the tail."""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime
from typing import Any

from . import (
    clock,
    events,
    workspaces,
)


def _format_local_datetime(value: Any) -> str:
    parsed = clock.parse_datetime(value)
    if parsed is None:
        return "none"
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _progress_commit_label(payload: dict[str, Any]) -> str:
    scope = payload.get("scope")
    session = payload.get("session_id") or payload.get("session")
    if scope == "session" and session is not None:
        return f"session {session}"
    if scope == "end":
        return "end"
    return str(scope or "unknown")


def _format_session_label(payload: dict[str, Any]) -> str:
    session = payload.get("session_id") or payload.get("session", "unknown")
    total = payload.get("total_sessions")
    if total is None:
        return str(session)
    return f"{session}/{total}"


def _run_started_message(payload: dict[str, Any]) -> str:
    run_id = payload.get("run_id", "unknown")
    harness = payload.get("harness", "unknown")
    next_pending = payload.get("next_pending_id") or payload.get("next_pending")
    next_label = "none" if next_pending is None else f"session {next_pending}"
    parts = [
        f"run started: harness {harness}",
        f"run {run_id}",
    ]

    counts = payload.get("counts")
    if isinstance(counts, dict):
        total = 0
        for value in counts.values():
            with contextlib.suppress(TypeError, ValueError):
                total += int(value)
        complete = counts.get("complete", "unknown")
        pending = counts.get("pending", "unknown")
        parts.append(f"{complete}/{total} sessions complete")
        parts.append(f"{pending} pending")

    parts.append(f"next {next_label}")

    approval = payload.get("approval_mode")
    if approval is not None:
        parts.append(f"approval {approval}")

    workspace_root = payload.get("workspace_root")
    if workspace_root is not None:
        parts.append(f"workspace {workspace_root}")
        repositories = payload.get("repositories")
        if isinstance(repositories, list):
            visible, omitted = workspaces.bounded_repository_rows(repositories)
            aliases = [
                str(repository.get("alias"))
                for repository in visible
                if isinstance(repository, dict) and repository.get("alias")
            ]
            if aliases:
                suffix = f",+{omitted}" if omitted else ""
                parts.append(f"repositories {','.join(aliases)}{suffix}")

    run_dir = payload.get("run_dir")
    if run_dir is not None:
        parts.append(f"run dir {run_dir}")

    return "; ".join(str(part) for part in parts)


_ABORT_TERMINAL_ACTIONS = frozenset(
    {
        events.RUN_CONTROL_ACTION_ABORT_TURN_HONORED,
        events.RUN_CONTROL_ACTION_ABORT_TURN_SATISFIED_BY_WORKER,
    }
)


def _abort_grace_remaining(deadline: Any) -> str:
    parsed = clock.parse_datetime(deadline)
    if parsed is None:
        return "grace not started" if deadline is None else "remaining unknown"
    remaining = (parsed - datetime.now(UTC)).total_seconds()
    if remaining <= 0:
        return "grace expired"
    return f"{int(remaining)}s remaining"


def _abort_details(payload: dict[str, Any]) -> list[str]:
    """Render the abort state carried by a run-control event payload.

    The emitter clears ``abort_deadline`` when the abort is consumed, so a
    terminal action still describes the window it acted on but never counts
    down to a deadline that no longer exists.
    """
    terminal = payload.get("action") in _ABORT_TERMINAL_ACTIONS
    if terminal:
        details = ["abort resolved"]
    elif payload.get("abort_requested"):
        details = ["abort pending"]
    else:
        return []
    grace = payload.get("abort_grace_seconds")
    if grace is not None:
        details.append(f"abort grace {grace}s")
    if not terminal:
        details.append(_abort_grace_remaining(payload.get("abort_deadline")))
    reason = payload.get("abort_reason")
    if reason:
        details.append(f"abort reason {reason}")
    return details


def _run_control_message(payload: dict[str, Any]) -> str:
    message = payload.get("message") or payload.get("action") or "unknown action"
    status = "on" if payload.get("stop_after_current") else "off"
    details = [f"stop-after-current {status}"]
    details.extend(_abort_details(payload))
    if payload.get("action") == events.RUN_CONTROL_ACTION_IMPLEMENTATION_MODEL_UPDATED:
        target_harness = payload.get("target_harness") or "unknown"
        changed_fields = payload.get("changed_fields")
        if isinstance(changed_fields, list) and changed_fields:
            changed = ", ".join(str(field) for field in changed_fields)
        else:
            changed = "none"
        overrides = payload.get("implementation_overrides")
        if not isinstance(overrides, dict):
            overrides = {}
        details.extend(
            (
                f"target harness {target_harness}",
                f"changed {changed}",
                f"effective overrides {json.dumps(overrides, sort_keys=True)}",
            )
        )
    session = payload.get("session")
    if session is not None:
        details.append(f"session {_format_session_label(payload)}")
    turn_index = payload.get("turn_index")
    if turn_index is not None:
        details.append(f"turn {turn_index}")
    return f"run control: {message} ({', '.join(details)})"


def _mailbox_message(payload: dict[str, Any]) -> str:
    action = payload.get("action") or "unknown"
    if action == events.MAILBOX_ACTION_GUIDANCE_OBSERVED:
        details = [
            f"id {payload.get('message_id', 'unknown')}",
            f"sequence {payload.get('seq', 'unknown')}",
            f"length {payload.get('length', 'unknown')}",
            f"digest {payload.get('digest', 'unknown')}",
        ]
        session = payload.get("session_id") or payload.get("session")
        if session is not None:
            details.append(f"session {session}")
        turn_index = payload.get("turn_index")
        if turn_index is not None:
            details.append(f"turn {turn_index}")
        return f"mailbox: guidance observed ({', '.join(details)})"
    if action == events.MAILBOX_ACTION_NOTICE_PUBLISHED:
        details = [
            f"id {payload.get('message_id', 'unknown')}",
            f"sequence {payload.get('seq', 'unknown')}",
            f"length {payload.get('length', 'unknown')}",
            f"digest {payload.get('digest', 'unknown')}",
            f"run {payload.get('run_id', 'unknown')}",
            f"session {_format_session_label(payload)}",
            f"turn {payload.get('turn_index', 'unknown')}",
        ]
        return f"mailbox: notice published ({', '.join(details)})"
    if action == events.MAILBOX_ACTION_WAIT_STARTED:
        details = [
            f"question {payload.get('question_id', 'unknown')}",
            f"session {_format_session_label(payload)}",
            f"turn {payload.get('turn_index', 'unknown')}",
            f"pending outbox {payload.get('pending_outbox', 'unknown')}",
            f"base deadline {payload.get('base_deadline', 'unknown')}",
        ]
        unsupervised_deadline = payload.get("unsupervised_deadline_at")
        if unsupervised_deadline is None:
            details.append(f"hard deadline {payload.get('hard_deadline', 'unknown')}")
        else:
            details.append(f"unsupervised deadline {unsupervised_deadline}")
            details.append(
                "unsupervised ceiling "
                f"{payload.get('unsupervised_ceiling_seconds', 'unknown')}s"
            )
        lease_activity = payload.get("last_lease_activity_at")
        if lease_activity is not None:
            details.append(f"last lease activity {lease_activity}")
        return f"mailbox: reply wait started ({', '.join(details)})"
    if action == events.MAILBOX_ACTION_WAIT_CLEARED:
        details = [
            f"question {payload.get('question_id', 'unknown')}",
            f"session {_format_session_label(payload)}",
            f"turn {payload.get('turn_index', 'unknown')}",
            f"reason {payload.get('reason', 'unknown')}",
            f"pending outbox {payload.get('pending_outbox', 'unknown')}",
        ]
        lease_supervisor_id = payload.get("lease_supervisor_id")
        if lease_supervisor_id is not None:
            details.append(f"lease supervisor {lease_supervisor_id}")
        lease_updated_at = payload.get("lease_updated_at")
        if lease_updated_at is not None:
            details.append(f"lease updated {lease_updated_at}")
        lease_reason = payload.get("lease_reason")
        if lease_reason is not None:
            details.append(f"lease reason {lease_reason}")
        suspend_skew = payload.get("suspend_skew_seconds")
        if suspend_skew is not None:
            details.append(f"suspend skew {suspend_skew}s")
        suspend_grace = payload.get("suspend_lease_grace_seconds")
        if suspend_grace is not None:
            details.append(f"suspend lease grace {suspend_grace}s")
        if payload.get("reason") == "resolution":
            details.extend(
                (
                    f"resolution {payload.get('resolution_message_id', 'unknown')}",
                    f"resolution sequence {payload.get('resolution_seq', 'unknown')}",
                    f"resolution action {payload.get('resolution_action', 'unknown')}",
                )
            )
        return f"mailbox: reply wait cleared ({', '.join(details)})"
    if action == events.MAILBOX_ACTION_WAIT_PREPARATION_FAILED:
        details = [
            f"reason {payload.get('reason', 'unknown')}",
            f"session {_format_session_label(payload)}",
            f"turn {payload.get('turn_index', 'unknown')}",
        ]
        run_id = payload.get("run_id")
        if run_id is not None:
            details.append(f"run {run_id}")
        return f"mailbox: reply wait preparation failed ({', '.join(details)})"
    if action in (
        events.MAILBOX_ACTION_GUIDANCE_DELIVERED,
        events.MAILBOX_ACTION_GUIDANCE_DELIVERY_DEFERRED,
    ):
        # Delivery is evidence about transport, so the line names identity,
        # channel, and outcome — never anything derived from the body.
        details = [
            f"guidance {payload.get('guidance_id', 'unknown')}",
            f"state {payload.get('delivery_state', 'unknown')}",
            f"reason {payload.get('delivery_reason', 'unknown')}",
            f"channel {payload.get('channel', 'unknown')}",
        ]
        harness = payload.get("harness")
        if harness is not None:
            details.append(f"harness {harness}")
        run_id = payload.get("run_id")
        if run_id is not None:
            details.append(f"run {run_id}")
            details.append(f"session {_format_session_label(payload)}")
            details.append(f"turn {payload.get('turn_index', 'unknown')}")
        label = (
            "guidance delivered"
            if action == events.MAILBOX_ACTION_GUIDANCE_DELIVERED
            else "guidance delivery deferred"
        )
        return f"mailbox: {label} ({', '.join(details)})"
    return f"mailbox: {action}"
