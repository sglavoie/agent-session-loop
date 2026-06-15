"""Run-loop orchestration boundaries."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from . import config, events, harnesses, plans, responses, run_control, state
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
    "request": "stop-after-current-requested",
    "cancel": "stop-after-current-canceled",
    "consumed": "stop-after-current-consumed",
}
RUN_CONTROL_EVENT_MESSAGES: dict[str, str] = {
    "request": "stop-after-current requested",
    "cancel": "stop-after-current canceled",
    "consumed": "stop-after-current consumed",
}
AGENT_MUX_FINISH_EVENT = "AgentSessionLoopFinished"
AGENT_MUX_FINISH_SOURCE = "agent_session_loop"
AGENT_MUX_FINISH_REASON = "run_finished"
AGENT_MUX_FINISH_NOTIFY_TIMEOUT_SECONDS = 1.0


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


def _selected_model_effort_payload(args: argparse.Namespace) -> dict[str, str | None]:
    return config.model_effort_payload(
        args.harness,
        model=getattr(args, "model", None),
        effort=getattr(args, "effort", None),
    )


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


def _status_map(snapshot: plans.PlanSnapshot) -> dict[int, plans.StatusValue]:
    return {session.number: session.status for session in snapshot.sessions}


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
    return {
        "number": session.number,
        "status": session.status,
        "path": str(session.path),
        "line_number": session.line_number,
        "raw_value": session.raw_value,
    }


def _snapshot_payload(snapshot: plans.PlanSnapshot) -> dict[str, Any]:
    return {
        "plan_dir": str(snapshot.plan_dir),
        "plan_hash": snapshot.plan_hash,
        "content_sha256": snapshot.content_sha256,
        "path_hash": snapshot.path_hash,
        "counts": _status_counts(snapshot),
        "next_pending": (
            snapshot.next_pending.number if snapshot.next_pending is not None else None
        ),
        "sessions": [_session_payload(session) for session in snapshot.sessions],
    }


def _status_changes(
    before: plans.PlanSnapshot,
    after: plans.PlanSnapshot,
) -> tuple[dict[str, Any], ...]:
    before_statuses = _status_map(before)
    after_statuses = _status_map(after)
    changes: list[dict[str, Any]] = []
    for number in sorted(set(before_statuses) | set(after_statuses)):
        before_value = before_statuses.get(number)
        after_value = after_statuses.get(number)
        if before_value != after_value:
            changes.append(
                {
                    "number": number,
                    "before": before_value,
                    "after": after_value,
                }
            )
    return tuple(changes)


def _pending_completed(
    before: plans.PlanSnapshot,
    after: plans.PlanSnapshot,
) -> tuple[int, ...]:
    before_statuses = _status_map(before)
    after_statuses = _status_map(after)
    return tuple(
        number
        for number in sorted(before_statuses)
        if before_statuses[number] == "pending"
        and after_statuses.get(number) == "complete"
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
    run_dir: Path, *, turn_index: int, session_number: int
) -> dict[str, Path]:
    artifact_dir = run_dir / f"turn-{turn_index:04d}-session-{session_number}"
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
    session_number: int | None,
) -> dict[str, Path]:
    if commit_index <= 0:
        raise RunError("commit_index must be a positive integer")
    if session_number is not None and session_number <= 0:
        raise RunError("session_number must be a positive integer")
    suffix = f"session-{session_number}" if session_number is not None else "end"
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
    pending_completed: tuple[int, ...],
    session_number: int,
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
    expected_change = {
        "number": session_number,
        "before": "pending",
        "after": "complete",
    }
    if status_changes != (expected_change,):
        return "unexpected-status-change"
    if pending_completed == (session_number,):
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
    session_number: int,
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
        session_number=session_number,
        runner_signals=runner_signals,
        plan_error=plan_error,
    )
    payload = {
        "run_id": run_id,
        "turn_index": turn_index,
        "session": session_number,
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
        "pending_completed": list(pending_completed),
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
    temporary_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
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
    session_number: int | None,
) -> dict[str, Any]:
    canonical_cwd = plans.canonicalize_path(cwd)
    paths = _commit_paths(
        run_dir,
        commit_index=commit_index,
        session_number=session_number,
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
    payload = {
        "commit_index": commit_index,
        "scope": "session" if session_number is not None else "end",
        "session": session_number,
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
    session_number: int | None,
) -> dict[str, Any]:
    paths = _commit_paths(
        run_dir,
        commit_index=commit_index,
        session_number=session_number,
    )
    return {
        "run_id": run_id,
        "commit_index": commit_index,
        "scope": "session" if session_number is not None else "end",
        "session": session_number,
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


def _notify_agent_mux_loop_finished(payload: dict[str, Any]) -> None:
    amx_path = shutil.which("amx")
    if amx_path is None:
        return

    hook_payload = {
        "source": AGENT_MUX_FINISH_SOURCE,
        "reason": AGENT_MUX_FINISH_REASON,
        "run_id": payload.get("run_id"),
        "plan_dir": payload.get("plan_dir"),
        "cwd": payload.get("cwd"),
        "run_finished_at": payload.get("run_finished_at"),
    }
    try:
        subprocess.run(
            [amx_path, "hook-indicator", AGENT_MUX_FINISH_EVENT],
            input=json.dumps(hook_payload, separators=(",", ":")),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=AGENT_MUX_FINISH_NOTIFY_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return


def _append_event(event_path: Path, name: str, payload: dict[str, Any]) -> None:
    events.append_event(event_path, events.Event(name=name, payload=payload))


def _initial_control_payload() -> dict[str, Any]:
    return {
        "stop_after_current": False,
        "consumed": False,
        "last_action": None,
    }


def _apply_run_control_action(
    control_payload: dict[str, Any],
    action: run_control.RunControlAction,
) -> str:
    event_action = RUN_CONTROL_EVENT_ACTIONS[action.action]
    control_payload["stop_after_current"] = action.stop_after_current
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
) -> None:
    event_action = _apply_run_control_action(control_payload, action)
    payload["control"] = dict(control_payload)
    event_payload = {
        "run_id": payload.get("run_id"),
        "action": event_action,
        "stop_after_current": action.stop_after_current,
        "message": RUN_CONTROL_EVENT_MESSAGES[action.action],
    }
    if context is not None:
        for key in ("session", "turn_index", "total_sessions"):
            value = context.get(key)
            if value is not None:
                event_payload[key] = value
    _append_event(event_path, events.RUN_CONTROL_EVENT, event_payload)


def _local_timestamp(moment: datetime | None = None) -> str:
    value = datetime.now().astimezone() if moment is None else moment.astimezone()
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _format_local_datetime(value: Any) -> str:
    parsed = _parse_datetime(value)
    if parsed is None:
        return "none"
    return _local_timestamp(parsed)


def _format_session_label(payload: dict[str, Any]) -> str:
    session = payload.get("session")
    total = payload.get("total_sessions")
    if total is None:
        return str(session)
    return f"{session}/{total}"


def _progress_commit_label(commit: dict[str, Any]) -> str:
    scope = commit.get("scope")
    session = commit.get("session")
    if scope == "session" and session is not None:
        return f"session {session}"
    if scope == "end":
        return "end"
    return str(scope or "unknown")


class _ProgressReporter:
    """Human-facing progress output for non-JSON runs."""

    def __init__(self, *, enabled: bool) -> None:
        self.enabled = enabled
        self._lock = threading.Lock()

    def _emit(self, message: str) -> None:
        if not self.enabled:
            return
        with self._lock:
            print(
                f"[{_local_timestamp()}] {message}",
                file=sys.stderr,
                flush=True,
            )

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


class _LiveTurn:
    """Thread-safe live state and heartbeat manager for one harness turn."""

    def __init__(
        self,
        *,
        payload: dict[str, Any],
        event_path: Path,
        progress: _ProgressReporter,
        active_turn: dict[str, Any],
        heartbeat_interval: float = HEARTBEAT_INTERVAL_SECONDS,
    ) -> None:
        self._payload = payload
        self._event_path = event_path
        self._progress = progress
        self._active_turn = dict(active_turn)
        self._heartbeat_interval = heartbeat_interval
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_at = _parse_datetime(active_turn["started_at"]) or _utc_now()
        self._last_activity_at = self._started_at

    def start(self) -> None:
        with self._lock:
            self._payload["active_turn"] = dict(self._active_turn)
            _write_state_payload(self._payload)
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
                self._payload["action_required"] = signal_to_report
                _append_event(
                    self._event_path,
                    events.ACTION_REQUIRED_EVENT,
                    signal_to_report,
                )

            self._payload["active_turn"] = dict(self._active_turn)
            _write_state_payload(self._payload)

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
            self._payload["active_turn"] = None
            _write_state_payload(self._payload)
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
                "total_sessions": self._active_turn.get("total_sessions"),
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
            self._active_turn["last_heartbeat_at"] = heartbeat["created_at"]
            self._active_turn["heartbeat_count"] = heartbeat_count
            self._active_turn["elapsed_seconds"] = heartbeat["elapsed_seconds"]
            self._active_turn["idle_seconds"] = heartbeat["idle_seconds"]
            self._payload["active_turn"] = dict(self._active_turn)
            _append_event(
                self._event_path,
                events.TURN_HEARTBEAT_EVENT,
                heartbeat,
            )
            _write_state_payload(self._payload)
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
            "total_sessions": self._active_turn.get("total_sessions"),
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
        }


def _run_auto_commit(
    *,
    event_path: Path,
    payload: dict[str, Any],
    progress: _ProgressReporter,
    cwd: Path,
    run_dir: Path,
    run_id: str,
    session_number: int | None,
) -> dict[str, Any]:
    commit_index = len(payload["commits"]) + 1
    commit_started = _commit_started_payload(
        run_id=run_id,
        cwd=cwd,
        run_dir=run_dir,
        commit_index=commit_index,
        session_number=session_number,
    )
    _append_event(event_path, events.COMMIT_STARTED_EVENT, commit_started)
    _write_state_payload(payload)
    progress.commit_started(commit_started)

    commit_payload = _run_spec_commit(
        cwd=cwd,
        run_dir=run_dir,
        commit_index=commit_index,
        session_number=session_number,
    )
    payload["commits"].append(commit_payload)
    commit_finished = {"run_id": run_id, **commit_payload}
    _append_event(event_path, events.COMMIT_FINISHED_EVENT, commit_finished)
    _write_state_payload(payload)
    progress.commit_finished(commit_finished)
    return commit_payload


def _finish_run(
    event_path: Path,
    payload: dict[str, Any],
    progress: _ProgressReporter | None = None,
) -> None:
    finished_at = _utc_now()
    payload["run_finished_at"] = _isoformat(finished_at)
    started_at = _parse_datetime(payload.get("run_started_at"))
    payload["duration_seconds"] = (
        _duration_seconds(started_at, finished_at) if started_at is not None else None
    )
    payload["active_turn"] = None
    _append_event(event_path, "run_finished", payload)
    _write_state_payload(payload)
    _notify_agent_mux_loop_finished(payload)
    if progress is not None:
        progress.run_finished(payload)


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
) -> None:
    _set_pause(payload, reason=reason, message=message, details=details)
    _append_event(
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
) -> bool:
    action = listener.consume_stop_after_current()
    if action is None:
        return False
    _record_run_control_action(
        event_path=event_path,
        payload=payload,
        control_payload=control_payload,
        action=action,
        context=context,
    )
    if snapshot.next_pending is None:
        return False
    payload["reason"] = "stopped"
    return True


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
        harness_config=config.harness_config_payload(),
        completed_sessions=[],
        commits=[],
        turns=[],
        control=_initial_control_payload(),
        initial_snapshot=_snapshot_payload(snapshot),
        final_snapshot=_snapshot_payload(snapshot),
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
            "prompt": None,
            "invocation": None,
            **model_effort,
        }

    paths = _turn_paths(run_dir, turn_index=1, session_number=next_pending.number)
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
        "next_pending": next_pending.number,
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
    progress = _ProgressReporter(enabled=not args.json)
    _append_event(
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
    _write_state_payload(payload)
    progress.run_started(payload)

    invalid_pause = _invalid_status_pause(snapshot)
    if invalid_pause is not None:
        reason, message, details = invalid_pause
        payload["final_snapshot"] = _snapshot_payload(snapshot)
        _pause_run(
            event_path,
            payload,
            reason=reason,
            message=message,
            details=details,
        )
        _finish_run(event_path, payload, progress)
        return 1, payload

    if snapshot.next_pending is None:
        if not _all_sessions_complete(snapshot):
            _pause_run(
                event_path,
                payload,
                reason="no-pending-but-incomplete",
                message="no pending session was found, but not every session is complete",
                details={"snapshot": _snapshot_payload(snapshot)},
            )
            _finish_run(event_path, payload, progress)
            return 1, payload
        payload["reason"] = "all-complete"
        _finish_run(event_path, payload, progress)
        return 0, payload

    if args.dry_run:
        payload["reason"] = "dry-run"
        payload["planned"] = _dry_run_payload(
            args=args,
            snapshot=snapshot,
            cwd=cwd,
            run_dir=run_dir,
        )
        _finish_run(event_path, payload, progress)
        return 0, payload

    try:
        if commit_mode == COMMIT_MODE_END:
            _validate_commit_at_end_scope(
                snapshot=snapshot,
                max_sessions=max_sessions,
            )
        if commit_mode is not None:
            payload["commit_preflight"] = _validate_commit_preflight(cwd)
    except RunError as error:
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
        )
        _finish_run(event_path, payload, progress)
        return 1, payload

    run_control_lock = threading.Lock()
    run_control_context: dict[str, Any] | None = None
    control_payload = dict(payload["control"])

    def handle_run_control_action(action: run_control.RunControlAction) -> None:
        if action.action == "consumed":
            return
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
            )

    run_controls = run_control.RunControlListener.for_run(
        args,
        state=run_control.RunControlState(),
        stdin=sys.stdin,
        stderr=sys.stderr,
        on_action=handle_run_control_action,
    )
    run_controls.start()

    completed_count = 0
    previous_turn_finished_at: datetime | None = None
    total_sessions = sum(payload["initial_snapshot"]["counts"].values())
    while snapshot.next_pending is not None:
        if max_sessions is not None and completed_count >= max_sessions:
            payload["reason"] = "max-sessions"
            break

        before = snapshot
        pending_session = before.next_pending
        if pending_session is None:
            break

        turn_index = len(payload["turns"]) + 1
        paths = _turn_paths(
            run_dir,
            turn_index=turn_index,
            session_number=pending_session.number,
        )
        prompt = build_session_prompt(args.harness, before.plan_dir)
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
            "session": pending_session.number,
            "total_sessions": total_sessions,
            "artifact_dir": str(paths["artifact_dir"]),
            "prompt": prompt,
            "model": payload["model"],
            "model_source": payload["model_source"],
            "effort": payload["effort"],
            "effort_source": payload["effort_source"],
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
        )

        def stream_callback(stream_name: harnesses.StreamName, text: str) -> None:
            log_stream_callback(stream_name, text)
            live_turn.record_activity(stream_name, text)

        with run_control_lock:
            run_control_context = dict(active_turn)
        _append_event(event_path, "turn_started", active_turn)
        live_turn.start()
        try:
            result = harnesses.run_harness(
                args.harness,
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
            session_number=pending_session.number,
            live_turn=live_turn_payload,
            plan_error=plan_error,
        )
        payload["turns"].append(turn_payload)
        _append_event(event_path, "turn_finished", turn_payload)
        _write_state_payload(payload)
        progress.turn_finished(turn_payload)

        if result.timed_out:
            payload["final_snapshot"] = (
                _snapshot_payload(after)
                if after is not None
                else _snapshot_payload(before)
            )
            _pause_run(
                event_path,
                payload,
                reason="timeout",
                message=f"harness turn timed out for session {pending_session.number}",
                details={"session": pending_session.number},
            )
            break
        if after is None:
            payload["final_snapshot"] = _snapshot_payload(before)
            _pause_run(
                event_path,
                payload,
                reason="malformed-plan",
                message="plan could not be inspected after the harness turn",
                details={"error": plan_error, "session": pending_session.number},
            )
            break

        invalid_pause = _invalid_status_pause(after)
        if invalid_pause is not None:
            reason, message, details = invalid_pause
            payload["final_snapshot"] = _snapshot_payload(after)
            _pause_run(
                event_path,
                payload,
                reason=reason,
                message=message,
                details=details | {"session": pending_session.number},
            )
            break

        runner_pause = turn_payload.get("runner_pause")
        if runner_pause is not None:
            payload["final_snapshot"] = _snapshot_payload(after)
            _pause_run(
                event_path,
                payload,
                reason="runner-pause",
                message=str(runner_pause),
                details={
                    "session": pending_session.number,
                    "source": turn_payload.get("runner_pause_source"),
                },
            )
            break
        if result.returncode != 0:
            payload["final_snapshot"] = _snapshot_payload(after)
            _pause_run(
                event_path,
                payload,
                reason="nonzero-exit",
                message=(
                    f"harness exited with status {result.returncode} "
                    f"for session {pending_session.number}"
                ),
                details={
                    "session": pending_session.number,
                    "returncode": result.returncode,
                },
            )
            break

        expected_change = (
            {
                "number": pending_session.number,
                "before": "pending",
                "after": "complete",
            },
        )
        status_changes = _status_changes(before, after)
        if not status_changes:
            payload["expected_status_changes"] = list(expected_change)
            payload["observed_status_changes"] = []
            payload["final_snapshot"] = _snapshot_payload(after)
            _pause_run(
                event_path,
                payload,
                reason="no-status-change",
                message=f"session {pending_session.number} did not change status",
                details={
                    "session": pending_session.number,
                    "expected_status_changes": list(expected_change),
                    "observed_status_changes": [],
                },
            )
            break
        if len(status_changes) > 1:
            payload["expected_status_changes"] = list(expected_change)
            payload["observed_status_changes"] = list(status_changes)
            payload["final_snapshot"] = _snapshot_payload(after)
            _pause_run(
                event_path,
                payload,
                reason="multiple-status-changes",
                message="more than one session status changed during one harness turn",
                details={
                    "session": pending_session.number,
                    "expected_status_changes": list(expected_change),
                    "observed_status_changes": list(status_changes),
                },
            )
            break
        if status_changes != expected_change:
            payload["expected_status_changes"] = list(expected_change)
            payload["observed_status_changes"] = list(status_changes)
            payload["final_snapshot"] = _snapshot_payload(after)
            _pause_run(
                event_path,
                payload,
                reason="unexpected-status-change",
                message="the changed session status did not match the expected pending completion",
                details={
                    "session": pending_session.number,
                    "expected_status_changes": list(expected_change),
                    "observed_status_changes": list(status_changes),
                },
            )
            break

        try:
            ledger_updates = plans.sync_derived_status_ledgers(after)
        except plans.PlanError as error:
            payload["final_snapshot"] = _snapshot_payload(after)
            _pause_run(
                event_path,
                payload,
                reason="malformed-plan",
                message="derived status ledgers could not be synced",
                details={
                    "session": pending_session.number,
                    "error": str(error),
                },
            )
            break
        if ledger_updates:
            try:
                after = plans.inspect_plan(before.plan_dir)
            except plans.PlanError as error:
                payload["final_snapshot"] = _snapshot_payload(after)
                _pause_run(
                    event_path,
                    payload,
                    reason="malformed-plan",
                    message="plan could not be inspected after syncing ledgers",
                    details={
                        "session": pending_session.number,
                        "error": str(error),
                    },
                )
                break
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
                session_number=pending_session.number,
            )
            if not commit_payload["success"]:
                payload["final_snapshot"] = _snapshot_payload(after)
                _pause_run(
                    event_path,
                    payload,
                    reason="commit-failed",
                    message=(
                        "auto-commit failed after session "
                        f"{pending_session.number} completed"
                    ),
                    details={
                        "session": pending_session.number,
                        "commit": commit_payload,
                    },
                )
                break

        payload["completed_sessions"].append(pending_session.number)
        completed_count += 1
        snapshot = after
        payload["final_snapshot"] = _snapshot_payload(snapshot)
        with run_control_lock:
            should_stop_after_current = _consume_run_control_stop(
                run_controls,
                snapshot=snapshot,
                payload=payload,
                event_path=event_path,
                control_payload=control_payload,
                context=turn_payload,
            )
        if should_stop_after_current:
            break

    if payload["ok"] and payload["reason"] == "":
        if snapshot.next_pending is None:
            if _all_sessions_complete(snapshot):
                payload["final_snapshot"] = _snapshot_payload(snapshot)
                if commit_mode == COMMIT_MODE_END:
                    commit_payload = _run_auto_commit(
                        event_path=event_path,
                        payload=payload,
                        progress=progress,
                        cwd=cwd,
                        run_dir=run_dir,
                        run_id=run_id,
                        session_number=None,
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
                        )
                    else:
                        payload["reason"] = "all-complete"
                else:
                    payload["reason"] = "all-complete"
            else:
                _pause_run(
                    event_path,
                    payload,
                    reason="no-pending-but-incomplete",
                    message="no pending session was found, but not every session is complete",
                    details={"snapshot": _snapshot_payload(snapshot)},
                )
        else:
            payload["reason"] = "stopped"

    run_controls.stop()
    _finish_run(event_path, payload, progress)
    return (0 if payload["ok"] else 1), payload


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
    session = commit.get("session")
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
                    snapshot.next_pending.number
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
        print(f"Next pending: session {snapshot.next_pending.number}")
    print(f"Prompt: {prompt}")
    print("Argv:")
    for arg in invocation.argv:
        print(f"  {arg}")
    return 0
