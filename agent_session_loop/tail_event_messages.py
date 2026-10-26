"""Format lifecycle event messages and timestamped tail lines."""

from __future__ import annotations

from typing import Any

from . import (
    events,
    tail_records,
    workspaces,
)
from .formatting import (
    format_duration_seconds,
    format_turn_finished_message,
    format_turn_started_message,
)
from .tail_control_messages import (
    _format_local_datetime,
    _format_session_label,
    _mailbox_message,
    _progress_commit_label,
    _run_control_message,
    _run_started_message,
)


def _event_message(name: str, payload: dict[str, Any]) -> str:
    if name == "run_started":
        return _run_started_message(payload)
    if name == "turn_started":
        return format_turn_started_message(payload)
    if name == events.TURN_HEARTBEAT_EVENT:
        stream = payload.get("last_activity_stream") or "none"
        return (
            f"session {_format_session_label(payload)} heartbeat "
            f"(elapsed {format_duration_seconds(payload.get('elapsed_seconds'))}, "
            f"idle {format_duration_seconds(payload.get('idle_seconds'))}, "
            f"last activity {_format_local_datetime(payload.get('last_activity_at'))}, "
            f"stream {stream})"
        )
    if name == events.ACTION_REQUIRED_EVENT:
        message = payload.get("message") or ""
        return (
            f"action required in session {_format_session_label(payload)} "
            f"(reason {payload.get('reason', 'unknown')}, "
            f"source {payload.get('source', 'unknown')}): "
            f"{message}"
        )
    if name == events.TURN_EXECUTION_FINISHED_EVENT:
        plan_state = "readable" if payload.get("plan_readable") else "unreadable"
        return (
            f"session {_format_session_label(payload)} harness exited "
            f"(code {payload.get('returncode', 'unknown')}, "
            f"duration {format_duration_seconds(payload.get('duration_seconds'))}, "
            f"plan {plan_state})"
        )
    if name == events.TURN_HANDOFF_CORRECTION_EVENT:
        return (
            f"session {_format_session_label(payload)} handoff correction "
            f"attempt {payload.get('attempt', 'unknown')} of "
            f"{payload.get('max_attempts', 'unknown')} "
            f"({payload.get('defect', 'unknown')}): "
            f"{payload.get('detail', '')}"
        )
    if name == events.TURN_REJECTED_EVENT:
        reopened = " and reopened" if payload.get("reopened") else ""
        return (
            f"session {_format_session_label(payload)} turn rejected{reopened} "
            f"by {payload.get('guard', 'unknown')}: "
            f"{payload.get('reason', 'unknown')}"
        )
    if name == events.SESSION_REOPENED_EVENT:
        return (
            f"session {_format_session_label(payload)} returned to "
            f"{payload.get('to_status', 'unknown')} from "
            f"{payload.get('from_status', 'unknown')} "
            f"by {payload.get('guard', 'unknown')}"
        )
    if name == events.TURN_FINISHED_EVENT:
        return format_turn_finished_message(payload)
    if name == events.COMMIT_STARTED_EVENT:
        return (
            f"commit {payload.get('commit_index', 'unknown')} started "
            f"({_progress_commit_label(payload)})"
        )
    if name == events.COMMIT_FINISHED_EVENT:
        sha = payload.get("commit_sha") or "none"
        return (
            f"commit {payload.get('commit_index', 'unknown')} finished "
            f"({_progress_commit_label(payload)}, "
            f"status {payload.get('status_result', 'unknown')}, "
            f"sha {sha}, "
            f"duration {format_duration_seconds(payload.get('duration_seconds'))})"
        )
    if name in {
        events.COMMIT_GROUP_STARTED_EVENT,
        events.COMMIT_GROUP_UPDATED_EVENT,
        events.COMMIT_GROUP_FINISHED_EVENT,
    }:
        group = payload.get("group")
        if not isinstance(group, dict):
            group = {}
        partial = group.get("successful_repositories")
        shas: list[str] = []
        if isinstance(partial, list):
            for row in partial:
                if not isinstance(row, dict) or not row.get("commit_sha"):
                    continue
                alias = row.get("alias")
                label = alias if alias is not None else row.get("key")
                shas.append(f"{str(label)}={str(row.get('commit_sha'))}")
        remaining = group.get("remaining_aliases")
        remaining_values = (
            [str(alias) for alias in remaining] if isinstance(remaining, list) else []
        )
        visible_remaining, omitted_remaining = workspaces.bounded_repository_rows(
            remaining_values
        )
        visible_shas, omitted_shas = workspaces.bounded_repository_rows(shas)
        remaining_preview = ",".join(visible_remaining) or "none"
        sha_preview = ",".join(visible_shas) or "none"
        if omitted_remaining:
            remaining_preview += f" (+{omitted_remaining} more)"
        if omitted_shas:
            sha_preview += f" (+{omitted_shas} more)"
        return (
            f"commit group {payload.get('phase') or group.get('status') or 'updated'} "
            f"(scope {group.get('scope', 'unknown')}, "
            f"failed {group.get('failed_alias') or 'none'}, "
            f"remaining {remaining_preview}, "
            f"partial SHAs {sha_preview})"
        )
    if name == events.RUN_CONTROL_EVENT:
        return _run_control_message(payload)
    if name == events.MAILBOX_EVENT:
        return _mailbox_message(payload)
    if name == events.REVIEW_SESSION_CREATED_EVENT:
        session = payload.get("session_id") or payload.get("session", "unknown")
        harness = payload.get("review_harness", "unknown")
        review_path = payload.get("review_session_path", "unknown")
        return (
            f"review session {session} created (harness {harness}, file {review_path})"
        )
    if name == events.VERIFICATION_STARTED_EVENT:
        check_count = payload.get("check_count")
        plural = "" if check_count == 1 else "s"
        return (
            f"verification started for session {_format_session_label(payload)} "
            f"turn {payload.get('turn_index')} ({check_count} check{plural})"
        )
    if name == events.VERIFICATION_CHECK_STARTED_EVENT:
        check_index = payload.get("check_index")
        check_count = payload.get("check_count")
        position = (
            f"{int(check_index) + 1}/{check_count}"
            if check_index is not None and check_count is not None
            else "?/?"
        )
        repository = payload.get("repo")
        repository_label = f" repo {repository}" if repository else ""
        return (
            f"verification check {position} started: "
            f"{payload.get('name', 'unknown')} "
            f"({payload.get('kind', 'unknown')}) "
            f"session {_format_session_label(payload)} "
            f"turn {payload.get('turn_index')}{repository_label}"
        )
    if name == events.VERIFICATION_HEARTBEAT_EVENT:
        check_index = payload.get("check_index")
        check_count = payload.get("check_count")
        position = (
            f"{int(check_index) + 1}/{check_count}"
            if check_index is not None and check_count is not None
            else "?/?"
        )
        return (
            f"verification heartbeat for session {_format_session_label(payload)} "
            f"check {position} {payload.get('name', 'unknown')} "
            f"(elapsed {format_duration_seconds(payload.get('elapsed_seconds'))}, "
            f"check elapsed "
            f"{format_duration_seconds(payload.get('check_elapsed_seconds'))})"
        )
    if name == events.VERIFICATION_CHECK_FINISHED_EVENT:
        check_index = payload.get("check_index")
        check_count = payload.get("check_count")
        position = (
            f"{int(check_index) + 1}/{check_count}"
            if check_index is not None and check_count is not None
            else "?/?"
        )
        failure_reason = payload.get("failure_reason")
        suffix = f", failure {failure_reason}" if failure_reason else ""
        repository = payload.get("repo")
        repository_label = f", repo {repository}" if repository else ""
        return (
            f"verification check {position} finished: "
            f"{payload.get('name', 'unknown')} "
            f"({payload.get('kind', 'unknown')}) "
            f"status {payload.get('status', 'unknown')}, "
            f"duration {format_duration_seconds(payload.get('duration_seconds'))}"
            f"{suffix}{repository_label}"
        )
    if name == events.VERIFICATION_CHECK_SKIPPED_EVENT:
        check_index = payload.get("check_index")
        check_count = payload.get("check_count")
        position = (
            f"{int(check_index) + 1}/{check_count}"
            if check_index is not None and check_count is not None
            else "?/?"
        )
        dependencies = ", ".join(payload.get("on_failure_of") or [])
        return (
            f"verification check {position} skipped: "
            f"{payload.get('name', 'unknown')} "
            f"({payload.get('kind', 'unknown')}), "
            f"condition {payload.get('reason', 'unknown')}, "
            f"on failure of {dependencies}"
        )
    if name == events.VERIFICATION_FINISHED_EVENT:
        outcome = "passed" if payload.get("passed") else "failed"
        passed_count = payload.get("passed_count", "?")
        check_count = payload.get("check_count", "?")
        gate_count = payload.get("gate_count", check_count)
        gate_plural = "" if gate_count == 1 else "s"
        skipped_count = payload.get("skipped_count", 0)
        reason = payload.get("reason") or "unknown"
        return (
            f"verification {outcome} for session {_format_session_label(payload)} "
            f"({passed_count}/{check_count} checks passed, "
            f"{gate_count} gate{gate_plural}, {skipped_count} skipped, "
            f"reason {reason}, "
            f"duration {format_duration_seconds(payload.get('duration_seconds'))})"
        )
    if name == events.AGENT_MUX_DELIVERY_EVENT:
        status = payload.get("final_status") or "unknown"
        event_name = payload.get("agent_mux_event") or "unknown"
        source_event_id = payload.get("source_event_id") or "unknown"
        attempts = payload.get("attempt_count", "unknown")
        trace_id = payload.get("trace_id") or "none"
        error_classification = payload.get("error_classification") or "unknown"
        return (
            f"AgentMux delivery {status}: {event_name} "
            f"(source {source_event_id}, attempts {attempts}, "
            f"trace {trace_id}, error {error_classification})"
        )
    if name == events.RUN_PAUSED_EVENT:
        detail = payload.get("message")
        suffix = "" if not detail else f": {detail}"
        return (
            f"run paused: run {payload.get('run_id', 'unknown')}, "
            f"reason {payload.get('reason', 'unknown')}"
            f"{suffix}"
        )
    if name == "run_finished":
        # `paused` first: a run that ended without succeeding did not
        # necessarily pause (an abandoned one stopped).
        if payload.get("paused"):
            verb = "run paused"
        elif payload.get("ok"):
            verb = "run finished"
        else:
            verb = "run stopped"
        reason = payload.get("pause_reason") or payload.get("reason") or "unknown"
        return (
            f"{verb}: run {payload.get('run_id', 'unknown')}, "
            f"reason {reason}, "
            f"duration {format_duration_seconds(payload.get('duration_seconds'))}"
        )

    parts = [f"event {name}"]
    run_id = payload.get("run_id")
    if run_id is not None:
        parts.append(f"run {run_id}")
    session = payload.get("session_id") or payload.get("session")
    if session is not None:
        parts.append(f"session {session}")
    turn_index = payload.get("turn_index")
    if turn_index is not None:
        parts.append(f"turn {turn_index}")
    return ", ".join(str(part) for part in parts)


def _event_line(record: dict[str, Any]) -> str:
    name = str(record.get("name") or "unknown")
    payload = tail_records._payload(record)
    timestamp = _format_local_datetime(record.get("created_at"))
    return f"[{timestamp}] {_event_message(name, payload)}"
