"""Tail live agent-session-loop state, events, and harness logs."""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from . import events, plans, state, workspaces
from .formatting import (
    format_duration_seconds,
    format_turn_finished_message,
    format_turn_started_message,
)

DEFAULT_LINES = 40
POLL_INTERVAL_SECONDS = 0.1


class TailError(Exception):
    """Raised when tail arguments or runtime files are invalid."""


def _read_json_file(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise TailError(f"cannot read {path}: {error}") from error
    if not isinstance(raw, dict):
        raise TailError(f"{path} did not contain a JSON object")
    return raw


def _read_event_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []

    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise TailError(f"cannot read {path}: {error}") from error

    for line in lines:
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(raw, dict):
            records.append(raw)
    return records


def _payload(record: dict[str, Any]) -> dict[str, Any]:
    raw = record.get("payload")
    return raw if isinstance(raw, dict) else {}


def _record_run_id(record: dict[str, Any]) -> str | None:
    run_id = _payload(record).get("run_id")
    return str(run_id) if run_id is not None else None


def _state_run_id(state_payload: dict[str, Any] | None) -> str | None:
    if state_payload is None:
        return None
    run_id = state_payload.get("run_id")
    return str(run_id) if run_id is not None else None


def _latest_run_id(
    state_payload: dict[str, Any] | None,
    records: list[dict[str, Any]],
) -> str | None:
    state_run_id = _state_run_id(state_payload)
    if state_run_id is not None:
        return state_run_id

    for record in reversed(records):
        run_id = _record_run_id(record)
        if run_id is not None:
            return run_id
    return None


def _matches_run(record: dict[str, Any], run_id: str | None) -> bool:
    if run_id is None:
        return True
    record_run_id = _record_run_id(record)
    return record_run_id == run_id


def _event_records_for_run(
    records: list[dict[str, Any]],
    run_id: str | None,
) -> list[dict[str, Any]]:
    return [record for record in records if _matches_run(record, run_id)]


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _format_local_datetime(value: Any) -> str:
    parsed = _parse_datetime(value)
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
    parsed = _parse_datetime(deadline)
    if parsed is None:
        return "grace not started" if deadline is None else "remaining unknown"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
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
        verb = "run finished" if payload.get("ok") else "run paused"
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
    payload = _payload(record)
    timestamp = _format_local_datetime(record.get("created_at"))
    return f"[{timestamp}] {_event_message(name, payload)}"


def _target_key(path: Path) -> str:
    return str(path)


def _add_log_targets(
    targets: dict[str, dict[str, Any]],
    payload: dict[str, Any],
) -> None:
    for stream_name in ("stdout", "stderr"):
        path_value = payload.get(f"{stream_name}_path")
        if not isinstance(path_value, str) or not path_value:
            continue
        path = plans.canonicalize_path(path_value)
        key = _target_key(path)
        targets.setdefault(
            key,
            {
                "path": path,
                "stream": stream_name,
                "session": payload.get("session_id") or payload.get("session"),
                "session_id": payload.get("session_id"),
                "turn_index": payload.get("turn_index"),
                "commit_index": payload.get("commit_index"),
                "scope": payload.get("scope"),
            },
        )


def _active_targets(
    state_payload: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    targets: dict[str, dict[str, Any]] = {}
    if isinstance(state_payload, dict):
        active_turn = state_payload.get("active_turn")
        if isinstance(active_turn, dict):
            _add_log_targets(targets, active_turn)
    return targets


def _discover_targets(
    records: list[dict[str, Any]],
    state_payload: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    targets: dict[str, dict[str, Any]] = {}
    for record in records:
        _add_log_targets(targets, _payload(record))

    if isinstance(state_payload, dict):
        active_turn = state_payload.get("active_turn")
        if isinstance(active_turn, dict):
            _add_log_targets(targets, active_turn)
        turns = state_payload.get("turns")
        if isinstance(turns, list):
            for turn in turns:
                if isinstance(turn, dict):
                    _add_log_targets(targets, turn)

    return targets


def _log_prefix(target: dict[str, Any]) -> str:
    commit_index = target.get("commit_index")
    if commit_index is not None:
        parts = [
            f"{target['stream']} commit={commit_index}",
            f"scope={target.get('scope', 'unknown')}",
        ]
        session = target.get("session")
        if session is not None:
            parts.append(f"session={session}")
        return " ".join(str(part) for part in parts) + " | "

    return (
        f"{target['stream']} "
        f"session={target.get('session', 'unknown')} "
        f"turn={target.get('turn_index', 'unknown')} | "
    )


def _read_log_lines(target: dict[str, Any]) -> list[str]:
    path = target["path"]
    if not isinstance(path, Path) or not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    prefix = _log_prefix(target)
    return [f"{prefix}{line}" for line in text.splitlines()]


def _set_offsets_to_eof(
    targets: dict[str, dict[str, Any]],
    offsets: dict[str, int],
) -> None:
    for key, target in targets.items():
        path = target["path"]
        if not isinstance(path, Path) or not path.exists():
            offsets.setdefault(key, 0)
            continue
        try:
            offsets[key] = path.stat().st_size
        except OSError:
            offsets.setdefault(key, 0)


def _read_new_log_text(
    target: dict[str, Any],
    offsets: dict[str, int],
) -> list[str]:
    path = target["path"]
    key = _target_key(path)
    if not isinstance(path, Path) or not path.exists():
        offsets.setdefault(key, 0)
        return []

    try:
        with path.open("rb") as log_file:
            log_file.seek(offsets.get(key, 0))
            data = log_file.read()
            offsets[key] = log_file.tell()
    except OSError:
        return []

    if not data:
        return []
    text = data.decode("utf-8", "replace")
    prefix = _log_prefix(target)
    return [f"{prefix}{line}" for line in text.splitlines()]


def _int_sort_key(value: Any) -> int:
    try:
        return int(value)
    except TypeError, ValueError:
        return 0


def _session_sort_key(value: Any) -> tuple[tuple[int, ...], str]:
    try:
        parsed = plans.parse_session_id(value)
    except TypeError, ValueError:
        return ((sys.maxsize,), str(value or ""))
    return (parsed.sort_key, parsed.value)


def _print_lines(lines: list[str], out: TextIO) -> None:
    for line in lines:
        print(line, file=out, flush=True)


def _emit_recent(
    *,
    records: list[dict[str, Any]],
    state_payload: dict[str, Any] | None,
    run_id: str | None,
    lines: int,
    out: TextIO,
    targets: dict[str, dict[str, Any]],
    offsets: dict[str, int],
) -> None:
    run_records = _event_records_for_run(records, run_id)
    targets.update(_discover_targets(run_records, state_payload))
    activity = [_event_line(record) for record in run_records]
    for target in sorted(
        targets.values(),
        key=lambda value: (
            _int_sort_key(value.get("turn_index")),
            _session_sort_key(value.get("session_id") or value.get("session")),
            str(value.get("stream")),
        ),
    ):
        activity.extend(_read_log_lines(target))

    if activity:
        _print_lines(activity[-lines:], out)
    else:
        _print_lines(["No activity yet."], out)

    _set_offsets_to_eof(targets, offsets)


def _has_run_finished(records: list[dict[str, Any]], run_id: str | None) -> bool:
    for record in reversed(records):
        if record.get("name") == "run_finished" and _matches_run(record, run_id):
            return True
    return False


def _state_run_finished(state_payload: dict[str, Any] | None) -> bool:
    return bool(
        isinstance(state_payload, dict)
        and state_payload.get("run_finished_at") is not None
    )


def follow_events(path: str | Path) -> Iterator[list[dict[str, Any]]]:
    """Incrementally follow appended event records from a JSONL events file.

    A generator that yields one *batch* — a ``list`` of decoded event-record
    dicts — per iteration: each ``next()`` reads the bytes appended since the
    previous call, splits them into complete newline-terminated JSON records,
    and yields the parsed dicts (an empty list when nothing new arrived). A
    trailing partial line is buffered until its newline appears, so a record
    still being written is never parsed half-way.

    It tolerates the file not existing yet (yields ``[]`` until it appears),
    blank or unparseable "garbage" lines (skipped), non-object records
    (skipped), and truncation or rotation — a file that shrank below the read
    offset, or whose identity (device/inode) changed, is reopened from the
    start. Versionless (v1) records need no special handling: the follower
    surfaces every dict record verbatim and leaves schema interpretation to the
    caller. The generator never stops on its own; the caller drives it and
    decides when to stop.
    """
    events_path = Path(path)
    offset = 0
    buffer = b""
    identity: tuple[int, int] | None = None
    while True:
        records: list[dict[str, Any]] = []
        try:
            stat_result = events_path.stat()
        except OSError:
            yield records
            continue
        current_identity = (stat_result.st_dev, stat_result.st_ino)
        if stat_result.st_size < offset or (
            identity is not None and current_identity != identity
        ):
            # Truncation (file shrank) or rotation (new inode): reopen from the
            # start so a rewritten events file is followed rather than skipped.
            offset = 0
            buffer = b""
        identity = current_identity
        if stat_result.st_size > offset:
            try:
                with events_path.open("rb") as handle:
                    handle.seek(offset)
                    chunk = handle.read()
                    offset = handle.tell()
            except OSError:
                yield records
                continue
            buffer += chunk
            *complete, buffer = buffer.split(b"\n")
            for raw_line in complete:
                stripped = raw_line.strip()
                if not stripped:
                    continue
                try:
                    text = stripped.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                try:
                    raw = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if isinstance(raw, dict):
                    records.append(raw)
        yield records


class _TailFollowLoop:
    def __init__(
        self,
        *,
        state_path: Path,
        events_path: Path,
        run_id: str | None,
        seen_event_count: int,
        targets: dict[str, dict[str, Any]],
        offsets: dict[str, int],
        out: TextIO,
        poll_interval: float,
        stop_event: threading.Event,
        initial_finished: bool,
        include_events: bool = True,
    ) -> None:
        self._state_path = state_path
        self._events_path = events_path
        self._run_id = run_id
        self._seen_event_count = seen_event_count
        self._targets = targets
        self._offsets = offsets
        self._out = out
        self._poll_interval = poll_interval
        self._stop_event = stop_event
        self._initial_finished = initial_finished
        self._include_events = include_events

    def run(self) -> None:
        finished = self._initial_finished
        while not finished and not self._stop_event.is_set():
            finished = self._poll_once()
            if finished or self._stop_event.is_set():
                break
            self._stop_event.wait(self._poll_interval)

    def _poll_once(self) -> bool:
        state_payload = _read_json_file(self._state_path)
        current_records = _read_event_records(self._events_path)
        if self._run_id is None:
            self._run_id = _latest_run_id(state_payload, current_records)

        run_records = _event_records_for_run(current_records, self._run_id)
        self._targets.update(_discover_targets(run_records, state_payload))

        new_records = current_records[self._seen_event_count :]
        finished = False
        for record in new_records:
            if not _matches_run(record, self._run_id):
                continue
            if self._include_events:
                _print_lines([_event_line(record)], self._out)
            _add_log_targets(self._targets, _payload(record))
            if record.get("name") == "run_finished":
                finished = True
        self._seen_event_count = len(current_records)

        for target in list(self._targets.values()):
            _print_lines(_read_new_log_text(target, self._offsets), self._out)

        if finished or _state_run_finished(state_payload):
            for target in list(self._targets.values()):
                _print_lines(_read_new_log_text(target, self._offsets), self._out)
            return True
        return False


class TailFollower:
    """Cancellable in-process follower for new events and log text."""

    def __init__(
        self,
        plan_dir: str | Path,
        *,
        state_dir: str | Path | None = None,
        out: TextIO | None = None,
        poll_interval: float = POLL_INTERVAL_SECONDS,
        include_events: bool = True,
        initial_log_lines: int = 0,
        initial_log_scope: str = "all",
    ) -> None:
        if initial_log_lines < 0:
            raise TailError("initial_log_lines must be non-negative")
        if initial_log_scope not in ("all", "active"):
            raise TailError("initial_log_scope must be 'all' or 'active'")
        self._plan_dir = plan_dir
        self._state_dir = state_dir
        self._out = sys.stdout if out is None else out
        self._poll_interval = poll_interval
        self._include_events = include_events
        self._initial_log_lines = initial_log_lines
        self._initial_log_scope = initial_log_scope
        self._stop_event = threading.Event()
        self._done_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._thread_error: BaseException | None = None

    def start(self) -> None:
        """Start following in a background thread from the current EOF."""
        if self._thread is not None and self._thread.is_alive():
            raise TailError("tail follower is already running")

        follow_loop = self._build_loop_from_eof()
        self._stop_event.clear()
        self._done_event.clear()
        self._thread_error = None
        self._thread = threading.Thread(
            target=self._run_thread,
            args=(follow_loop,),
            name="agent-session-loop-tail",
            daemon=True,
        )
        self._thread.start()

    def follow(self) -> None:
        """Follow in the current thread from the current EOF until stopped."""
        if self._thread is not None and self._thread.is_alive():
            raise TailError("tail follower is already running")

        follow_loop = self._build_loop_from_eof()
        self._stop_event.clear()
        self._done_event.clear()
        self._thread_error = None
        try:
            follow_loop.run()
        finally:
            self._done_event.set()

    def stop(self) -> None:
        """Request follower shutdown."""
        self._stop_event.set()

    def join(self, timeout: float | None = None) -> bool:
        """Wait for the background follower to stop; return whether it stopped."""
        if self._thread is None:
            return True
        self._thread.join(timeout)
        stopped = not self._thread.is_alive()
        if stopped and self._thread_error is not None:
            raise self._thread_error
        return stopped

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def done(self) -> bool:
        return self._done_event.is_set()

    def _run_thread(self, follow_loop: _TailFollowLoop) -> None:
        try:
            follow_loop.run()
        except BaseException as error:
            self._thread_error = error
        finally:
            self._done_event.set()

    def _build_loop_from_eof(self) -> _TailFollowLoop:
        snapshot = plans.inspect_plan(self._plan_dir)
        plan_state = state.plan_state_dir(snapshot.plan_dir, self._state_dir)
        state_path = plan_state / events.STATE_FILE_NAME
        events_path = plan_state / events.EVENTS_FILE_NAME
        records = _read_event_records(events_path)
        state_payload = _read_json_file(state_path)
        run_id = _latest_run_id(state_payload, records)
        run_records = _event_records_for_run(records, run_id)
        targets = _discover_targets(run_records, state_payload)
        offsets: dict[str, int] = {}
        if self._initial_log_lines > 0:
            self._emit_initial_log_replay(state_payload, targets)
        _set_offsets_to_eof(targets, offsets)
        return _TailFollowLoop(
            state_path=state_path,
            events_path=events_path,
            run_id=run_id,
            seen_event_count=len(records),
            targets=targets,
            offsets=offsets,
            out=self._out,
            poll_interval=self._poll_interval,
            stop_event=self._stop_event,
            initial_finished=(
                _has_run_finished(records, run_id) or _state_run_finished(state_payload)
            ),
            include_events=self._include_events,
        )

    def _emit_initial_log_replay(
        self,
        state_payload: dict[str, Any] | None,
        targets: dict[str, dict[str, Any]],
    ) -> None:
        if self._initial_log_scope == "active":
            replay_targets = _active_targets(state_payload)
        else:
            replay_targets = targets
        if not replay_targets:
            return
        lines: list[str] = []
        for target in sorted(
            replay_targets.values(),
            key=lambda value: (
                _int_sort_key(value.get("turn_index")),
                _session_sort_key(value.get("session_id") or value.get("session")),
                str(value.get("stream")),
            ),
        ):
            lines.extend(_read_log_lines(target))
        if lines:
            _print_lines(lines[-self._initial_log_lines :], self._out)


def tail_plan(
    plan_dir: str | Path,
    *,
    state_dir: str | Path | None = None,
    lines: int = DEFAULT_LINES,
    follow: bool = True,
    out: TextIO = sys.stdout,
    poll_interval: float = POLL_INTERVAL_SECONDS,
) -> None:
    """Print recent activity and optionally follow live events/logs."""
    if lines <= 0:
        raise TailError("--lines must be a positive integer")

    snapshot = plans.inspect_plan(plan_dir)
    plan_state = state.plan_state_dir(snapshot.plan_dir, state_dir)
    state_path = plan_state / events.STATE_FILE_NAME
    events_path = plan_state / events.EVENTS_FILE_NAME
    records = _read_event_records(events_path)
    state_payload = _read_json_file(state_path)
    run_id = _latest_run_id(state_payload, records)
    targets: dict[str, dict[str, Any]] = {}
    offsets: dict[str, int] = {}

    _emit_recent(
        records=records,
        state_payload=state_payload,
        run_id=run_id,
        lines=lines,
        out=out,
        targets=targets,
        offsets=offsets,
    )

    if not follow:
        return

    stop_event = threading.Event()
    follow_loop = _TailFollowLoop(
        state_path=state_path,
        events_path=events_path,
        run_id=run_id,
        seen_event_count=len(records),
        targets=targets,
        offsets=offsets,
        out=out,
        poll_interval=poll_interval,
        stop_event=stop_event,
        initial_finished=(
            _has_run_finished(records, run_id) or _state_run_finished(state_payload)
        ),
    )
    follow_loop.run()


def tail_from_args(args: argparse.Namespace) -> int:
    """Tail plan activity from CLI arguments."""
    try:
        tail_plan(
            args.plan_dir,
            state_dir=args.state_dir,
            lines=args.lines,
            follow=not args.no_follow,
        )
    except (plans.PlanError, TailError, OSError) as error:
        print(f"agent-session-loop tail: {error}", file=sys.stderr)
        return 1
    return 0
