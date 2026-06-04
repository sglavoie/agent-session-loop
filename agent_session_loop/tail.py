"""Tail live agent-session-loop state, events, and harness logs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
from typing import Any, TextIO

from . import events, plans, state


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


def _format_seconds(value: Any) -> str:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return "unknown"
    return f"{seconds:.1f}s"


def _event_line(record: dict[str, Any]) -> str:
    name = str(record.get("name") or "unknown")
    payload = _payload(record)
    parts = [f"event {name}"]

    run_id = payload.get("run_id")
    if run_id is not None:
        parts.append(f"run={run_id}")
    session = payload.get("session")
    if session is not None:
        parts.append(f"session={session}")
    turn_index = payload.get("turn_index")
    if turn_index is not None:
        parts.append(f"turn={turn_index}")

    if name == "run_started":
        parts.append(f"harness={payload.get('harness', 'unknown')}")
        parts.append(f"next={payload.get('next_pending', 'none')}")
    elif name == "turn_started":
        parts.append(f"stdout={payload.get('stdout_path', 'unknown')}")
        parts.append(f"stderr={payload.get('stderr_path', 'unknown')}")
    elif name == events.TURN_HEARTBEAT_EVENT:
        parts.append(f"elapsed={_format_seconds(payload.get('elapsed_seconds'))}")
        parts.append(f"idle={_format_seconds(payload.get('idle_seconds'))}")
        parts.append(f"last_activity={payload.get('last_activity_at') or 'none'}")
    elif name == events.ACTION_REQUIRED_EVENT:
        parts.append(f"reason={payload.get('reason', 'unknown')}")
        parts.append(f"source={payload.get('source', 'unknown')}")
        message = payload.get("message")
        if message:
            parts.append(f"detail={message}")
    elif name == "turn_finished":
        parts.append(f"status={payload.get('status_result', 'unknown')}")
        parts.append(f"duration={_format_seconds(payload.get('duration_seconds'))}")
    elif name == events.RUN_PAUSED_EVENT:
        parts.append(f"reason={payload.get('reason', 'unknown')}")
        message = payload.get("message")
        if message:
            parts.append(f"detail={message}")
    elif name == "run_finished":
        status = "ok" if payload.get("ok") else "paused"
        parts.append(f"status={status}")
        parts.append(f"reason={payload.get('reason', 'unknown')}")
        parts.append(f"duration={_format_seconds(payload.get('duration_seconds'))}")

    created_at = record.get("created_at")
    if created_at is not None:
        parts.append(f"time={created_at}")
    return " ".join(str(part) for part in parts)


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
