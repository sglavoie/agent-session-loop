"""Run-loop orchestration boundaries."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, TextIO

from . import (
    config,
    contracts,
    events,
    harnesses,
    plans,
    preflight,
    responses,
    run_control,
    state,
    tail,
    verification,
)
from .formatting import format_duration_seconds


NOT_IMPLEMENTED_EXIT = 2
HEARTBEAT_INTERVAL_SECONDS = 30.0
COMMIT_MODE_PER_SESSION = "per-session"
COMMIT_MODE_END = "end"
COMMIT_MODES = (COMMIT_MODE_PER_SESSION, COMMIT_MODE_END)
COMMIT_ARGV: tuple[str, ...] = ("spec", "commit", "--yes", "--no-verify")
APPROVAL_SIGNAL_PATTERNS: tuple[str, ...] = (
    "approval required",
    "requires approval",
    "waiting for approval",
    "requesting approval",
    "needs approval",
    "approve this",
    "allow this command",
    "permission to proceed",
)
RUN_CONTROL_EVENT_ACTIONS: dict[str, str] = {
    "request": events.RUN_CONTROL_ACTION_STOP_AFTER_CURRENT_REQUESTED,
    "cancel": events.RUN_CONTROL_ACTION_STOP_AFTER_CURRENT_CANCELED,
    "consumed": events.RUN_CONTROL_ACTION_STOP_AFTER_CURRENT_CONSUMED,
    "tail-enabled": events.RUN_CONTROL_ACTION_TAIL_ENABLED,
    "tail-disabled": events.RUN_CONTROL_ACTION_TAIL_DISABLED,
    "harness-selected": events.RUN_CONTROL_ACTION_HARNESS_SELECTED,
}
RUN_CONTROL_EVENT_MESSAGES: dict[str, str] = {
    "request": "stop-after-current requested",
    "cancel": "stop-after-current canceled",
    "consumed": "stop-after-current consumed",
    "tail-enabled": "live tail enabled",
    "tail-disabled": "live tail disabled",
    "harness-selected": "next harness selected",
}
AGENT_MUX_START_EVENT = "AgentSessionLoopStarted"
AGENT_MUX_TURN_START_EVENT = "AgentSessionLoopTurnStarted"
AGENT_MUX_FINISH_EVENT = "AgentSessionLoopFinished"
AGENT_MUX_SOURCE = "agent_session_loop"
AGENT_MUX_FINISH_SOURCE = AGENT_MUX_SOURCE
AGENT_MUX_START_REASON = "run_started"
AGENT_MUX_TURN_START_REASON = "turn_started"
AGENT_MUX_FINISH_REASON = "run_finished"
AGENT_MUX_NOTIFY_TIMEOUT_SECONDS = 1.0
AGENT_MUX_FINISH_NOTIFY_TIMEOUT_SECONDS = AGENT_MUX_NOTIFY_TIMEOUT_SECONDS
INLINE_TAIL_JOIN_TIMEOUT_SECONDS = 1.0


class RunError(Exception):
    """Raised when the run loop cannot continue safely."""


def _not_implemented(command: str) -> int:
    print(f"agent-session-loop {command}: not implemented yet", file=sys.stderr)
    return NOT_IMPLEMENTED_EXIT


def _prompt_plan_dir(plan_dir: str | Path) -> str:
    plan_dir_text = str(plans.canonicalize_path(plan_dir))
    if not plan_dir_text.endswith("/"):
        plan_dir_text += "/"
    return plan_dir_text


def build_session_prompt(harness: str, plan_dir: str | Path) -> str:
    """Build the one-turn prompt, keeping the skill invocation first."""
    adapter = harnesses.get_adapter(harness)
    return f"{adapter.skill_prefix}session-run {_prompt_plan_dir(plan_dir)}"


def build_harness_invocation(
    harness: str,
    *,
    cwd: str | Path,
    plan_dir: str | Path,
    unsafe_bypass: bool = False,
    last_message_path: str | Path | None = None,
    model: str | None = None,
    effort: str | None = None,
) -> harnesses.HarnessInvocation:
    """Build the subprocess command for one pending-session harness turn."""
    canonical_cwd = plans.canonicalize_path(cwd)
    canonical_plan_dir = plans.canonicalize_path(plan_dir)
    prompt = build_session_prompt(harness, canonical_plan_dir)
    adapter = harnesses.get_adapter(harness)
    return adapter.build_invocation(
        cwd=canonical_cwd,
        plan_dir=canonical_plan_dir,
        prompt=prompt,
        unsafe_bypass=unsafe_bypass,
        last_message_path=(
            plans.canonicalize_path(last_message_path)
            if last_message_path is not None
            else None
        ),
        model=model,
        effort=effort,
    )


def _selected_model_effort_payload(
    args: argparse.Namespace,
    *,
    harness: str | None = None,
) -> dict[str, str | None]:
    return config.model_effort_payload(
        args.harness if harness is None else harness,
        model=getattr(args, "model", None),
        effort=getattr(args, "effort", None),
    )


def _selected_future_harness(
    args: argparse.Namespace,
    control_payload: dict[str, Any],
) -> str:
    selected = control_payload.get("next_harness") or args.harness
    if selected not in harnesses.HARNESS_NAMES:
        raise RunError(f"unsupported selected harness: {selected}")
    return str(selected)


def _safe_harness_config_payload() -> dict[str, Any]:
    try:
        return config.harness_config_payload()
    except config.ConfigError as error:
        config_path = config.default_config_path()
        return {
            "path": str(config_path),
            "present": config_path.exists(),
            "configured_harnesses": [],
            "error": str(error),
        }


def _harness_preflight_failure(
    requested_harness: str,
    *,
    session_id: str,
) -> tuple[str, dict[str, Any]] | None:
    config_path = config.default_config_path()
    details: dict[str, Any] = {
        **_session_identity_payload(session_id),
        "session_file": plans.session_id_to_filename(session_id),
        "requested_harness": requested_harness,
        "config_path": str(config_path),
    }

    try:
        adapter = harnesses.get_adapter(requested_harness)
    except (config.ConfigError, harnesses.HarnessError) as error:
        details.update(
            {
                "executable": None,
                "resolved_executable": None,
                "error": str(error),
            }
        )
        return str(error), details

    details["executable"] = adapter.executable
    try:
        executable_path = adapter.resolve_executable()
    except OSError as error:
        details.update(
            {
                "resolved_executable": None,
                "error": str(error),
            }
        )
        return str(error), details

    if executable_path is None:
        message = (
            f"{requested_harness} executable not found on PATH: "
            f"{adapter.executable}"
        )
        details.update(
            {
                "resolved_executable": None,
                "error": message,
            }
        )
        return message, details

    details["resolved_executable"] = executable_path
    return None


def _utc_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{timestamp}-{os.getpid()}"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _isoformat(moment: datetime) -> str:
    return moment.isoformat()


def _duration_seconds(started_at: datetime, finished_at: datetime) -> float:
    return round(max(0.0, (finished_at - started_at).total_seconds()), 3)


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _unsafe_bypass(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "unsafe_bypass", True))


def _approval_mode(args: argparse.Namespace) -> str:
    return "unsafe-bypass" if _unsafe_bypass(args) else "harness-default"


def _commit_mode(args: argparse.Namespace) -> str | None:
    value = getattr(args, "commit_mode", None)
    if value is None or value in COMMIT_MODES:
        return value
    raise RunError("commit_mode must be None, 'per-session', or 'end'")


def _commit_mode_payload(args: argparse.Namespace) -> str | None:
    value = getattr(args, "commit_mode", None)
    return value if value in COMMIT_MODES else None


def _session_number_alias(session_id: str) -> int | None:
    parsed = plans.parse_session_id(session_id)
    return parsed.as_int() if parsed.is_integer else None


def _session_compat_value(session_id: str) -> int | str:
    number = _session_number_alias(session_id)
    return number if number is not None else session_id


def _session_label(session_id: str) -> str:
    return f"session {session_id}"


def _session_path_label(session_id: str) -> str:
    return f"session-{plans.parse_session_id(session_id).value}"


def _session_identity_payload(session_id: str) -> dict[str, Any]:
    payload: dict[str, Any] = {"session": _session_compat_value(session_id)}
    payload["session_id"] = session_id
    number = _session_number_alias(session_id)
    if number is not None:
        payload["session_number"] = number
    return payload


def _session_details_payload(session_id: str) -> dict[str, Any]:
    number = _session_number_alias(session_id)
    if number is not None:
        return {"session": number}
    return {"session": session_id, "session_id": session_id}


def _session_status_change_payload(
    session_id: str,
    *,
    before: plans.StatusValue | None,
    after: plans.StatusValue | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "before": before,
        "after": after,
    }
    number = _session_number_alias(session_id)
    if number is None:
        payload["id"] = session_id
    else:
        payload["number"] = number
    return payload


def _status_map(snapshot: plans.PlanSnapshot) -> dict[str, plans.StatusValue]:
    return snapshot.status_by_id


def _status_counts(snapshot: plans.PlanSnapshot) -> dict[str, int]:
    counts = {"pending": 0, "complete": 0, "empty": 0, "unknown": 0}
    for session in snapshot.sessions:
        counts[session.status] += 1
    return counts


def _sessions_with_status(
    snapshot: plans.PlanSnapshot,
    status: plans.StatusValue,
) -> list[dict[str, Any]]:
    return [
        _session_payload(session)
        for session in snapshot.sessions
        if session.status == status
    ]


def _invalid_status_pause(
    snapshot: plans.PlanSnapshot,
) -> tuple[str, str, dict[str, Any]] | None:
    unknown_sessions = _sessions_with_status(snapshot, "unknown")
    if unknown_sessions:
        return (
            "unknown-status",
            "one or more session handoff statuses are not pending or complete",
            {"sessions": unknown_sessions},
        )

    empty_sessions = _sessions_with_status(snapshot, "empty")
    if empty_sessions:
        return (
            "malformed-plan",
            "one or more session files are missing a populated handoff status",
            {"sessions": empty_sessions},
        )

    return None


def _session_payload(session: plans.SessionStatus) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": session.id,
        "status": session.status,
        "path": str(session.path),
        "line_number": session.line_number,
        "raw_value": session.raw_value,
    }
    number = _session_number_alias(session.id)
    if number is not None:
        payload["number"] = number
    return payload


def _snapshot_payload(snapshot: plans.PlanSnapshot) -> dict[str, Any]:
    next_pending = snapshot.next_pending
    next_pending_id = next_pending.id if next_pending is not None else None
    return {
        "plan_dir": str(snapshot.plan_dir),
        "plan_hash": snapshot.plan_hash,
        "content_sha256": snapshot.content_sha256,
        "path_hash": snapshot.path_hash,
        "counts": _status_counts(snapshot),
        "next_pending": (
            _session_compat_value(next_pending_id)
            if next_pending_id is not None
            else None
        ),
        "next_pending_id": next_pending_id,
        "sessions": [_session_payload(session) for session in snapshot.sessions],
    }


def _status_changes(
    before: plans.PlanSnapshot,
    after: plans.PlanSnapshot,
) -> tuple[dict[str, Any], ...]:
    before_statuses = _status_map(before)
    after_statuses = _status_map(after)
    changes: list[dict[str, Any]] = []
    for session_id in sorted(
        set(before_statuses) | set(after_statuses),
        key=lambda value: plans.parse_session_id(value).sort_key,
    ):
        before_value = before_statuses.get(session_id)
        after_value = after_statuses.get(session_id)
        if before_value != after_value:
            changes.append(
                _session_status_change_payload(
                    session_id,
                    before=before_value,
                    after=after_value,
                )
            )
    return tuple(changes)


def _pending_completed(
    before: plans.PlanSnapshot,
    after: plans.PlanSnapshot,
) -> tuple[str, ...]:
    before_statuses = _status_map(before)
    after_statuses = _status_map(after)
    return tuple(
        session_id
        for session_id in sorted(
            before_statuses,
            key=lambda value: plans.parse_session_id(value).sort_key,
        )
        if before_statuses[session_id] == "pending"
        and after_statuses.get(session_id) == "complete"
    )


def _extract_prefixed_line(text: str, prefix: str) -> str | None:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(prefix):
            return stripped[len(prefix) :].strip()
    return None


def _runner_signals(result: harnesses.HarnessResult) -> dict[str, Any]:
    signals: dict[str, Any] = {}
    for source, text in (
        ("last_message", result.last_message),
        ("stdout", result.stdout),
        ("stderr", result.stderr),
    ):
        if "runner_status" not in signals:
            status = _extract_prefixed_line(text, events.RUNNER_STATUS_PREFIX)
            if status is not None:
                signals["runner_status"] = status
                signals["runner_status_source"] = source

        if "runner_pause" not in signals:
            pause = _extract_prefixed_line(text, events.RUNNER_PAUSE_PREFIX)
            if pause is not None:
                signals["runner_pause"] = pause
                signals["runner_pause_source"] = source

    runner_status = str(signals.get("runner_status", "")).strip().lower()
    if "runner_pause" not in signals and runner_status in {"pause", "paused"}:
        signals["runner_pause"] = "Runner status reported paused"
        signals["runner_pause_source"] = signals.get("runner_status_source")

    return signals


def _preview_text(text: str, *, limit: int = 160) -> str:
    collapsed = " ".join(text.strip().split())
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[: limit - 3]}..."


def _live_action_signal(
    stream_name: harnesses.StreamName,
    text: str,
) -> dict[str, Any] | None:
    pause = _extract_prefixed_line(text, events.RUNNER_PAUSE_PREFIX)
    if pause is not None:
        return {
            "reason": "runner-pause",
            "message": pause,
            "source": stream_name,
            "matched": events.RUNNER_PAUSE_PREFIX,
        }

    status = _extract_prefixed_line(text, events.RUNNER_STATUS_PREFIX)
    if status is not None and status.strip().lower() in {"pause", "paused"}:
        return {
            "reason": "runner-status-paused",
            "message": status,
            "source": stream_name,
            "matched": events.RUNNER_STATUS_PREFIX,
        }

    lowered = text.lower()
    for pattern in APPROVAL_SIGNAL_PATTERNS:
        if pattern in lowered:
            return {
                "reason": "approval-required",
                "message": _preview_text(text),
                "source": stream_name,
                "matched": pattern,
            }

    return None


def _all_sessions_complete(snapshot: plans.PlanSnapshot) -> bool:
    return all(session.status == "complete" for session in snapshot.sessions)


def _validate_run_inputs(
    args: argparse.Namespace,
) -> tuple[Path, int | None, int | None, str | None]:
    cwd = plans.canonicalize_path(args.cwd)
    if not cwd.is_dir():
        raise plans.PlanError(f"cwd does not exist or is not a directory: {cwd}")

    commit_mode = _commit_mode(args)

    timeout = args.timeout
    if timeout is not None and timeout <= 0:
        raise RunError("--timeout must be a positive integer")

    max_sessions = args.max_sessions
    if max_sessions is not None and max_sessions <= 0:
        raise RunError("--max-sessions must be a positive integer")

    return cwd, timeout, max_sessions, commit_mode


def _run_git(
    cwd: Path,
    args: list[str],
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except FileNotFoundError as error:
        raise RunError("git executable not found on PATH") from error
    except OSError as error:
        raise RunError(f"could not run git {' '.join(args)}: {error}") from error


def _read_git_head(cwd: Path) -> dict[str, Any]:
    result = _run_git(cwd, ["rev-parse", "--verify", "HEAD"])
    head = result.stdout.strip() if result.returncode == 0 else None
    return {
        "head": head,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def _validate_git_worktree(cwd: Path) -> None:
    result = _run_git(cwd, ["rev-parse", "--is-inside-work-tree"])
    if result.returncode != 0 or result.stdout.strip() != "true":
        detail = result.stderr.strip() or result.stdout.strip()
        if detail:
            raise RunError(f"cwd is not inside a git repository: {detail}")
        raise RunError("cwd is not inside a git repository")


def _validate_clean_git_status(cwd: Path) -> None:
    result = _run_git(cwd, ["status", "--porcelain"])
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        if detail:
            raise RunError(f"could not inspect git status: {detail}")
        raise RunError("could not inspect git status")
    if result.stdout:
        raise RunError("git status --porcelain must be empty before auto-commit")


def _validate_spec_available() -> str:
    spec_path = shutil.which("spec")
    if spec_path is None:
        raise RunError("spec executable not found on PATH")
    return spec_path


def _validate_commit_preflight(cwd: Path) -> dict[str, Any]:
    _validate_git_worktree(cwd)
    spec_path = _validate_spec_available()
    _validate_clean_git_status(cwd)
    return {"spec_path": spec_path}


def _validate_commit_at_end_scope(
    *,
    snapshot: plans.PlanSnapshot,
    max_sessions: int | None,
) -> None:
    if max_sessions is None:
        return
    pending_count = _status_counts(snapshot)["pending"]
    if max_sessions < pending_count:
        raise RunError(
            "--commit-at-end cannot be combined with --max-sessions when "
            "not all currently pending sessions can complete in one invocation"
        )


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _append_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)


def _prepare_stream_logs(paths: dict[str, Path]) -> harnesses.StreamCallback:
    _write_text(paths["stdout"], "")
    _write_text(paths["stderr"], "")

    def stream_callback(stream_name: harnesses.StreamName, text: str) -> None:
        _append_text(paths[stream_name], text)

    return stream_callback


def _turn_paths(
    run_dir: Path, *, turn_index: int, session_id: str
) -> dict[str, Path]:
    artifact_dir = run_dir / f"turn-{turn_index:04d}-{_session_path_label(session_id)}"
    return {
        "artifact_dir": artifact_dir,
        "stdout": artifact_dir / "stdout.log",
        "stderr": artifact_dir / "stderr.log",
        "last_message": artifact_dir / "last-message.txt",
        "result": artifact_dir / "result.json",
    }


def _commit_paths(
    run_dir: Path,
    *,
    commit_index: int,
    session_id: str | None,
) -> dict[str, Path]:
    if commit_index <= 0:
        raise RunError("commit_index must be a positive integer")
    if session_id is not None:
        session_id = plans.parse_session_id(session_id).value
    suffix = _session_path_label(session_id) if session_id is not None else "end"
    artifact_dir = run_dir / f"commit-{commit_index:04d}-{suffix}"
    return {
        "artifact_dir": artifact_dir,
        "stdout": artifact_dir / "stdout.log",
        "stderr": artifact_dir / "stderr.log",
        "result": artifact_dir / "result.json",
    }


def _build_invocation_payload(
    invocation: harnesses.HarnessInvocation,
    *,
    model_effort: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    if model_effort is None:
        model_effort = config.model_effort_payload(
            invocation.name,
            model=invocation.model,
            effort=invocation.effort,
        )
    return {
        "name": invocation.name,
        "argv": invocation.argv,
        "cwd": str(invocation.cwd),
        "last_message_path": (
            str(invocation.last_message_path)
            if invocation.last_message_path is not None
            else None
        ),
        **model_effort,
    }


def _turn_status_result(
    *,
    result: harnesses.HarnessResult,
    status_changes: tuple[dict[str, Any], ...],
    pending_completed: tuple[str, ...],
    session_id: str,
    runner_signals: dict[str, Any],
    plan_error: str | None,
) -> str:
    if result.timed_out:
        return "timeout"
    if plan_error is not None:
        return "malformed-plan"
    if runner_signals.get("runner_pause") is not None:
        return "runner-pause"
    if result.returncode != 0:
        return "nonzero-exit"
    if not status_changes:
        return "no-status-change"
    if len(status_changes) > 1:
        return "multiple-status-changes"
    expected_change = _session_status_change_payload(
        session_id,
        before="pending",
        after="complete",
    )
    if status_changes != (expected_change,):
        return "unexpected-status-change"
    if pending_completed == (session_id,):
        return "complete"
    return "unknown"


def _write_turn_artifacts(
    *,
    paths: dict[str, Path],
    result: harnesses.HarnessResult,
    before: plans.PlanSnapshot,
    after: plans.PlanSnapshot | None,
    run_id: str,
    turn_index: int,
    session_id: str,
    live_turn: dict[str, Any],
    plan_error: str | None = None,
) -> dict[str, Any]:
    _write_text(paths["stdout"], result.stdout)
    _write_text(paths["stderr"], result.stderr)
    _write_text(paths["last_message"], result.last_message)

    status_changes = _status_changes(before, after) if after is not None else ()
    pending_completed = _pending_completed(before, after) if after is not None else ()
    runner_signals = _runner_signals(result)
    status_result = _turn_status_result(
        result=result,
        status_changes=status_changes,
        pending_completed=pending_completed,
        session_id=session_id,
        runner_signals=runner_signals,
        plan_error=plan_error,
    )
    payload = {
        "run_id": run_id,
        "turn_index": turn_index,
        **_session_identity_payload(session_id),
        "total_sessions": live_turn.get("total_sessions"),
        "artifact_dir": str(paths["artifact_dir"]),
        "harness": result.name,
        "model": live_turn.get("model"),
        "model_source": live_turn.get("model_source"),
        "effort": live_turn.get("effort"),
        "effort_source": live_turn.get("effort_source"),
        "argv": result.argv,
        "returncode": result.returncode,
        "timed_out": result.timed_out,
        "stdout_path": str(paths["stdout"]),
        "stderr_path": str(paths["stderr"]),
        "last_message_path": str(paths["last_message"]),
        "result_path": str(paths["result"]),
        "status_changes": list(status_changes),
        "pending_completed": [
            _session_compat_value(completed_id) for completed_id in pending_completed
        ],
        "pending_completed_ids": list(pending_completed),
        "status_result": status_result,
        "started_at": live_turn["started_at"],
        "finished_at": live_turn["finished_at"],
        "duration_seconds": live_turn["duration_seconds"],
        "idle_seconds_since_previous_turn": live_turn[
            "idle_seconds_since_previous_turn"
        ],
        "last_activity_at": live_turn.get("last_activity_at"),
        "last_activity_stream": live_turn.get("last_activity_stream"),
        "last_activity_preview": live_turn.get("last_activity_preview"),
        "last_heartbeat_at": live_turn.get("last_heartbeat_at"),
        "heartbeat_count": live_turn.get("heartbeat_count", 0),
        "before": _snapshot_payload(before),
        "after": _snapshot_payload(after) if after is not None else None,
    }
    if live_turn.get("action_required") is not None:
        payload["action_required"] = live_turn["action_required"]
    if plan_error is not None:
        payload["plan_error"] = plan_error
    payload.update(runner_signals)
    _write_json_file(paths["result"], payload)
    return payload


def _write_json_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    temporary_path.write_text(
        json.dumps(payload, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary_path, path)


def _commit_status_result(
    *,
    returncode: int,
    head_before: str | None,
    head_after: str | None,
) -> str:
    if returncode != 0:
        return "nonzero-exit"
    if head_after is None:
        return "missing-head"
    if head_before == head_after:
        return "unchanged-head"
    return "success"


def _write_commit_artifacts(
    *,
    paths: dict[str, Path],
    stdout: str,
    stderr: str,
    payload: dict[str, Any],
) -> None:
    _write_text(paths["stdout"], stdout)
    _write_text(paths["stderr"], stderr)
    _write_json_file(paths["result"], payload)


def _run_spec_commit(
    *,
    cwd: Path,
    run_dir: Path,
    commit_index: int,
    session_id: str | None,
) -> dict[str, Any]:
    canonical_cwd = plans.canonicalize_path(cwd)
    paths = _commit_paths(
        run_dir,
        commit_index=commit_index,
        session_id=session_id,
    )
    argv = list(COMMIT_ARGV)
    head_before_payload = _read_git_head(canonical_cwd)
    started_at = _utc_now()
    exec_error = None
    try:
        result = subprocess.run(
            argv,
            cwd=canonical_cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except FileNotFoundError as error:
        exec_error = str(error)
        result = subprocess.CompletedProcess(
            argv,
            127,
            stdout="",
            stderr=exec_error,
        )
    except OSError as error:
        exec_error = str(error)
        result = subprocess.CompletedProcess(
            argv,
            1,
            stdout="",
            stderr=exec_error,
        )
    finished_at = _utc_now()
    head_after_payload = _read_git_head(canonical_cwd)
    head_before = head_before_payload["head"]
    head_after = head_after_payload["head"]
    status_result = _commit_status_result(
        returncode=result.returncode,
        head_before=head_before,
        head_after=head_after,
    )
    session_identity = (
        _session_identity_payload(session_id)
        if session_id is not None
        else {"session": None, "session_id": None}
    )
    payload = {
        "commit_index": commit_index,
        "scope": "session" if session_id is not None else "end",
        **session_identity,
        "artifact_dir": str(paths["artifact_dir"]),
        "stdout_path": str(paths["stdout"]),
        "stderr_path": str(paths["stderr"]),
        "result_path": str(paths["result"]),
        "argv": argv,
        "cwd": str(canonical_cwd),
        "returncode": result.returncode,
        "success": status_result == "success",
        "status_result": status_result,
        "head_before": head_before,
        "head_after": head_after,
        "head_before_result": head_before_payload,
        "head_after_result": head_after_payload,
        "commit_sha": head_after if status_result == "success" else None,
        "started_at": _isoformat(started_at),
        "finished_at": _isoformat(finished_at),
        "duration_seconds": _duration_seconds(started_at, finished_at),
        "stdout_chars": len(result.stdout),
        "stderr_chars": len(result.stderr),
    }
    if exec_error is not None:
        payload["exec_error"] = exec_error
    _write_commit_artifacts(
        paths=paths,
        stdout=result.stdout,
        stderr=result.stderr,
        payload=payload,
    )
    return payload


def _commit_started_payload(
    *,
    run_id: str,
    cwd: Path,
    run_dir: Path,
    commit_index: int,
    session_id: str | None,
) -> dict[str, Any]:
    paths = _commit_paths(
        run_dir,
        commit_index=commit_index,
        session_id=session_id,
    )
    session_identity = (
        _session_identity_payload(session_id)
        if session_id is not None
        else {"session": None, "session_id": None}
    )
    return {
        "run_id": run_id,
        "commit_index": commit_index,
        "scope": "session" if session_id is not None else "end",
        **session_identity,
        "artifact_dir": str(paths["artifact_dir"]),
        "stdout_path": str(paths["stdout"]),
        "stderr_path": str(paths["stderr"]),
        "result_path": str(paths["result"]),
        "argv": list(COMMIT_ARGV),
        "cwd": str(cwd),
    }


def _write_state_payload(payload: dict[str, Any]) -> None:
    state_path = payload.get("state_path")
    if not isinstance(state_path, str) or not state_path:
        return
    _write_json_file(Path(state_path), payload)


def _loop_harness(
    payload: dict[str, Any],
    active_turn: dict[str, Any] | None = None,
) -> str | None:
    if active_turn is not None:
        turn_harness = active_turn.get("harness")
        if isinstance(turn_harness, str) and turn_harness.strip():
            return turn_harness
    turns = payload.get("turns")
    if isinstance(turns, list):
        for turn in reversed(turns):
            if not isinstance(turn, dict):
                continue
            turn_harness = turn.get("harness")
            if isinstance(turn_harness, str) and turn_harness.strip():
                return turn_harness
    payload_harness = payload.get("harness")
    if isinstance(payload_harness, str) and payload_harness.strip():
        return payload_harness
    return None


def _agent_mux_loop_payload(
    payload: dict[str, Any],
    *,
    reason: str,
    active_turn: dict[str, Any] | None = None,
) -> dict[str, Any]:
    hook_payload: dict[str, Any] = {
        "source": AGENT_MUX_SOURCE,
        "reason": reason,
        "run_id": payload.get("run_id"),
        "plan_dir": payload.get("plan_dir"),
        "cwd": payload.get("cwd"),
        "state_path": payload.get("state_path"),
        "harness": _loop_harness(payload, active_turn),
    }
    if payload.get("run_started_at") is not None:
        hook_payload["run_started_at"] = payload.get("run_started_at")
    if payload.get("run_finished_at") is not None:
        hook_payload["run_finished_at"] = payload.get("run_finished_at")
    if active_turn is not None:
        for key in (
            "turn_index",
            "session",
            "session_id",
            "session_number",
            "total_sessions",
            "artifact_dir",
            "stdout_path",
            "stderr_path",
            "last_message_path",
            "result_path",
            "started_at",
        ):
            if active_turn.get(key) is not None:
                hook_payload[key] = active_turn.get(key)
    return hook_payload


def _notify_agent_mux_loop(
    event_name: str,
    payload: dict[str, Any],
    *,
    reason: str,
    active_turn: dict[str, Any] | None = None,
) -> None:
    amx_path = shutil.which("amx")
    if amx_path is None:
        return

    hook_payload = _agent_mux_loop_payload(
        payload,
        reason=reason,
        active_turn=active_turn,
    )
    try:
        subprocess.run(
            [amx_path, "hook-indicator", event_name],
            input=json.dumps(hook_payload, separators=(",", ":")),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=AGENT_MUX_NOTIFY_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return


def _notify_agent_mux_loop_started(payload: dict[str, Any]) -> None:
    _notify_agent_mux_loop(
        AGENT_MUX_START_EVENT,
        payload,
        reason=AGENT_MUX_START_REASON,
    )


def _notify_agent_mux_loop_turn_started(
    payload: dict[str, Any],
    active_turn: dict[str, Any],
) -> None:
    _notify_agent_mux_loop(
        AGENT_MUX_TURN_START_EVENT,
        payload,
        reason=AGENT_MUX_TURN_START_REASON,
        active_turn=active_turn,
    )


def _notify_agent_mux_loop_finished(payload: dict[str, Any]) -> None:
    _notify_agent_mux_loop(
        AGENT_MUX_FINISH_EVENT,
        payload,
        reason=AGENT_MUX_FINISH_REASON,
    )


def _append_event(event_path: Path, name: str, payload: dict[str, Any]) -> None:
    events.append_event(event_path, events.Event(name=name, payload=payload))


class _RunPersistence:
    """Serializes one run's mutable payload, state writes, and event appends."""

    def __init__(self) -> None:
        self.lock = threading.RLock()

    def append_event(
        self,
        event_path: Path,
        name: str,
        payload: dict[str, Any],
    ) -> None:
        with self.lock:
            _append_event(event_path, name, payload)

    def write_state(self, payload: dict[str, Any]) -> None:
        with self.lock:
            _write_state_payload(payload)


def _run_persistence(
    persistence: _RunPersistence | None,
) -> _RunPersistence:
    return _RunPersistence() if persistence is None else persistence


def _initial_control_payload() -> dict[str, Any]:
    return {
        "stop_after_current": False,
        "tail_enabled": False,
        "next_harness": None,
        "consumed": False,
        "last_action": None,
    }


def _apply_run_control_action(
    control_payload: dict[str, Any],
    action: run_control.RunControlAction,
) -> str:
    event_action = RUN_CONTROL_EVENT_ACTIONS[action.action]
    control_payload["stop_after_current"] = action.stop_after_current
    control_payload["tail_enabled"] = action.tail_enabled
    control_payload["next_harness"] = action.next_harness
    if action.action == "consumed":
        control_payload["consumed"] = True
    control_payload["last_action"] = event_action
    return event_action


def _record_run_control_action(
    *,
    event_path: Path,
    payload: dict[str, Any],
    control_payload: dict[str, Any],
    action: run_control.RunControlAction,
    context: dict[str, Any] | None = None,
    persistence: _RunPersistence | None = None,
) -> None:
    persistence = _run_persistence(persistence)
    with persistence.lock:
        previous_harness = control_payload.get("next_harness") or payload.get("harness")
        event_action = _apply_run_control_action(control_payload, action)
        payload["control"] = dict(control_payload)
        event_payload = {
            "run_id": payload.get("run_id"),
            "action": event_action,
            "stop_after_current": action.stop_after_current,
            "tail_enabled": action.tail_enabled,
            "next_harness": action.next_harness,
            "message": RUN_CONTROL_EVENT_MESSAGES[action.action],
        }
        if context is not None:
            for key in ("session", "turn_index", "total_sessions"):
                value = context.get(key)
                if value is not None:
                    event_payload[key] = value
        if action.action == "harness-selected":
            current_harness = (
                context.get("harness")
                if context is not None and context.get("harness") is not None
                else previous_harness
            )
            event_payload["current_harness"] = current_harness
            event_payload["selected_harness"] = action.next_harness
        persistence.append_event(event_path, events.RUN_CONTROL_EVENT, event_payload)
        persistence.write_state(payload)


def _local_timestamp(moment: datetime | None = None) -> str:
    value = datetime.now().astimezone() if moment is None else moment.astimezone()
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _format_local_datetime(value: Any) -> str:
    parsed = _parse_datetime(value)
    if parsed is None:
        return "none"
    return _local_timestamp(parsed)


def _format_session_label(payload: dict[str, Any]) -> str:
    session = payload.get("session_id") or payload.get("session")
    total = payload.get("total_sessions")
    if total is None:
        return str(session)
    return f"{session}/{total}"


def _progress_commit_label(commit: dict[str, Any]) -> str:
    scope = commit.get("scope")
    session = commit.get("session_id") or commit.get("session")
    if scope == "session" and session is not None:
        return f"session {session}"
    if scope == "end":
        return "end"
    return str(scope or "unknown")


class _CoordinatedStderrStream:
    """Line-buffering stream proxy that shares the runner output lock."""

    def __init__(self, coordinator: "_StderrOutputCoordinator") -> None:
        self._coordinator = coordinator

    def write(self, text: str) -> int:
        return self._coordinator.write_unprefixed(text)

    def flush(self) -> None:
        self._coordinator.flush_thread()

    def isatty(self) -> bool:
        return self._coordinator.isatty()

    def fileno(self) -> int:
        return self._coordinator.fileno()


class _StderrOutputCoordinator:
    """Shared stderr output path for progress, controls, status, and tails."""

    def __init__(
        self,
        *,
        enabled: bool,
        stream: TextIO | None = None,
    ) -> None:
        self.enabled = enabled
        self._stream = stream
        self._lock = threading.Lock()
        self._buffer_lock = threading.Lock()
        self._thread_buffers: dict[int, str] = {}
        self._stream_proxy = _CoordinatedStderrStream(self)

    @property
    def stream(self) -> _CoordinatedStderrStream:
        return self._stream_proxy

    def _target_stream(self) -> TextIO:
        return sys.stderr if self._stream is None else self._stream

    def isatty(self) -> bool:
        isatty = getattr(self._target_stream(), "isatty", None)
        if isatty is None:
            return False
        try:
            return bool(isatty())
        except (OSError, ValueError):
            return False

    def fileno(self) -> int:
        return self._target_stream().fileno()

    def emit_line(self, message: str) -> None:
        if not self.enabled:
            return
        with self._lock:
            print(
                f"[{_local_timestamp()}] {message}",
                file=self._target_stream(),
                flush=True,
            )

    def emit_lines(self, messages: list[str]) -> None:
        if not self.enabled:
            return
        with self._lock:
            for message in messages:
                print(
                    f"[{_local_timestamp()}] {message}",
                    file=self._target_stream(),
                    flush=True,
                )

    def write_unprefixed(self, text: str) -> int:
        if not self.enabled:
            return len(text)

        thread_id = threading.get_ident()
        complete_lines: list[str] = []
        with self._buffer_lock:
            buffered = self._thread_buffers.get(thread_id, "") + text
            while "\n" in buffered:
                line, buffered = buffered.split("\n", 1)
                complete_lines.append(line + "\n")
            if buffered:
                self._thread_buffers[thread_id] = buffered
            else:
                self._thread_buffers.pop(thread_id, None)

        if complete_lines:
            with self._lock:
                stream = self._target_stream()
                for line in complete_lines:
                    stream.write(line)
                stream.flush()
        return len(text)

    def flush_thread(self) -> None:
        if not self.enabled:
            return

        thread_id = threading.get_ident()
        with self._buffer_lock:
            buffered = self._thread_buffers.pop(thread_id, "")
        with self._lock:
            stream = self._target_stream()
            if buffered:
                stream.write(buffered)
            stream.flush()


class _ProgressReporter:
    """Human-facing progress output for non-JSON runs."""

    def __init__(
        self,
        *,
        enabled: bool,
        output: _StderrOutputCoordinator | None = None,
    ) -> None:
        self.enabled = enabled
        self.output = (
            _StderrOutputCoordinator(enabled=enabled) if output is None else output
        )

    def _emit(self, message: str) -> None:
        self.output.emit_line(message)

    def status_snapshot(self, lines: list[str]) -> None:
        self.output.emit_lines(lines)

    def run_started(self, payload: dict[str, Any]) -> None:
        snapshot = payload["initial_snapshot"]
        counts = snapshot["counts"]
        total = sum(counts.values())
        next_pending = snapshot["next_pending"]
        next_label = "none" if next_pending is None else f"session {next_pending}"
        self._emit(
            "run started: "
            f"harness {payload['harness']}; "
            f"run {payload['run_id']}; "
            f"{counts['complete']}/{total} sessions complete; "
            f"{counts['pending']} pending; "
            f"next {next_label}; "
            f"approval {payload['approval_mode']}; "
            f"run dir {payload['run_dir']}; "
            f"state {payload['state_path']}; "
            f"events {payload['events_path']}"
        )

    def turn_started(self, active_turn: dict[str, Any]) -> None:
        self._emit(f"session {_format_session_label(active_turn)} started")

    def heartbeat(self, heartbeat: dict[str, Any]) -> None:
        stream = heartbeat.get("last_activity_stream") or "none"
        self._emit(
            f"session {_format_session_label(heartbeat)} heartbeat "
            f"(elapsed {format_duration_seconds(heartbeat['elapsed_seconds'])}, "
            f"idle {format_duration_seconds(heartbeat['idle_seconds'])}, "
            f"last activity {_format_local_datetime(heartbeat.get('last_activity_at'))}, "
            f"stream {stream})"
        )

    def action_required(self, signal: dict[str, Any]) -> None:
        self._emit(
            f"action required in session {_format_session_label(signal)} "
            f"(reason {signal['reason']}, "
            f"source {signal['source']}): "
            f"{signal['message']}"
        )

    def turn_finished(self, turn: dict[str, Any]) -> None:
        self._emit(
            f"session {_format_session_label(turn)} finished "
            f"(status {turn['status_result']}, "
            f"return code {turn['returncode']}, "
            f"duration {format_duration_seconds(turn['duration_seconds'])})"
        )

    def commit_started(self, commit: dict[str, Any]) -> None:
        self._emit(
            f"commit {commit['commit_index']} started "
            f"({_progress_commit_label(commit)})"
        )

    def commit_finished(self, commit: dict[str, Any]) -> None:
        sha = commit.get("commit_sha") or "none"
        self._emit(
            f"commit {commit['commit_index']} finished "
            f"({_progress_commit_label(commit)}, "
            f"status {commit['status_result']}, "
            f"sha {sha}, "
            f"duration {format_duration_seconds(commit.get('duration_seconds'))})"
        )

    def run_finished(self, payload: dict[str, Any]) -> None:
        verb = "run finished" if payload["ok"] else "run paused"
        reason = payload.get("pause_reason") or payload.get("reason") or "unknown"
        self._emit(
            f"{verb}: "
            f"run {payload['run_id']}, "
            f"reason {reason}, "
            f"duration {format_duration_seconds(payload['duration_seconds'])}"
        )


class _InlineTailController:
    """Owns the optional in-process tail follower for a live run."""

    def __init__(
        self,
        *,
        plan_dir: Path,
        state_dir: str | Path | None,
        output: _StderrOutputCoordinator,
    ) -> None:
        self._plan_dir = plan_dir
        self._state_dir = state_dir
        self._output = output
        self._lock = threading.Lock()
        self._follower: tail.TailFollower | None = None

    def enable(self) -> None:
        with self._lock:
            if self._follower is not None and self._follower.running:
                return
            self._follower = tail.TailFollower(
                self._plan_dir,
                state_dir=self._state_dir,
                out=self._output.stream,
            )
            self._follower.start()

    def disable(self) -> None:
        self._stop_current()

    def stop(self) -> None:
        self._stop_current()

    def _stop_current(self) -> None:
        with self._lock:
            follower = self._follower
            self._follower = None

        if follower is None:
            return
        follower.stop()
        try:
            stopped = follower.join(timeout=INLINE_TAIL_JOIN_TIMEOUT_SECONDS)
        except Exception as error:
            self._output.emit_line(f"live tail stopped with error: {error}")
            return
        if not stopped:
            self._output.emit_line("live tail did not stop before cleanup timeout")


def _compact_value(value: Any, *, fallback: str = "none") -> str:
    if value is None or value == "":
        return fallback
    return str(value)


def _compact_status_counts(
    snapshot: plans.PlanSnapshot | None,
    fallback: dict[str, Any] | None,
) -> dict[str, int] | None:
    if snapshot is not None:
        return _status_counts(snapshot)
    if isinstance(fallback, dict):
        counts = fallback.get("counts")
        if isinstance(counts, dict):
            return {
                "pending": int(counts.get("pending", 0)),
                "complete": int(counts.get("complete", 0)),
                "empty": int(counts.get("empty", 0)),
                "unknown": int(counts.get("unknown", 0)),
            }
    return None


def _compact_next_pending_id(
    snapshot: plans.PlanSnapshot | None,
    fallback: dict[str, Any] | None,
) -> str | None:
    if snapshot is not None and snapshot.next_pending is not None:
        return snapshot.next_pending.id
    if isinstance(fallback, dict):
        value = fallback.get("next_pending_id")
        if value is None:
            value = fallback.get("next_pending")
        if value is not None:
            return str(value)
    return None


def _compact_artifact_source(
    payload: dict[str, Any],
    active_turn: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if active_turn is not None:
        return active_turn
    turns = payload.get("turns")
    if isinstance(turns, list):
        for turn in reversed(turns):
            if isinstance(turn, dict):
                return turn
    return None


def _compact_status_lines(
    *,
    payload: dict[str, Any],
    control: dict[str, Any],
    snapshot: plans.PlanSnapshot | None,
    plan_error: str | None = None,
) -> list[str]:
    """Build a short live status snapshot without mutating runner state."""

    active_turn = payload.get("active_turn")
    if not isinstance(active_turn, dict):
        active_turn = None

    fallback_snapshot = payload.get("final_snapshot") or payload.get(
        "initial_snapshot"
    )
    if not isinstance(fallback_snapshot, dict):
        fallback_snapshot = None
    counts = _compact_status_counts(snapshot, fallback_snapshot)
    total_sessions = sum(counts.values()) if counts is not None else None
    next_pending_id = _compact_next_pending_id(snapshot, fallback_snapshot)
    turns = payload.get("turns")
    turn_count = len(turns) if isinstance(turns, list) else 0
    next_turn_index = turn_count + 1

    if counts is None:
        counts_text = "sessions unavailable"
    else:
        counts_text = (
            f"sessions {counts['complete']}/{total_sessions} complete, "
            f"{counts['pending']} pending"
        )

    if active_turn is not None:
        current_text = (
            f"status: active session {_format_session_label(active_turn)} "
            f"turn {active_turn.get('turn_index')}; {counts_text}"
        )
        elapsed = active_turn.get("elapsed_seconds")
        idle = active_turn.get("idle_seconds")
        if elapsed is not None or idle is not None:
            current_text += (
                f"; elapsed {format_duration_seconds(elapsed)}, "
                f"idle {format_duration_seconds(idle)}"
            )
    elif next_pending_id is not None:
        total_suffix = "" if total_sessions is None else f"/{total_sessions}"
        state_text = "idle" if turn_count else "no active turn"
        current_text = (
            f"status: {state_text}; next session {next_pending_id}{total_suffix} "
            f"turn {next_turn_index}; {counts_text}"
        )
    else:
        current_text = f"status: no active turn; {counts_text}; next session none"

    active_harness = (
        active_turn.get("harness")
        if active_turn is not None and active_turn.get("harness") is not None
        else payload.get("harness")
    )
    model_value = (
        active_turn.get("model")
        if active_turn is not None and "model" in active_turn
        else payload.get("model")
    )
    model_source_value = (
        active_turn.get("model_source")
        if active_turn is not None and "model_source" in active_turn
        else payload.get("model_source")
    )
    effort_value = (
        active_turn.get("effort")
        if active_turn is not None and "effort" in active_turn
        else payload.get("effort")
    )
    effort_source_value = (
        active_turn.get("effort_source")
        if active_turn is not None and "effort_source" in active_turn
        else payload.get("effort_source")
    )
    model_source = _compact_value(model_source_value)
    effort_source = _compact_value(effort_source_value)
    metadata_line = (
        f"status: harness active {_compact_value(active_harness)}; "
        f"future {_compact_value(control.get('next_harness'), fallback='unchanged')}; "
        f"model {_compact_value(model_value)} ({model_source}); "
        f"effort {_compact_value(effort_value)} ({effort_source})"
    )

    stop_state = "on" if control.get("stop_after_current") else "off"
    tail_state = "on" if control.get("tail_enabled") else "off"
    control_line = (
        "status: controls "
        f"stop-after-current {stop_state}; live tail {tail_state}"
    )

    artifact_source = _compact_artifact_source(payload, active_turn)
    if artifact_source is None:
        paths_line = "status paths: none yet"
    else:
        paths_line = (
            "status paths: "
            f"stdout {_compact_value(artifact_source.get('stdout_path'))}; "
            f"stderr {_compact_value(artifact_source.get('stderr_path'))}; "
            f"last-message {_compact_value(artifact_source.get('last_message_path'))}; "
            f"result {_compact_value(artifact_source.get('result_path'))}"
        )

    lines = [current_text]
    if plan_error is not None:
        lines.append(f"status: plan inspection error: {plan_error}")
    lines.extend([metadata_line, control_line, paths_line])
    return lines


class _LiveTurn:
    """Thread-safe live state and heartbeat manager for one harness turn."""

    def __init__(
        self,
        *,
        payload: dict[str, Any],
        event_path: Path,
        progress: _ProgressReporter,
        active_turn: dict[str, Any],
        persistence: _RunPersistence | None = None,
        heartbeat_interval: float = HEARTBEAT_INTERVAL_SECONDS,
    ) -> None:
        self._payload = payload
        self._event_path = event_path
        self._progress = progress
        self._active_turn = dict(active_turn)
        self._persistence = _run_persistence(persistence)
        self._heartbeat_interval = heartbeat_interval
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_at = _parse_datetime(active_turn["started_at"]) or _utc_now()
        self._last_activity_at = self._started_at

    def start(self) -> None:
        with self._lock:
            with self._persistence.lock:
                self._payload["active_turn"] = dict(self._active_turn)
                self._persistence.write_state(self._payload)
        self._progress.turn_started(self._active_turn)
        self._thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._thread.start()

    def record_activity(self, stream_name: harnesses.StreamName, text: str) -> None:
        signal_to_report: dict[str, Any] | None = None
        now = _utc_now()
        with self._lock:
            self._last_activity_at = now
            self._active_turn["last_activity_at"] = _isoformat(now)
            self._active_turn["last_activity_stream"] = stream_name
            self._active_turn["last_activity_preview"] = _preview_text(text)
            self._active_turn["elapsed_seconds"] = _duration_seconds(
                self._started_at,
                now,
            )
            self._active_turn["idle_seconds"] = 0.0
            char_key = f"{stream_name}_chars"
            self._active_turn[char_key] = int(self._active_turn.get(char_key, 0)) + len(
                text
            )

            signal = _live_action_signal(stream_name, text)
            if signal is not None and self._active_turn.get("action_required") is None:
                signal_to_report = self._action_required_payload(signal, now)
                self._active_turn["action_required"] = signal_to_report
            with self._persistence.lock:
                if signal_to_report is not None:
                    self._payload["action_required"] = signal_to_report
                    self._persistence.append_event(
                        self._event_path,
                        events.ACTION_REQUIRED_EVENT,
                        signal_to_report,
                    )
                self._payload["active_turn"] = dict(self._active_turn)
                self._persistence.write_state(self._payload)

        if signal_to_report is not None:
            self._progress.action_required(signal_to_report)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=1)

    def finish(self, finished_at: datetime) -> dict[str, Any]:
        with self._lock:
            active_turn = dict(self._active_turn)
            active_turn["finished_at"] = _isoformat(finished_at)
            active_turn["duration_seconds"] = _duration_seconds(
                self._started_at,
                finished_at,
            )
            active_turn["idle_seconds"] = _duration_seconds(
                self._last_activity_at,
                finished_at,
            )
            with self._persistence.lock:
                self._payload["active_turn"] = None
                self._persistence.write_state(self._payload)
            return active_turn

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self._heartbeat_interval):
            heartbeat = self._record_heartbeat()
            self._progress.heartbeat(heartbeat)

    def _record_heartbeat(self) -> dict[str, Any]:
        now = _utc_now()
        with self._lock:
            heartbeat_count = int(self._active_turn.get("heartbeat_count", 0)) + 1
            heartbeat = {
                "run_id": self._active_turn["run_id"],
                "turn_index": self._active_turn["turn_index"],
                "session": self._active_turn["session"],
                "session_id": self._active_turn.get("session_id"),
                "total_sessions": self._active_turn.get("total_sessions"),
                "harness": self._active_turn.get("harness"),
                "artifact_dir": self._active_turn["artifact_dir"],
                "stdout_path": self._active_turn["stdout_path"],
                "stderr_path": self._active_turn["stderr_path"],
                "last_message_path": self._active_turn["last_message_path"],
                "result_path": self._active_turn["result_path"],
                "started_at": self._active_turn["started_at"],
                "created_at": _isoformat(now),
                "elapsed_seconds": _duration_seconds(self._started_at, now),
                "last_activity_at": self._active_turn.get("last_activity_at"),
                "last_activity_stream": self._active_turn.get("last_activity_stream"),
                "idle_seconds": _duration_seconds(self._last_activity_at, now),
                "heartbeat_count": heartbeat_count,
            }
            if self._active_turn.get("session_number") is not None:
                heartbeat["session_number"] = self._active_turn["session_number"]
            self._active_turn["last_heartbeat_at"] = heartbeat["created_at"]
            self._active_turn["heartbeat_count"] = heartbeat_count
            self._active_turn["elapsed_seconds"] = heartbeat["elapsed_seconds"]
            self._active_turn["idle_seconds"] = heartbeat["idle_seconds"]
            with self._persistence.lock:
                self._payload["active_turn"] = dict(self._active_turn)
                self._persistence.append_event(
                    self._event_path,
                    events.TURN_HEARTBEAT_EVENT,
                    heartbeat,
                )
                self._persistence.write_state(self._payload)
            return heartbeat

    def _action_required_payload(
        self,
        signal: dict[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        return {
            "run_id": self._active_turn["run_id"],
            "turn_index": self._active_turn["turn_index"],
            "session": self._active_turn["session"],
            "session_id": self._active_turn.get("session_id"),
            "total_sessions": self._active_turn.get("total_sessions"),
            "harness": self._active_turn.get("harness"),
            "artifact_dir": self._active_turn["artifact_dir"],
            "stdout_path": self._active_turn["stdout_path"],
            "stderr_path": self._active_turn["stderr_path"],
            "last_message_path": self._active_turn["last_message_path"],
            "result_path": self._active_turn["result_path"],
            "created_at": _isoformat(now),
            "reason": signal["reason"],
            "message": signal["message"],
            "source": signal["source"],
            "matched": signal["matched"],
        } | (
            {"session_number": self._active_turn["session_number"]}
            if self._active_turn.get("session_number") is not None
            else {}
        )


def _run_auto_commit(
    *,
    event_path: Path,
    payload: dict[str, Any],
    progress: _ProgressReporter,
    cwd: Path,
    run_dir: Path,
    run_id: str,
    session_id: str | None,
    persistence: _RunPersistence | None = None,
) -> dict[str, Any]:
    persistence = _run_persistence(persistence)
    with persistence.lock:
        commit_index = len(payload["commits"]) + 1
    commit_started = _commit_started_payload(
        run_id=run_id,
        cwd=cwd,
        run_dir=run_dir,
        commit_index=commit_index,
        session_id=session_id,
    )
    with persistence.lock:
        persistence.append_event(event_path, events.COMMIT_STARTED_EVENT, commit_started)
        persistence.write_state(payload)
    progress.commit_started(commit_started)

    commit_payload = _run_spec_commit(
        cwd=cwd,
        run_dir=run_dir,
        commit_index=commit_index,
        session_id=session_id,
    )
    commit_finished = {"run_id": run_id, **commit_payload}
    with persistence.lock:
        payload["commits"].append(commit_payload)
        persistence.append_event(event_path, events.COMMIT_FINISHED_EVENT, commit_finished)
        persistence.write_state(payload)
    progress.commit_finished(commit_finished)
    return commit_payload


def _finish_run(
    event_path: Path,
    payload: dict[str, Any],
    progress: _ProgressReporter | None = None,
    persistence: _RunPersistence | None = None,
) -> None:
    persistence = _run_persistence(persistence)
    with persistence.lock:
        finished_at = _utc_now()
        payload["run_finished_at"] = _isoformat(finished_at)
        started_at = _parse_datetime(payload.get("run_started_at"))
        payload["duration_seconds"] = (
            _duration_seconds(started_at, finished_at) if started_at is not None else None
        )
        payload["active_turn"] = None
        _finalize_readiness_lifecycle(payload)
        persistence.append_event(event_path, "run_finished", payload)
        persistence.write_state(payload)
    _notify_agent_mux_loop_finished(payload)
    if progress is not None:
        progress.run_finished(payload)


def _final_readiness_state_from_payload(
    payload: dict[str, Any],
) -> plans.ReadinessState:
    if payload.get("paused") or not payload.get("ok"):
        return "reviewed"
    final_snapshot = payload.get("final_snapshot")
    counts: dict[str, Any] | None = None
    if isinstance(final_snapshot, dict):
        snapshot_counts = final_snapshot.get("counts")
        if isinstance(snapshot_counts, dict):
            counts = snapshot_counts
    if counts is not None:
        pending = int(counts.get("pending", 0) or 0)
        empty = int(counts.get("empty", 0) or 0)
        unknown = int(counts.get("unknown", 0) or 0)
        if pending == 0 and empty == 0 and unknown == 0:
            return "complete"
    return "loop-ready"


def _finalize_readiness_lifecycle(payload: dict[str, Any]) -> None:
    if not payload.get("readiness_state_active"):
        return
    plan_dir_value = payload.get("plan_dir")
    if not isinstance(plan_dir_value, str) or not plan_dir_value:
        return
    final_state = _final_readiness_state_from_payload(payload)
    success, error = _write_readiness_state(Path(plan_dir_value), final_state)
    if success:
        payload["readiness_state_final"] = final_state
        payload["readiness_state_current"] = final_state
    else:
        payload["readiness_state_final"] = None
        payload["readiness_state_write_error"] = error


def _set_pause(
    payload: dict[str, Any],
    *,
    reason: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> None:
    payload["ok"] = False
    payload["paused"] = True
    payload["reason"] = reason
    payload["pause_reason"] = reason
    payload["pause_message"] = message
    payload["pause_details"] = {} if details is None else details
    payload["details"] = payload["pause_details"]


def _pause_run(
    event_path: Path,
    payload: dict[str, Any],
    *,
    reason: str,
    message: str,
    details: dict[str, Any] | None = None,
    persistence: _RunPersistence | None = None,
) -> None:
    persistence = _run_persistence(persistence)
    with persistence.lock:
        _set_pause(payload, reason=reason, message=message, details=details)
        persistence.append_event(
            event_path,
            events.RUN_PAUSED_EVENT,
            {
                "run_id": payload.get("run_id"),
                "run_dir": payload.get("run_dir"),
                "reason": reason,
                "message": message,
                "details": {} if details is None else details,
            },
        )


def _consume_run_control_stop(
    listener: run_control.RunControlListener,
    *,
    snapshot: plans.PlanSnapshot,
    payload: dict[str, Any],
    event_path: Path,
    control_payload: dict[str, Any],
    context: dict[str, Any] | None = None,
    persistence: _RunPersistence | None = None,
) -> bool:
    persistence = _run_persistence(persistence)
    action = listener.consume_stop_after_current()
    if action is None:
        return False
    _record_run_control_action(
        event_path=event_path,
        payload=payload,
        control_payload=control_payload,
        action=action,
        context=context,
        persistence=persistence,
    )
    if snapshot.next_pending is None:
        return False
    with persistence.lock:
        payload["reason"] = "stopped"
    return True


def _allow_not_loop_ready(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "allow_not_loop_ready", False))


def _readiness_gate_payload(
    report: preflight.PreflightReport,
) -> dict[str, Any]:
    readiness = report.readiness
    return {
        "ok": _readiness_gate_passed(report),
        "preflight_ok": report.ok,
        "readiness_state": readiness.state if readiness is not None else None,
        "expected_readiness_sha256": report.expected_readiness_sha256,
        "recorded_readiness_sha256": (
            readiness.readiness_sha256 if readiness is not None else None
        ),
        "findings_by_severity": {
            "red": len(report.red_findings),
            "yellow": len(report.yellow_findings),
            "green": len(report.green_findings),
        },
        "findings": [
            {
                "severity": finding.severity,
                "category": finding.category,
                "message": finding.message,
                "session_id": finding.session_id,
                "path": finding.path,
                "line": finding.line,
                "detail": finding.detail,
            }
            for finding in report.findings
        ],
    }


def _readiness_gate_passed(report: preflight.PreflightReport) -> bool:
    if not report.ok:
        return False
    readiness = report.readiness
    if readiness is None:
        return False
    if readiness.state != "loop-ready":
        return False
    return True


def _readiness_gate_failure_message(
    report: preflight.PreflightReport,
) -> str:
    if report.readiness is None:
        return "plan manifest is missing readiness metadata"
    if not report.ok:
        return (
            "preflight failed: "
            f"{len(report.red_findings)} red, "
            f"{len(report.yellow_findings)} yellow finding(s)"
        )
    return (
        f"plan readiness state is {report.readiness.state!r}; "
        "run preflight --mark-loop-ready first"
    )


def _write_readiness_state(
    plan_dir: Path,
    new_state: plans.ReadinessState,
    *,
    expected_sha256: str | None = None,
) -> tuple[bool, str | None]:
    sha = expected_sha256
    if sha is None:
        try:
            sha = plans.stable_readiness_sha256(plan_dir)
        except plans.PlanError as error:
            return False, str(error)
    metadata = plans.ReadinessMetadata(
        state=new_state,
        readiness_sha256=sha,
        checked_at=_isoformat(_utc_now()),
    )
    try:
        plans.write_readiness_metadata(plan_dir, metadata)
    except plans.PlanError as error:
        return False, str(error)
    return True, None


def _final_readiness_state(
    payload: dict[str, Any],
    snapshot: plans.PlanSnapshot,
) -> plans.ReadinessState:
    if payload.get("paused") or not payload.get("ok"):
        return "reviewed"
    if _all_sessions_complete(snapshot):
        return "complete"
    return "loop-ready"


def _strip_own_lock_findings(
    report: preflight.PreflightReport,
    *,
    lock_info: state.LockInfo,
) -> preflight.PreflightReport:
    """Drop env-lock findings that point at the runner's own active lock."""
    own_pid = os.getpid()
    own_lock_path = str(lock_info.path)

    def is_self_lock(finding: preflight.PreflightFinding) -> bool:
        if finding.category != "env-lock":
            return False
        if finding.path != own_lock_path:
            return False
        if lock_info.pid is not None and lock_info.pid != own_pid:
            return False
        return True

    filtered = tuple(
        finding for finding in report.findings if not is_self_lock(finding)
    )
    if len(filtered) == len(report.findings):
        return report
    return preflight.PreflightReport(
        plan_dir=report.plan_dir,
        findings=filtered,
        snapshot=report.snapshot,
        readiness=report.readiness,
        expected_readiness_sha256=report.expected_readiness_sha256,
        contracts_by_session=report.contracts_by_session,
    )


def _verification_failure_summary(
    result: verification.VerificationResult,
) -> str:
    failures = result.failures
    total = len(result.checks)
    parts = [
        f"{check.name} ({check.failure_reason or 'failed'})"
        for check in failures
    ]
    rendered = "; ".join(parts) if parts else "no checks reported"
    return (
        f"{len(failures)} of {total} verification check(s) failed: {rendered}"
    )


def _verification_failure_details(
    result: verification.VerificationResult,
    contract: contracts.CompletionContract,
) -> dict[str, Any]:
    return {
        "session": result.session_id.value,
        "recipe": {
            "check_count": len(contract.verify.checks),
            "kinds": [check.kind for check in contract.verify.checks],
            "names": [check.name for check in contract.verify.checks],
        },
        "passed": result.passed,
        "checks": [asdict(check) for check in result.checks],
        "failures": [asdict(check) for check in result.failures],
    }


def _session_path_in_snapshot(
    snapshot: plans.PlanSnapshot, session_id: str
) -> Path | None:
    for session in snapshot.sessions:
        if session.id == session_id:
            return session.path
    return None


def _revert_session_status_to_pending(
    snapshot: plans.PlanSnapshot,
    session_id: str,
    *,
    failure_note: str,
) -> Path | None:
    path = _session_path_in_snapshot(snapshot, session_id)
    if path is None:
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    lines = text.splitlines(keepends=False)
    handoff_index: int | None = None
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].strip() == plans.HANDOFF_HEADING:
            handoff_index = index
            break
    if handoff_index is None:
        return None
    section_end = len(lines)
    for index in range(handoff_index + 1, len(lines)):
        if lines[index].startswith("## "):
            section_end = index
            break
    for index in range(handoff_index + 1, section_end):
        if lines[index].startswith(plans.STATUS_PREFIX):
            lines[index] = f"{plans.STATUS_PREFIX} pending"
            break
    lines = _append_handoff_note(
        lines,
        handoff_index=handoff_index,
        section_end=section_end,
        note=failure_note,
    )
    suffix = "\n" if text.endswith("\n") else ""
    path.write_text("\n".join(lines) + suffix, encoding="utf-8")
    return path


def _enforce_session_verification(
    *,
    event_path: Path,
    payload: dict[str, Any],
    persistence: _RunPersistence,
    before: plans.PlanSnapshot,
    after: plans.PlanSnapshot,
    cwd: Path,
    session_id: str,
    session_label: str,
    session_details: dict[str, Any],
    allow_not_loop_ready: bool,
    turn_payload: dict[str, Any],
    paths: dict[str, Path],
) -> bool:
    """Run the session's `Verify` recipe and pause on failure or contract problems.

    Returns ``True`` when the run should pause (the caller must ``break``);
    ``False`` when verification passed and the loop should continue.
    """
    try:
        contract = contracts.read_completion_contract(before.plan_dir, session_id)
    except plans.PlanError as error:
        failure_note = (
            f"verification skipped: malformed completion contract ({error})"
        )
        reverted = _revert_session_status_to_pending(
            after, session_id, failure_note=failure_note
        )
        snapshot_after = _reinspect_after_revert(before.plan_dir, after, reverted)
        with persistence.lock:
            payload["final_snapshot"] = _snapshot_payload(snapshot_after)
            turn_payload["verification"] = {
                "session": session_id,
                "reason": "malformed-contract",
                "error": str(error),
            }
            _write_json_file(paths["result"], turn_payload)
        _pause_run(
            event_path,
            payload,
            reason="malformed-contract",
            message=(
                f"completion contract for {session_label} is malformed: {error}"
            ),
            details={**session_details, "error": str(error)},
            persistence=persistence,
        )
        return True

    if contract is None:
        if allow_not_loop_ready:
            with persistence.lock:
                turn_payload["verification"] = {
                    "session": session_id,
                    "reason": "no-contract",
                    "enforced": False,
                }
                _write_json_file(paths["result"], turn_payload)
            return False
        failure_note = (
            "verification skipped: session is missing a Completion Contract section"
        )
        reverted = _revert_session_status_to_pending(
            after, session_id, failure_note=failure_note
        )
        snapshot_after = _reinspect_after_revert(before.plan_dir, after, reverted)
        with persistence.lock:
            payload["final_snapshot"] = _snapshot_payload(snapshot_after)
            turn_payload["verification"] = {
                "session": session_id,
                "reason": "no-contract",
                "enforced": True,
            }
            _write_json_file(paths["result"], turn_payload)
        _pause_run(
            event_path,
            payload,
            reason="no-contract",
            message=(
                f"{session_label} has no Completion Contract section to verify"
            ),
            details={**session_details},
            persistence=persistence,
        )
        return True

    try:
        result = verification.run_verification(contract, repo_root=cwd)
    except OSError as error:
        failure_note = (
            f"verification failed to execute: {error}"
        )
        reverted = _revert_session_status_to_pending(
            after, session_id, failure_note=failure_note
        )
        snapshot_after = _reinspect_after_revert(before.plan_dir, after, reverted)
        with persistence.lock:
            payload["final_snapshot"] = _snapshot_payload(snapshot_after)
            turn_payload["verification"] = {
                "session": session_id,
                "reason": "verify-failed",
                "error": str(error),
            }
            _write_json_file(paths["result"], turn_payload)
        _pause_run(
            event_path,
            payload,
            reason="verify-failed",
            message=f"verification for {session_label} could not run: {error}",
            details={**session_details, "error": str(error)},
            persistence=persistence,
        )
        return True

    details = _verification_failure_details(result, contract)
    with persistence.lock:
        turn_payload["verification"] = {
            "session": session_id,
            "reason": (
                verification.VERIFY_EXIT_REASON_OK
                if result.passed
                else verification.VERIFY_EXIT_REASON_FAILED
            ),
            "passed": result.passed,
            "recipe": details["recipe"],
            "checks": details["checks"],
        }
        _write_json_file(paths["result"], turn_payload)

    if result.passed:
        return False

    summary = _verification_failure_summary(result)
    reverted = _revert_session_status_to_pending(
        after,
        session_id,
        failure_note=f"verification failed: {summary}",
    )
    snapshot_after = _reinspect_after_revert(before.plan_dir, after, reverted)
    with persistence.lock:
        payload["final_snapshot"] = _snapshot_payload(snapshot_after)
    _pause_run(
        event_path,
        payload,
        reason="verify-failed",
        message=(
            f"verification failed for {session_label}: {summary}"
        ),
        details={**session_details, **details},
        persistence=persistence,
    )
    return True


def _reinspect_after_revert(
    plan_dir: Path,
    fallback: plans.PlanSnapshot,
    reverted_path: Path | None,
) -> plans.PlanSnapshot:
    if reverted_path is None:
        return fallback
    try:
        return plans.inspect_plan(plan_dir)
    except plans.PlanError:
        return fallback


def _append_handoff_note(
    lines: list[str],
    *,
    handoff_index: int,
    section_end: int,
    note: str,
) -> list[str]:
    new_bullet = f"  - {note}"
    notes_index: int | None = None
    for index in range(handoff_index + 1, section_end):
        if lines[index].lstrip().startswith("- **Notes**"):
            notes_index = index
            break
    if notes_index is None:
        insert_at = section_end
        while insert_at - 1 > handoff_index and lines[insert_at - 1].strip() == "":
            insert_at -= 1
        return (
            lines[:insert_at]
            + ["- **Notes**:", new_bullet]
            + lines[insert_at:]
        )
    insert_at = notes_index + 1
    while insert_at < section_end:
        candidate = lines[insert_at]
        stripped = candidate.lstrip()
        leading = candidate[: len(candidate) - len(stripped)]
        if len(leading) >= 2 and stripped.startswith("-"):
            insert_at += 1
            continue
        break
    return lines[:insert_at] + [new_bullet] + lines[insert_at:]


def _initial_payload(
    *,
    args: argparse.Namespace,
    snapshot: plans.PlanSnapshot,
    state_dir: Path,
    run_dir: Path,
    event_path: Path,
    state_path: Path,
    lock_info: state.LockInfo,
    cwd: Path,
    run_id: str,
    commit_mode: str | None,
) -> dict[str, Any]:
    model_effort = _selected_model_effort_payload(args)
    return responses.success_payload(
        "run",
        paused=False,
        details={},
        dry_run=args.dry_run,
        reason="",
        pause_reason=None,
        pause_message="",
        pause_details={},
        run_id=run_id,
        run_started_at=_isoformat(_utc_now()),
        run_finished_at=None,
        duration_seconds=None,
        active_turn=None,
        action_required=None,
        approval_mode=_approval_mode(args),
        commit_mode=commit_mode,
        plan_dir=str(snapshot.plan_dir),
        cwd=str(cwd),
        harness=args.harness,
        **model_effort,
        state_dir=str(state_dir),
        run_dir=str(run_dir),
        event_path=str(event_path),
        events_path=str(event_path),
        state_path=str(state_path),
        lock_path=str(lock_info.path),
        harness_config=_safe_harness_config_payload(),
        completed_sessions=[],
        completed_session_ids=[],
        commits=[],
        turns=[],
        control=_initial_control_payload(),
        initial_snapshot=_snapshot_payload(snapshot),
        final_snapshot=_snapshot_payload(snapshot),
        allow_not_loop_ready=_allow_not_loop_ready(args),
        readiness_gate=None,
        readiness_state_initial=None,
        readiness_state_final=None,
    )


def _dry_run_payload(
    *,
    args: argparse.Namespace,
    snapshot: plans.PlanSnapshot,
    cwd: Path,
    run_dir: Path,
) -> dict[str, Any]:
    model_effort = _selected_model_effort_payload(args)
    next_pending = snapshot.next_pending
    if next_pending is None:
        return {
            "next_pending": None,
            "next_pending_id": None,
            "prompt": None,
            "invocation": None,
            **model_effort,
        }

    paths = _turn_paths(run_dir, turn_index=1, session_id=next_pending.id)
    invocation = build_harness_invocation(
        args.harness,
        cwd=cwd,
        plan_dir=snapshot.plan_dir,
        unsafe_bypass=_unsafe_bypass(args),
        last_message_path=paths["last_message"],
        model=args.model,
        effort=args.effort,
    )
    return {
        "next_pending": _session_compat_value(next_pending.id),
        "next_pending_id": next_pending.id,
        "prompt": build_session_prompt(args.harness, snapshot.plan_dir),
        "invocation": _build_invocation_payload(
            invocation,
            model_effort=model_effort,
        ),
        **model_effort,
    }


def _run_locked(
    *,
    args: argparse.Namespace,
    cwd: Path,
    timeout: int | None,
    max_sessions: int | None,
    commit_mode: str | None,
    lock_info: state.LockInfo,
    run_id: str,
) -> tuple[int, dict[str, Any]]:
    snapshot = plans.inspect_plan(args.plan_dir)
    state_dir = state.plan_state_dir(snapshot.plan_dir, args.state_dir)
    run_dir = state_dir / "runs" / run_id
    event_path = state_dir / events.EVENTS_FILE_NAME
    state_path = state_dir / events.STATE_FILE_NAME
    payload = _initial_payload(
        args=args,
        snapshot=snapshot,
        state_dir=state_dir,
        run_dir=run_dir,
        event_path=event_path,
        state_path=state_path,
        lock_info=lock_info,
        cwd=cwd,
        run_id=run_id,
        commit_mode=commit_mode,
    )
    persistence = _RunPersistence()
    output = _StderrOutputCoordinator(enabled=not args.json)
    progress = _ProgressReporter(enabled=not args.json, output=output)
    inline_tail = _InlineTailController(
        plan_dir=snapshot.plan_dir,
        state_dir=args.state_dir,
        output=output,
    )
    persistence.append_event(
        event_path,
        "run_started",
        {
            "plan_dir": str(snapshot.plan_dir),
            "harness": args.harness,
            "model": payload["model"],
            "model_source": payload["model_source"],
            "effort": payload["effort"],
            "effort_source": payload["effort_source"],
            "dry_run": args.dry_run,
            "max_sessions": max_sessions,
            "commit_mode": commit_mode,
            "run_id": run_id,
            "run_dir": str(run_dir),
            "run_started_at": payload["run_started_at"],
            "approval_mode": payload["approval_mode"],
            "counts": payload["initial_snapshot"]["counts"],
            "next_pending": payload["initial_snapshot"]["next_pending"],
        },
    )
    persistence.write_state(payload)
    _notify_agent_mux_loop_started(payload)
    progress.run_started(payload)

    invalid_pause = _invalid_status_pause(snapshot)
    if invalid_pause is not None:
        reason, message, details = invalid_pause
        with persistence.lock:
            payload["final_snapshot"] = _snapshot_payload(snapshot)
        _pause_run(
            event_path,
            payload,
            reason=reason,
            message=message,
            details=details,
            persistence=persistence,
        )
        _finish_run(event_path, payload, progress, persistence=persistence)
        return 1, payload

    if snapshot.next_pending is None:
        if not _all_sessions_complete(snapshot):
            _pause_run(
                event_path,
                payload,
                reason="no-pending-but-incomplete",
                message="no pending session was found, but not every session is complete",
                details={"snapshot": _snapshot_payload(snapshot)},
                persistence=persistence,
            )
            _finish_run(event_path, payload, progress, persistence=persistence)
            return 1, payload
        with persistence.lock:
            payload["reason"] = "all-complete"
        _finish_run(event_path, payload, progress, persistence=persistence)
        return 0, payload

    if args.dry_run:
        with persistence.lock:
            payload["reason"] = "dry-run"
            payload["planned"] = _dry_run_payload(
                args=args,
                snapshot=snapshot,
                cwd=cwd,
                run_dir=run_dir,
            )
        _finish_run(event_path, payload, progress, persistence=persistence)
        return 0, payload

    allow_not_loop_ready = _allow_not_loop_ready(args)
    preflight_report = preflight.run_preflight(
        snapshot.plan_dir,
        cwd=cwd,
        harness=args.harness,
        commit_mode=commit_mode,
        state_dir=args.state_dir,
    )
    preflight_report = _strip_own_lock_findings(
        preflight_report, lock_info=lock_info
    )
    gate_payload = _readiness_gate_payload(preflight_report)
    gate_passed = gate_payload["ok"]
    with persistence.lock:
        payload["readiness_gate"] = gate_payload
        if preflight_report.readiness is not None:
            payload["readiness_state_initial"] = preflight_report.readiness.state

    if not gate_passed and not allow_not_loop_ready:
        gate_message = _readiness_gate_failure_message(preflight_report)
        with persistence.lock:
            payload["final_snapshot"] = _snapshot_payload(snapshot)
        _pause_run(
            event_path,
            payload,
            reason="not-loop-ready",
            message=gate_message,
            details=gate_payload,
            persistence=persistence,
        )
        _finish_run(event_path, payload, progress, persistence=persistence)
        return 1, payload

    try:
        if commit_mode == COMMIT_MODE_END:
            _validate_commit_at_end_scope(
                snapshot=snapshot,
                max_sessions=max_sessions,
            )
        if commit_mode is not None:
            commit_preflight = _validate_commit_preflight(cwd)
            with persistence.lock:
                payload["commit_preflight"] = commit_preflight
    except RunError as error:
        with persistence.lock:
            payload["final_snapshot"] = _snapshot_payload(snapshot)
        _pause_run(
            event_path,
            payload,
            reason="commit-preflight",
            message=str(error),
            details={
                "commit_mode": commit_mode,
                "max_sessions": max_sessions,
                "pending_sessions": _status_counts(snapshot)["pending"],
            },
            persistence=persistence,
        )
        _finish_run(event_path, payload, progress, persistence=persistence)
        return 1, payload

    should_transition = gate_passed or (
        allow_not_loop_ready and preflight_report.readiness is not None
    )
    if should_transition:
        running_success, running_error = _write_readiness_state(
            snapshot.plan_dir,
            "running",
            expected_sha256=preflight_report.expected_readiness_sha256,
        )
        with persistence.lock:
            if running_success:
                payload["readiness_state_active"] = True
                payload["readiness_state_current"] = "running"
            else:
                payload["readiness_state_active"] = False
                payload["readiness_state_write_error"] = running_error

    run_control_lock = persistence.lock
    run_control_context: dict[str, Any] | None = None
    control_payload = dict(payload["control"])

    def handle_run_control_action(action: run_control.RunControlAction) -> None:
        if action.action == "consumed":
            return
        if action.action == "tail-disabled":
            inline_tail.disable()
        with run_control_lock:
            context = (
                None
                if run_control_context is None
                else dict(run_control_context)
            )
            _record_run_control_action(
                event_path=event_path,
                payload=payload,
                control_payload=control_payload,
                action=action,
                context=context,
                persistence=persistence,
            )
        if action.action == "tail-enabled":
            inline_tail.enable()

    def handle_run_control_status(action: run_control.RunControlAction) -> None:
        with run_control_lock:
            context = (
                None
                if run_control_context is None
                else dict(run_control_context)
            )
            status_control = dict(control_payload)
            status_control["stop_after_current"] = action.stop_after_current
            status_control["tail_enabled"] = action.tail_enabled
            status_control["next_harness"] = action.next_harness
            status_payload = dict(payload)
            active_turn = payload.get("active_turn")
            if isinstance(active_turn, dict):
                status_payload["active_turn"] = dict(active_turn)
            elif context is not None:
                status_payload["active_turn"] = dict(context)
            turns = payload.get("turns")
            if isinstance(turns, list):
                status_payload["turns"] = [
                    dict(turn) if isinstance(turn, dict) else turn
                    for turn in turns
                ]

        try:
            status_snapshot = plans.inspect_plan(snapshot.plan_dir)
            plan_error = None
        except plans.PlanError as error:
            status_snapshot = None
            plan_error = str(error)

        progress.status_snapshot(
            _compact_status_lines(
                payload=status_payload,
                control=status_control,
                snapshot=status_snapshot,
                plan_error=plan_error,
            )
        )

    run_controls = run_control.RunControlListener.for_run(
        args,
        state=run_control.RunControlState(current_harness=args.harness),
        stdin=sys.stdin,
        stderr=output.stream,
        on_action=handle_run_control_action,
        on_status=handle_run_control_status,
    )
    run_controls.start()

    completed_count = 0
    previous_turn_finished_at: datetime | None = None
    with persistence.lock:
        total_sessions = sum(payload["initial_snapshot"]["counts"].values())
    while snapshot.next_pending is not None:
        if max_sessions is not None and completed_count >= max_sessions:
            with persistence.lock:
                payload["reason"] = "max-sessions"
            break

        before = snapshot
        pending_session = before.next_pending
        if pending_session is None:
            break
        session_id = pending_session.id
        session_label = _session_label(session_id)
        session_identity = _session_identity_payload(session_id)
        session_details = _session_details_payload(session_id)

        with persistence.lock:
            turn_index = len(payload["turns"]) + 1
        paths = _turn_paths(
            run_dir,
            turn_index=turn_index,
            session_id=session_id,
        )
        with run_control_lock:
            turn_harness = _selected_future_harness(args, control_payload)
        turn_model_effort = _selected_model_effort_payload(
            args,
            harness=turn_harness,
        )
        preflight_failure = _harness_preflight_failure(
            turn_harness,
            session_id=session_id,
        )
        if preflight_failure is not None:
            message, details = preflight_failure
            with persistence.lock:
                payload["final_snapshot"] = _snapshot_payload(before)
            _pause_run(
                event_path,
                payload,
                reason="harness-unavailable",
                message=(
                    f"harness unavailable before {session_label}: {message}"
                ),
                details=details,
                persistence=persistence,
            )
            break
        prompt = build_session_prompt(turn_harness, before.plan_dir)
        turn_started_at = _utc_now()
        turn_started_at_text = _isoformat(turn_started_at)
        idle_seconds_since_previous_turn = (
            0.0
            if previous_turn_finished_at is None
            else _duration_seconds(previous_turn_finished_at, turn_started_at)
        )
        active_turn = {
            "run_id": run_id,
            "turn_index": turn_index,
            **session_identity,
            "total_sessions": total_sessions,
            "artifact_dir": str(paths["artifact_dir"]),
            "prompt": prompt,
            "harness": turn_harness,
            "model": turn_model_effort["model"],
            "model_source": turn_model_effort["model_source"],
            "effort": turn_model_effort["effort"],
            "effort_source": turn_model_effort["effort_source"],
            "stdout_path": str(paths["stdout"]),
            "stderr_path": str(paths["stderr"]),
            "last_message_path": str(paths["last_message"]),
            "result_path": str(paths["result"]),
            "started_at": turn_started_at_text,
            "finished_at": None,
            "duration_seconds": None,
            "idle_seconds_since_previous_turn": idle_seconds_since_previous_turn,
            "last_activity_at": turn_started_at_text,
            "last_activity_stream": None,
            "last_activity_preview": "",
            "last_heartbeat_at": None,
            "elapsed_seconds": 0.0,
            "idle_seconds": 0.0,
            "heartbeat_count": 0,
            "stdout_chars": 0,
            "stderr_chars": 0,
            "action_required": None,
        }
        log_stream_callback = _prepare_stream_logs(paths)
        live_turn = _LiveTurn(
            payload=payload,
            event_path=event_path,
            progress=progress,
            active_turn=active_turn,
            persistence=persistence,
        )

        def stream_callback(stream_name: harnesses.StreamName, text: str) -> None:
            log_stream_callback(stream_name, text)
            live_turn.record_activity(stream_name, text)

        with run_control_lock:
            run_control_context = dict(active_turn)
        persistence.append_event(event_path, "turn_started", active_turn)
        live_turn.start()
        _notify_agent_mux_loop_turn_started(payload, active_turn)
        try:
            result = harnesses.run_harness(
                turn_harness,
                cwd=cwd,
                plan_dir=before.plan_dir,
                prompt=prompt,
                unsafe_bypass=_unsafe_bypass(args),
                last_message_path=paths["last_message"],
                timeout=timeout,
                stream_callback=stream_callback,
                model=args.model,
                effort=args.effort,
            )
        except Exception:
            live_turn.stop()
            live_turn.finish(_utc_now())
            run_controls.stop()
            inline_tail.stop()
            raise
        live_turn.stop()
        turn_finished_at = _utc_now()
        live_turn_payload = live_turn.finish(turn_finished_at)
        with run_control_lock:
            run_control_context = None
        previous_turn_finished_at = turn_finished_at
        plan_error = None
        try:
            after = plans.inspect_plan(before.plan_dir)
        except plans.PlanError as error:
            after = None
            plan_error = str(error)
        turn_payload = _write_turn_artifacts(
            paths=paths,
            result=result,
            before=before,
            after=after,
            run_id=run_id,
            turn_index=turn_index,
            session_id=session_id,
            live_turn=live_turn_payload,
            plan_error=plan_error,
        )
        with persistence.lock:
            payload["turns"].append(turn_payload)
            persistence.append_event(event_path, "turn_finished", turn_payload)
            persistence.write_state(payload)
        progress.turn_finished(turn_payload)

        if result.timed_out:
            with persistence.lock:
                payload["final_snapshot"] = (
                    _snapshot_payload(after)
                    if after is not None
                    else _snapshot_payload(before)
                )
            _pause_run(
                event_path,
                payload,
                reason="timeout",
                message=f"harness turn timed out for {session_label}",
                details=session_details,
                persistence=persistence,
            )
            break
        if after is None:
            with persistence.lock:
                payload["final_snapshot"] = _snapshot_payload(before)
            _pause_run(
                event_path,
                payload,
                reason="malformed-plan",
                message="plan could not be inspected after the harness turn",
                details={"error": plan_error, **session_details},
                persistence=persistence,
            )
            break

        invalid_pause = _invalid_status_pause(after)
        if invalid_pause is not None:
            reason, message, details = invalid_pause
            with persistence.lock:
                payload["final_snapshot"] = _snapshot_payload(after)
            _pause_run(
                event_path,
                payload,
                reason=reason,
                message=message,
                details=details | session_details,
                persistence=persistence,
            )
            break

        runner_pause = turn_payload.get("runner_pause")
        if runner_pause is not None:
            with persistence.lock:
                payload["final_snapshot"] = _snapshot_payload(after)
            _pause_run(
                event_path,
                payload,
                reason="runner-pause",
                message=str(runner_pause),
                details={
                    **session_details,
                    "source": turn_payload.get("runner_pause_source"),
                },
                persistence=persistence,
            )
            break
        if result.returncode != 0:
            with persistence.lock:
                payload["final_snapshot"] = _snapshot_payload(after)
            _pause_run(
                event_path,
                payload,
                reason="nonzero-exit",
                message=(
                    f"harness exited with status {result.returncode} "
                    f"for {session_label}"
                ),
                details={
                    **session_details,
                    "returncode": result.returncode,
                },
                persistence=persistence,
            )
            break

        expected_change = (
            _session_status_change_payload(
                session_id,
                before="pending",
                after="complete",
            ),
        )
        status_changes = _status_changes(before, after)
        if not status_changes:
            with persistence.lock:
                payload["expected_status_changes"] = list(expected_change)
                payload["observed_status_changes"] = []
                payload["final_snapshot"] = _snapshot_payload(after)
            _pause_run(
                event_path,
                payload,
                reason="no-status-change",
                message=f"{session_label} did not change status",
                details={
                    **session_details,
                    "expected_status_changes": list(expected_change),
                    "observed_status_changes": [],
                },
                persistence=persistence,
            )
            break
        if len(status_changes) > 1:
            with persistence.lock:
                payload["expected_status_changes"] = list(expected_change)
                payload["observed_status_changes"] = list(status_changes)
                payload["final_snapshot"] = _snapshot_payload(after)
            _pause_run(
                event_path,
                payload,
                reason="multiple-status-changes",
                message="more than one session status changed during one harness turn",
                details={
                    **session_details,
                    "expected_status_changes": list(expected_change),
                    "observed_status_changes": list(status_changes),
                },
                persistence=persistence,
            )
            break
        if status_changes != expected_change:
            with persistence.lock:
                payload["expected_status_changes"] = list(expected_change)
                payload["observed_status_changes"] = list(status_changes)
                payload["final_snapshot"] = _snapshot_payload(after)
            _pause_run(
                event_path,
                payload,
                reason="unexpected-status-change",
                message="the changed session status did not match the expected pending completion",
                details={
                    **session_details,
                    "expected_status_changes": list(expected_change),
                    "observed_status_changes": list(status_changes),
                },
                persistence=persistence,
            )
            break

        if _enforce_session_verification(
            event_path=event_path,
            payload=payload,
            persistence=persistence,
            before=before,
            after=after,
            cwd=cwd,
            session_id=session_id,
            session_label=session_label,
            session_details=session_details,
            allow_not_loop_ready=allow_not_loop_ready,
            turn_payload=turn_payload,
            paths=paths,
        ):
            break

        try:
            ledger_updates = plans.sync_derived_status_ledgers(after)
        except plans.PlanError as error:
            with persistence.lock:
                payload["final_snapshot"] = _snapshot_payload(after)
            _pause_run(
                event_path,
                payload,
                reason="malformed-plan",
                message="derived status ledgers could not be synced",
                details={
                    **session_details,
                    "error": str(error),
                },
                persistence=persistence,
            )
            break
        if ledger_updates:
            try:
                after = plans.inspect_plan(before.plan_dir)
            except plans.PlanError as error:
                with persistence.lock:
                    payload["final_snapshot"] = _snapshot_payload(after)
                _pause_run(
                    event_path,
                    payload,
                    reason="malformed-plan",
                    message="plan could not be inspected after syncing ledgers",
                    details={
                        **session_details,
                        "error": str(error),
                    },
                    persistence=persistence,
                )
                break
            with persistence.lock:
                turn_payload["ledger_updates"] = [str(path) for path in ledger_updates]
                turn_payload["after"] = _snapshot_payload(after)
                _write_json_file(paths["result"], turn_payload)

        if commit_mode == COMMIT_MODE_PER_SESSION:
            commit_payload = _run_auto_commit(
                event_path=event_path,
                payload=payload,
                progress=progress,
                cwd=cwd,
                run_dir=run_dir,
                run_id=run_id,
                session_id=session_id,
                persistence=persistence,
            )
            if not commit_payload["success"]:
                with persistence.lock:
                    payload["final_snapshot"] = _snapshot_payload(after)
                _pause_run(
                    event_path,
                    payload,
                    reason="commit-failed",
                    message=(
                        "auto-commit failed after session "
                        f"{session_id} completed"
                    ),
                    details={
                        **session_details,
                        "commit": commit_payload,
                    },
                    persistence=persistence,
                )
                break

        with persistence.lock:
            payload["completed_sessions"].append(_session_compat_value(session_id))
            payload.setdefault("completed_session_ids", []).append(session_id)
        completed_count += 1
        snapshot = after
        with persistence.lock:
            payload["final_snapshot"] = _snapshot_payload(snapshot)
        with run_control_lock:
            should_stop_after_current = _consume_run_control_stop(
                run_controls,
                snapshot=snapshot,
                payload=payload,
                event_path=event_path,
                control_payload=control_payload,
                context=turn_payload,
                persistence=persistence,
            )
        if should_stop_after_current:
            break

    with persistence.lock:
        should_finalize_reason = payload["ok"] and payload["reason"] == ""

    if should_finalize_reason:
        if snapshot.next_pending is None:
            if _all_sessions_complete(snapshot):
                with persistence.lock:
                    payload["final_snapshot"] = _snapshot_payload(snapshot)
                if commit_mode == COMMIT_MODE_END:
                    commit_payload = _run_auto_commit(
                        event_path=event_path,
                        payload=payload,
                        progress=progress,
                        cwd=cwd,
                        run_dir=run_dir,
                        run_id=run_id,
                        session_id=None,
                        persistence=persistence,
                    )
                    if not commit_payload["success"]:
                        _pause_run(
                            event_path,
                            payload,
                            reason="commit-failed",
                            message=(
                                "final auto-commit failed after all sessions completed"
                            ),
                            details={"commit": commit_payload},
                            persistence=persistence,
                        )
                    else:
                        with persistence.lock:
                            payload["reason"] = "all-complete"
                else:
                    with persistence.lock:
                        payload["reason"] = "all-complete"
            else:
                _pause_run(
                    event_path,
                    payload,
                    reason="no-pending-but-incomplete",
                    message="no pending session was found, but not every session is complete",
                    details={"snapshot": _snapshot_payload(snapshot)},
                    persistence=persistence,
                )
        else:
            with persistence.lock:
                payload["reason"] = "stopped"

    run_controls.stop()
    try:
        _finish_run(event_path, payload, progress, persistence=persistence)
    finally:
        inline_tail.stop()
    with persistence.lock:
        exit_code = 0 if payload["ok"] else 1
    return exit_code, payload


def _state_error_reason(error: state.StateError) -> tuple[str, bool]:
    message = str(error).lower()
    if "plan is locked" in message or "could not acquire lock" in message:
        return "lock-contention", True
    return "state-error", False


def _error_payload(
    args: argparse.Namespace,
    *,
    reason: str,
    message: str,
    paused: bool,
    cwd: Path | None = None,
    snapshot: plans.PlanSnapshot | None = None,
) -> dict[str, Any]:
    try:
        plan_dir = str(
            snapshot.plan_dir
            if snapshot is not None
            else plans.canonicalize_path(args.plan_dir)
        )
    except OSError:
        plan_dir = str(args.plan_dir)

    payload = responses.error_payload(
        "run",
        reason=reason,
        error=message,
        paused=paused,
        details={},
        dry_run=getattr(args, "dry_run", False),
        pause_reason=reason if paused else None,
        pause_message=message if paused else "",
        pause_details={},
        commit_mode=_commit_mode_payload(args),
        commits=[],
        plan_dir=plan_dir,
        cwd=str(cwd) if cwd is not None else str(getattr(args, "cwd", "")),
        harness=getattr(args, "harness", None),
        completed_sessions=[],
        completed_session_ids=[],
        turns=[],
    )
    payload.update(_selected_model_effort_payload(args))
    if snapshot is not None:
        state_dir = state.plan_state_dir(snapshot.plan_dir, args.state_dir)
        payload["state_dir"] = str(state_dir)
        payload["state_path"] = str(state_dir / events.STATE_FILE_NAME)
        payload["event_path"] = str(state_dir / events.EVENTS_FILE_NAME)
        payload["events_path"] = str(state_dir / events.EVENTS_FILE_NAME)
        payload["lock_path"] = str(
            state.lock_path_for_plan(snapshot.plan_dir, args.state_dir)
        )
        payload["initial_snapshot"] = _snapshot_payload(snapshot)
        payload["final_snapshot"] = _snapshot_payload(snapshot)
    return payload


def _print_model_effort_lines(payload: dict[str, Any]) -> None:
    model = payload.get("model")
    model_source = payload.get("model_source")
    if model is not None and model_source is not None:
        print(f"Model: {model} ({model_source})")

    effort = payload.get("effort")
    if effort is not None:
        effort_source = payload.get("effort_source")
        if effort_source is None:
            print(f"Effort: {effort}")
        else:
            print(f"Effort: {effort} ({effort_source})")


def _commit_scope_label(commit: dict[str, Any]) -> str:
    scope = commit.get("scope")
    session = commit.get("session_id") or commit.get("session")
    if scope == "session" and session is not None:
        return f"session {session}"
    if scope == "end":
        return "end"
    return str(scope or "unknown")


def _print_commit_outcome_lines(payload: dict[str, Any]) -> None:
    commits = payload.get("commits")
    if not isinstance(commits, list):
        return
    for commit in commits:
        if not isinstance(commit, dict):
            continue
        label = _commit_scope_label(commit)
        index = commit.get("commit_index", "?")
        if commit.get("success"):
            sha = commit.get("commit_sha") or "unknown"
            print(f"Created commit {sha} ({label})")
        else:
            status = commit.get("status_result", "unknown")
            print(f"Commit {index} failed ({label}): {status}")


def _last_commit(payload: dict[str, Any]) -> dict[str, Any] | None:
    commits = payload.get("commits")
    if not isinstance(commits, list) or not commits:
        return None
    commit = commits[-1]
    return commit if isinstance(commit, dict) else None


def _print_text_outcome(payload: dict[str, Any]) -> None:
    print(f"Plan: {payload['plan_dir']}")
    if payload.get("harness") is not None:
        print(f"Harness: {payload['harness']}")
        _print_model_effort_lines(payload)
    if "commit_mode" in payload:
        print(f"Commit mode: {payload.get('commit_mode') or 'none'}")
    if payload.get("run_dir") is not None:
        print(f"Run dir: {payload['run_dir']}")
    if payload.get("state_path") is not None:
        print(f"State file: {payload['state_path']}")
    if payload.get("events_path") is not None:
        print(f"Events: {payload['events_path']}")

    if payload.get("dry_run") and payload["ok"]:
        planned = payload.get("planned", {})
        next_pending = planned.get("next_pending")
        if next_pending is None:
            print("Next pending: none")
            print("Dry run: nothing to invoke")
            return
        print(f"Next pending: session {next_pending}")
        print(f"Prompt: {planned['prompt']}")
        print("Argv:")
        for arg in planned["invocation"]["argv"]:
            print(f"  {arg}")
        return

    for session_number in payload["completed_sessions"]:
        print(f"Completed session {session_number}")
    _print_commit_outcome_lines(payload)

    if not payload["ok"]:
        if payload.get("paused"):
            print(f"Run paused: {payload['pause_reason']}", file=sys.stderr)
            if payload.get("pause_message"):
                print(f"Pause detail: {payload['pause_message']}", file=sys.stderr)
        else:
            print(f"Run failed: {payload['reason']}", file=sys.stderr)
        if payload["turns"]:
            turn = payload["turns"][-1]
            print(f"Stdout: {turn['stdout_path']}", file=sys.stderr)
            print(f"Stderr: {turn['stderr_path']}", file=sys.stderr)
            print(f"Last message: {turn['last_message_path']}", file=sys.stderr)
        commit = _last_commit(payload)
        if commit is not None:
            print(f"Commit stdout: {commit['stdout_path']}", file=sys.stderr)
            print(f"Commit stderr: {commit['stderr_path']}", file=sys.stderr)
            print(f"Commit result: {commit['result_path']}", file=sys.stderr)
        return

    final_snapshot = payload["final_snapshot"]
    next_pending = final_snapshot["next_pending"]
    print(f"Run complete: {len(payload['completed_sessions'])} session(s) completed")
    if next_pending is None:
        print("Next pending: none")
    else:
        print(f"Next pending: session {next_pending}")


def run_from_args(args: argparse.Namespace) -> int:
    """Run one or more pending sessions from CLI arguments."""
    try:
        cwd, timeout, max_sessions, commit_mode = _validate_run_inputs(args)
    except (plans.PlanError, RunError) as error:
        payload = _error_payload(
            args,
            reason="input-error",
            message=str(error),
            paused=False,
        )
        if args.json:
            responses.print_json(payload)
        else:
            _print_text_outcome(payload)
        return 1

    try:
        initial_snapshot = plans.inspect_plan(args.plan_dir)
    except plans.PlanError as error:
        payload = _error_payload(
            args,
            reason="malformed-plan",
            message=str(error),
            paused=True,
            cwd=cwd,
        )
        if args.json:
            responses.print_json(payload)
        else:
            _print_text_outcome(payload)
        return 1

    run_id = _utc_run_id()
    try:
        lock_info = state.acquire_lock(initial_snapshot.plan_dir, args.state_dir)
    except state.StateError as error:
        reason, paused = _state_error_reason(error)
        payload = _error_payload(
            args,
            reason=reason,
            message=str(error),
            paused=paused,
            cwd=cwd,
            snapshot=initial_snapshot,
        )
        if args.json:
            responses.print_json(payload)
        else:
            _print_text_outcome(payload)
        return 1

    try:
        try:
            exit_code, payload = _run_locked(
                args=args,
                cwd=cwd,
                timeout=timeout,
                max_sessions=max_sessions,
                commit_mode=commit_mode,
                lock_info=lock_info,
                run_id=run_id,
            )
        finally:
            state.release_lock(initial_snapshot.plan_dir, args.state_dir)
    except (
        config.ConfigError,
        harnesses.HarnessError,
        plans.PlanError,
        RunError,
        state.StateError,
        OSError,
    ) as error:
        payload = _error_payload(
            args,
            reason="run-error",
            message=str(error),
            paused=False,
            cwd=cwd,
            snapshot=initial_snapshot,
        )
        if args.json:
            responses.print_json(payload)
        else:
            _print_text_outcome(payload)
        return 1

    if args.json:
        responses.print_json(payload)
    else:
        _print_text_outcome(payload)
    return exit_code


def _print_doctor_error(
    args: argparse.Namespace,
    *,
    reason: str,
    error: Exception,
    cwd: Path | None = None,
    plan_dir: Path | None = None,
    details: dict[str, Any] | None = None,
) -> int:
    if args.json:
        payload = responses.error_payload(
            "doctor",
            reason=reason,
            error=str(error),
            paused=False,
            details=details,
            harness=args.harness,
            cwd=str(cwd) if cwd is not None else str(args.cwd),
            plan_dir=str(plan_dir) if plan_dir is not None else str(args.plan_dir),
        )
        payload.update(_selected_model_effort_payload(args))
        responses.print_json(payload)
    else:
        print(f"agent-session-loop doctor: {error}", file=sys.stderr)
    return 1


def doctor_from_args(args: argparse.Namespace) -> int:
    """Check local harness prerequisites from CLI arguments."""
    try:
        cwd = plans.canonicalize_path(args.cwd)
        if not cwd.is_dir():
            raise plans.PlanError(f"cwd does not exist or is not a directory: {cwd}")
    except (plans.PlanError, OSError) as error:
        return _print_doctor_error(args, reason="input-error", error=error)

    try:
        snapshot = plans.inspect_plan(args.plan_dir)
    except plans.PlanError as error:
        return _print_doctor_error(
            args,
            reason="malformed-plan",
            error=error,
            cwd=cwd,
        )

    try:
        harness_config = config.harness_config_payload()
        adapter = harnesses.get_adapter(args.harness)
        executable_path = adapter.resolve_executable()
        if executable_path is None:
            raise harnesses.HarnessError(
                f"{args.harness} executable not found on PATH: {adapter.executable}"
            )

        last_message_path = (
            state.plan_state_dir(snapshot.plan_dir) / "last-message-doctor.txt"
        )
        model_effort = _selected_model_effort_payload(args)
        invocation = build_harness_invocation(
            args.harness,
            cwd=cwd,
            plan_dir=snapshot.plan_dir,
            unsafe_bypass=_unsafe_bypass(args),
            last_message_path=last_message_path,
            model=args.model,
            effort=args.effort,
        )
    except config.ConfigError as error:
        return _print_doctor_error(
            args,
            reason="config-error",
            error=error,
            cwd=cwd,
            plan_dir=snapshot.plan_dir,
        )
    except harnesses.HarnessError as error:
        return _print_doctor_error(
            args,
            reason="harness-error",
            error=error,
            cwd=cwd,
            plan_dir=snapshot.plan_dir,
        )
    except OSError as error:
        return _print_doctor_error(
            args,
            reason="os-error",
            error=error,
            cwd=cwd,
            plan_dir=snapshot.plan_dir,
        )

    prompt = build_session_prompt(adapter.name, snapshot.plan_dir)
    if args.json:
        responses.print_json(
            responses.success_payload(
                "doctor",
                reason="ok",
                harness=adapter.name,
                **model_effort,
                harness_config=harness_config,
                executable=str(executable_path),
                resolved_executable=str(executable_path),
                cwd=str(cwd),
                plan_dir=str(snapshot.plan_dir),
                next_pending=(
                    _session_compat_value(snapshot.next_pending.id)
                    if snapshot.next_pending is not None
                    else None
                ),
                next_pending_id=(
                    snapshot.next_pending.id
                    if snapshot.next_pending is not None
                    else None
                ),
                prompt=prompt,
                invocation=_build_invocation_payload(
                    invocation,
                    model_effort=model_effort,
                ),
                invocation_argv=invocation.argv,
            )
        )
        return 0

    print(f"Harness: {adapter.name}")
    _print_model_effort_lines(model_effort)
    print(f"Harness config: {harness_config['path']}")
    configured_harnesses = harness_config["configured_harnesses"]
    if configured_harnesses:
        print(f"Configured harnesses: {', '.join(configured_harnesses)}")
    else:
        print("Configured harnesses: none")
    print(f"Executable: {executable_path}")
    print(f"CWD: {cwd}")
    print(f"Plan: {snapshot.plan_dir}")
    if snapshot.next_pending is None:
        print("Next pending: none")
    else:
        print(f"Next pending: session {snapshot.next_pending.id}")
    print(f"Prompt: {prompt}")
    print("Argv:")
    for arg in invocation.argv:
        print(f"  {arg}")
    return 0
