"""Run-loop orchestration boundaries."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import config, events, harnesses, plans, responses, state


NOT_IMPLEMENTED_EXIT = 2


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
    )


def _utc_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{timestamp}-{os.getpid()}"


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


def _all_sessions_complete(snapshot: plans.PlanSnapshot) -> bool:
    return all(session.status == "complete" for session in snapshot.sessions)


def _validate_run_inputs(args: argparse.Namespace) -> tuple[Path, int | None, int | None]:
    cwd = plans.canonicalize_path(args.cwd)
    if not cwd.is_dir():
        raise plans.PlanError(f"cwd does not exist or is not a directory: {cwd}")

    timeout = args.timeout
    if timeout is not None and timeout <= 0:
        raise RunError("--timeout must be a positive integer")

    max_sessions = args.max_sessions
    if max_sessions is not None and max_sessions <= 0:
        raise RunError("--max-sessions must be a positive integer")

    return cwd, timeout, max_sessions


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _turn_paths(run_dir: Path, *, turn_index: int, session_number: int) -> dict[str, Path]:
    artifact_dir = run_dir / f"turn-{turn_index:04d}-session-{session_number}"
    return {
        "artifact_dir": artifact_dir,
        "stdout": artifact_dir / "stdout.log",
        "stderr": artifact_dir / "stderr.log",
        "last_message": artifact_dir / "last-message.txt",
        "result": artifact_dir / "result.json",
    }


def _build_invocation_payload(invocation: harnesses.HarnessInvocation) -> dict[str, Any]:
    return {
        "name": invocation.name,
        "argv": invocation.argv,
        "cwd": str(invocation.cwd),
        "last_message_path": (
            str(invocation.last_message_path)
            if invocation.last_message_path is not None
            else None
        ),
    }


def _write_turn_artifacts(
    *,
    paths: dict[str, Path],
    result: harnesses.HarnessResult,
    before: plans.PlanSnapshot,
    after: plans.PlanSnapshot | None,
    run_id: str,
    turn_index: int,
    session_number: int,
    plan_error: str | None = None,
) -> dict[str, Any]:
    _write_text(paths["stdout"], result.stdout)
    _write_text(paths["stderr"], result.stderr)
    _write_text(paths["last_message"], result.last_message)

    status_changes = _status_changes(before, after) if after is not None else ()
    pending_completed = _pending_completed(before, after) if after is not None else ()
    runner_signals = _runner_signals(result)
    payload = {
        "run_id": run_id,
        "turn_index": turn_index,
        "session": session_number,
        "artifact_dir": str(paths["artifact_dir"]),
        "harness": result.name,
        "argv": result.argv,
        "returncode": result.returncode,
        "timed_out": result.timed_out,
        "stdout_path": str(paths["stdout"]),
        "stderr_path": str(paths["stderr"]),
        "last_message_path": str(paths["last_message"]),
        "result_path": str(paths["result"]),
        "status_changes": list(status_changes),
        "pending_completed": list(pending_completed),
        "before": _snapshot_payload(before),
        "after": _snapshot_payload(after) if after is not None else None,
    }
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


def _write_state_payload(payload: dict[str, Any]) -> None:
    state_path = payload.get("state_path")
    if not isinstance(state_path, str) or not state_path:
        return
    _write_json_file(Path(state_path), payload)


def _append_event(event_path: Path, name: str, payload: dict[str, Any]) -> None:
    events.append_event(event_path, events.Event(name=name, payload=payload))


def _finish_run(event_path: Path, payload: dict[str, Any]) -> None:
    _append_event(event_path, "run_finished", payload)
    _write_state_payload(payload)


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
            "reason": reason,
            "message": message,
            "details": {} if details is None else details,
        },
    )


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
) -> dict[str, Any]:
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
        plan_dir=str(snapshot.plan_dir),
        cwd=str(cwd),
        harness=args.harness,
        state_dir=str(state_dir),
        run_dir=str(run_dir),
        event_path=str(event_path),
        events_path=str(event_path),
        state_path=str(state_path),
        lock_path=str(lock_info.path),
        harness_config=config.harness_config_payload(),
        completed_sessions=[],
        turns=[],
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
    next_pending = snapshot.next_pending
    if next_pending is None:
        return {
            "next_pending": None,
            "prompt": None,
            "invocation": None,
        }

    paths = _turn_paths(run_dir, turn_index=1, session_number=next_pending.number)
    invocation = build_harness_invocation(
        args.harness,
        cwd=cwd,
        plan_dir=snapshot.plan_dir,
        unsafe_bypass=args.unsafe_bypass,
        last_message_path=paths["last_message"],
    )
    return {
        "next_pending": next_pending.number,
        "prompt": build_session_prompt(args.harness, snapshot.plan_dir),
        "invocation": _build_invocation_payload(invocation),
    }


def _run_locked(
    *,
    args: argparse.Namespace,
    cwd: Path,
    timeout: int | None,
    max_sessions: int | None,
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
    )
    _append_event(
        event_path,
        "run_started",
        {
            "plan_dir": str(snapshot.plan_dir),
            "harness": args.harness,
            "dry_run": args.dry_run,
            "max_sessions": max_sessions,
            "run_id": run_id,
            "run_dir": str(run_dir),
        },
    )
    _write_state_payload(payload)

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
        _finish_run(event_path, payload)
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
            _finish_run(event_path, payload)
            return 1, payload
        payload["reason"] = "all-complete"
        _finish_run(event_path, payload)
        return 0, payload

    if args.dry_run:
        payload["reason"] = "dry-run"
        payload["planned"] = _dry_run_payload(
            args=args,
            snapshot=snapshot,
            cwd=cwd,
            run_dir=run_dir,
        )
        _finish_run(event_path, payload)
        return 0, payload

    completed_count = 0
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
        _append_event(
            event_path,
            "turn_started",
            {
                "run_id": run_id,
                "turn_index": turn_index,
                "session": pending_session.number,
                "artifact_dir": str(paths["artifact_dir"]),
                "prompt": prompt,
                "stdout_path": str(paths["stdout"]),
                "stderr_path": str(paths["stderr"]),
                "last_message_path": str(paths["last_message"]),
                "result_path": str(paths["result"]),
            },
        )
        result = harnesses.run_harness(
            args.harness,
            cwd=cwd,
            plan_dir=before.plan_dir,
            prompt=prompt,
            unsafe_bypass=args.unsafe_bypass,
            last_message_path=paths["last_message"],
            timeout=timeout,
        )
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
            plan_error=plan_error,
        )
        payload["turns"].append(turn_payload)
        _append_event(event_path, "turn_finished", turn_payload)
        _write_state_payload(payload)

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

        payload["completed_sessions"].append(pending_session.number)
        completed_count += 1
        snapshot = after
        payload["final_snapshot"] = _snapshot_payload(snapshot)

    if payload["ok"] and payload["reason"] == "":
        if snapshot.next_pending is None:
            if _all_sessions_complete(snapshot):
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

    _finish_run(event_path, payload)
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
        plan_dir=plan_dir,
        cwd=str(cwd) if cwd is not None else str(getattr(args, "cwd", "")),
        harness=getattr(args, "harness", None),
        completed_sessions=[],
        turns=[],
    )
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


def _print_text_outcome(payload: dict[str, Any]) -> None:
    print(f"Plan: {payload['plan_dir']}")
    if payload.get("harness") is not None:
        print(f"Harness: {payload['harness']}")
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
        cwd, timeout, max_sessions = _validate_run_inputs(args)
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
        responses.print_json(
            responses.error_payload(
                "doctor",
                reason=reason,
                error=str(error),
                paused=False,
                details=details,
                harness=args.harness,
                cwd=str(cwd) if cwd is not None else str(args.cwd),
                plan_dir=str(plan_dir) if plan_dir is not None else str(args.plan_dir),
            )
        )
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
        invocation = build_harness_invocation(
            args.harness,
            cwd=cwd,
            plan_dir=snapshot.plan_dir,
            last_message_path=last_message_path,
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
                invocation=_build_invocation_payload(invocation),
                invocation_argv=invocation.argv,
            )
        )
        return 0

    print(f"Harness: {adapter.name}")
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
