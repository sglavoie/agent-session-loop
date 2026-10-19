"""Discover harness logs and replay or incrementally read their output."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from . import (
    plans_files,
    plans_session_ids,
    tail_event_messages,
    tail_models,
    tail_records,
)


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
        path = plans_files.canonicalize_path(path_value)
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
        _add_log_targets(targets, tail_records._payload(record))

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
        parsed = plans_session_ids.parse_session_id(value)
    except TypeError, ValueError:
        return ((sys.maxsize,), str(value or ""))
    return (parsed.sort_key, parsed.value)


def _print_lines(lines: list[str], out: tail_models.TextSink) -> None:
    for line in lines:
        print(line, file=out, flush=True)


def _emit_recent(
    *,
    records: list[dict[str, Any]],
    state_payload: dict[str, Any] | None,
    run_id: str | None,
    lines: int,
    out: tail_models.TextSink,
    targets: dict[str, dict[str, Any]],
    offsets: dict[str, int],
) -> None:
    run_records = tail_records._event_records_for_run(records, run_id)
    targets.update(_discover_targets(run_records, state_payload))
    activity = [tail_event_messages._event_line(record) for record in run_records]
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
