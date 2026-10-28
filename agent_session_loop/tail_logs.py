"""Discover harness logs and replay or incrementally read their output."""

from __future__ import annotations

import os
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


def _read_log_snapshot(
    target: dict[str, Any], *, final: bool = True
) -> tuple[list[str], int]:
    """Replay lines and the offset immediately after the bytes they came from.

    Unless ``final``, an unterminated last line is held back for the follow.
    The offset must come from this read: measuring the file later would skip
    any complete line appended between the replay and the follow.
    """
    path = target["path"]
    if not isinstance(path, Path) or not path.exists():
        return [], 0
    try:
        data = path.read_bytes()
    except OSError:
        return [], 0
    if not final:
        data = data[: data.rfind(b"\n") + 1]
    text = data.decode("utf-8", "replace")
    prefix = _log_prefix(target)
    return [f"{prefix}{line}" for line in text.splitlines()], len(data)


def _read_log_lines(target: dict[str, Any], *, final: bool = True) -> list[str]:
    """Every line a log holds now, for a replay without a follow offset."""
    lines, _ = _read_log_snapshot(target, final=final)
    return lines


def _last_line_end(path: Path) -> int:
    """The offset just past a log's last newline, or 0 when it has none.

    Read backwards a chunk at a time, so a long log costs one chunk and not a
    full read just to find where its last complete line ends.
    """
    chunk_bytes = 64 * 1024
    with path.open("rb") as log_file:
        end = os.fstat(log_file.fileno()).st_size
        while end > 0:
            start = max(0, end - chunk_bytes)
            log_file.seek(start)
            newline = log_file.read(end - start).rfind(b"\n")
            if newline >= 0:
                return start + newline + 1
            end = start
    return 0


def _set_offsets_to_last_line(
    targets: dict[str, dict[str, Any]],
    offsets: dict[str, int],
) -> None:
    """Start following each log just past the last line a replay could print.

    Not at end of file: a line still being written would then be followed from
    its middle, and its first half — held back by the replay — never printed.
    """
    for key, target in targets.items():
        path = target["path"]
        if not isinstance(path, Path) or not path.exists():
            offsets.setdefault(key, 0)
            continue
        try:
            offsets[key] = _last_line_end(path)
        except OSError:
            offsets.setdefault(key, 0)


def _read_new_log_text(
    target: dict[str, Any],
    offsets: dict[str, int],
    *,
    final: bool = False,
) -> list[str]:
    """The complete lines appended to a log since the last read.

    The offset stops after the last newline read, so a line written in two
    appends, or cut inside a multibyte character, is printed once, whole, when
    its newline arrives; ``final`` flushes a last unterminated line once the
    run is over. A log that shrank below the offset was truncated or
    rewritten and is read again from the start. A log replaced whole by a
    longer one (the turn's final stdout write) keeps its offset, so only what
    the replacement added is printed.
    """
    path = target["path"]
    key = _target_key(path)
    if not isinstance(path, Path) or not path.exists():
        offsets.setdefault(key, 0)
        return []

    offset = offsets.get(key, 0)
    try:
        with path.open("rb") as log_file:
            if os.fstat(log_file.fileno()).st_size < offset:
                offset = 0
            log_file.seek(offset)
            data = log_file.read()
    except OSError:
        return []

    if not final:
        data = data[: data.rfind(b"\n") + 1]
    offsets[key] = offset + len(data)
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


def _target_sort_key(target: dict[str, Any]) -> tuple[Any, ...]:
    return (
        _int_sort_key(target.get("turn_index")),
        _session_sort_key(target.get("session_id") or target.get("session")),
        str(target.get("stream")),
    )


def _log_anchors(
    run_records: list[dict[str, Any]],
) -> dict[str, tuple[int, bool]]:
    """Where each log's lines belong among the run's events.

    Log lines carry no timestamps, so a log is placed by the events that name
    it: just before the first ``*_finished`` record that names it (its turn or
    commit wrote the log before finishing), or, while nothing has finished it,
    just after the last record that names it. Maps target key to ``(index,
    before)``.
    """
    anchors: dict[str, tuple[int, bool]] = {}
    finished: set[str] = set()
    for index, record in enumerate(run_records):
        named: dict[str, dict[str, Any]] = {}
        _add_log_targets(named, tail_records._payload(record))
        is_finish = str(record.get("name", "")).endswith("_finished")
        for key in named:
            if key in finished:
                continue
            if is_finish:
                anchors[key] = (index, True)
                finished.add(key)
            else:
                anchors[key] = (index, False)
    return anchors


def _emit_recent(
    *,
    records: list[dict[str, Any]],
    state_payload: dict[str, Any] | None,
    run_id: str | None,
    lines: int,
    out: tail_models.TextSink,
    targets: dict[str, dict[str, Any]],
    offsets: dict[str, int],
    final: bool,
) -> None:
    """Print the run's last ``lines`` of activity and seed ``offsets``.

    ``final`` says nothing will follow this replay, so a log's unterminated
    last line is printed now instead of being left for a follow to complete.
    """
    run_records = tail_records._event_records_for_run(records, run_id)
    targets.update(_discover_targets(run_records, state_payload))
    # Interleave each log with the events of the turn that wrote it, rather
    # than listing every log after every event: the last `lines` lines must
    # still end on the run's outcome, not on the final turn's output.
    anchors = _log_anchors(run_records)
    before: dict[int, list[str]] = {}
    after: dict[int, list[str]] = {}
    trailing: list[str] = []
    for target in sorted(targets.values(), key=_target_sort_key):
        log_lines, offsets[_target_key(target["path"])] = _read_log_snapshot(
            target, final=final
        )
        anchor = anchors.get(_target_key(target["path"]))
        if anchor is None:
            trailing.extend(log_lines)
        else:
            index, is_before = anchor
            (before if is_before else after).setdefault(index, []).extend(log_lines)
    activity: list[str] = []
    for index, record in enumerate(run_records):
        activity.extend(before.get(index, ()))
        activity.append(tail_event_messages._event_line(record))
        activity.extend(after.get(index, ()))
    activity.extend(trailing)

    if activity:
        _print_lines(activity[-lines:], out)
    else:
        _print_lines(["No activity yet."], out)
