"""Tail live agent-session-loop state, events, and harness logs."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
import time
from typing import Any, TextIO

from . import events, plans, state
from .formatting import format_duration_seconds


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
    session = payload.get("session")
    if scope == "session" and session is not None:
        return f"session {session}"
    if scope == "end":
        return "end"
    return str(scope or "unknown")


def _format_session_label(payload: dict[str, Any]) -> str:
    session = payload.get("session", "unknown")
    total = payload.get("total_sessions")
    if total is None:
        return str(session)
    return f"{session}/{total}"


def _run_started_message(payload: dict[str, Any]) -> str:
    run_id = payload.get("run_id", "unknown")
    harness = payload.get("harness", "unknown")
    next_pending = payload.get("next_pending")
    next_label = "none" if next_pending is None else f"session {next_pending}"
    parts = [
        f"run started: harness {harness}",
        f"run {run_id}",
    ]

    counts = payload.get("counts")
    if isinstance(counts, dict):
        total = 0
        for value in counts.values():
            try:
                total += int(value)
            except (TypeError, ValueError):
                pass
        complete = counts.get("complete", "unknown")
        pending = counts.get("pending", "unknown")
        parts.append(f"{complete}/{total} sessions complete")
        parts.append(f"{pending} pending")

    parts.append(f"next {next_label}")

    approval = payload.get("approval_mode")
    if approval is not None:
        parts.append(f"approval {approval}")

    run_dir = payload.get("run_dir")
    if run_dir is not None:
        parts.append(f"run dir {run_dir}")

    return "; ".join(str(part) for part in parts)


def _run_control_message(payload: dict[str, Any]) -> str:
    message = payload.get("message") or payload.get("action") or "unknown action"
    status = "on" if payload.get("stop_after_current") else "off"
    details = [f"stop-after-current {status}"]
    session = payload.get("session")
    if session is not None:
        details.append(f"session {_format_session_label(payload)}")
    turn_index = payload.get("turn_index")
    if turn_index is not None:
        details.append(f"turn {turn_index}")
    return f"run control: {message} ({', '.join(details)})"


def _event_message(name: str, payload: dict[str, Any]) -> str:
    if name == "run_started":
        return _run_started_message(payload)
    if name == "turn_started":
        return f"session {_format_session_label(payload)} started"
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
    if name == "turn_finished":
        return (
            f"session {_format_session_label(payload)} finished "
            f"(status {payload.get('status_result', 'unknown')}, "
            f"return code {payload.get('returncode', 'unknown')}, "
            f"duration {format_duration_seconds(payload.get('duration_seconds'))})"
        )
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
    if name == events.RUN_CONTROL_EVENT:
        return _run_control_message(payload)
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
    session = payload.get("session")
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
                "session": payload.get("session"),
                "turn_index": payload.get("turn_index"),
                "commit_index": payload.get("commit_index"),
                "scope": payload.get("scope"),
            },
        )


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
            int(value.get("turn_index") or 0),
            int(value.get("session") or 0),
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

    seen_event_count = len(records)
    finished = _has_run_finished(records, run_id) or _state_run_finished(state_payload)

    while not finished:
        state_payload = _read_json_file(state_path)
        current_records = _read_event_records(events_path)
        if run_id is None:
            run_id = _latest_run_id(state_payload, current_records)

        run_records = _event_records_for_run(current_records, run_id)
        targets.update(_discover_targets(run_records, state_payload))

        new_records = current_records[seen_event_count:]
        for record in new_records:
            if not _matches_run(record, run_id):
                continue
            _print_lines([_event_line(record)], out)
            _add_log_targets(targets, _payload(record))
            if record.get("name") == "run_finished":
                finished = True
        seen_event_count = len(current_records)

        for target in list(targets.values()):
            _print_lines(_read_new_log_text(target, offsets), out)

        if finished or _state_run_finished(state_payload):
            for target in list(targets.values()):
                _print_lines(_read_new_log_text(target, offsets), out)
            break

        time.sleep(poll_interval)


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
