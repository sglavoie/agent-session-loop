"""Follow live agent-session-loop state, events, and harness logs."""

from __future__ import annotations

import argparse
import json
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from . import (
    cli_common,
    events,
    plans_errors,
    plans_snapshot,
    responses,
    state,
    tail_event_messages,
    tail_logs,
    tail_models,
    tail_records,
)
from .cli_defaults import program_name
from .cli_parser_support import POSITIVE_INTEGER, flag_refusal
from .tail_control_messages import (
    _ABORT_TERMINAL_ACTIONS,
    _abort_details,
    _abort_grace_remaining,
    _format_local_datetime,
    _format_session_label,
    _mailbox_message,
    _progress_commit_label,
    _run_control_message,
    _run_started_message,
)
from .tail_event_messages import (
    _event_line,
    _event_message,
)
from .tail_logs import (
    _active_targets,
    _add_log_targets,
    _discover_targets,
    _emit_recent,
    _int_sort_key,
    _log_prefix,
    _print_lines,
    _read_log_lines,
    _read_new_log_text,
    _session_sort_key,
    _set_offsets_to_last_line,
    _target_key,
)
from .tail_models import (
    DEFAULT_LINES,
    POLL_INTERVAL_SECONDS,
    TailError,
    TextSink,
)
from .tail_records import (
    _event_records_for_run,
    _latest_run_id,
    _matches_run,
    _payload,
    _read_event_records,
    _read_json_file,
    _record_run_id,
    _state_run_id,
)

__all__ = [
    "follow_events",
    "TailFollower",
    "tail_plan",
    "tail_from_args",
    "_ABORT_TERMINAL_ACTIONS",
    "_abort_details",
    "_abort_grace_remaining",
    "_format_local_datetime",
    "_format_session_label",
    "_mailbox_message",
    "_progress_commit_label",
    "_run_control_message",
    "_run_started_message",
    "_event_line",
    "_event_message",
    "_active_targets",
    "_add_log_targets",
    "_discover_targets",
    "_emit_recent",
    "_int_sort_key",
    "_log_prefix",
    "_print_lines",
    "_read_log_lines",
    "_read_new_log_text",
    "_session_sort_key",
    "_set_offsets_to_last_line",
    "_target_key",
    "DEFAULT_LINES",
    "POLL_INTERVAL_SECONDS",
    "TailError",
    "TextSink",
    "_event_records_for_run",
    "_latest_run_id",
    "_matches_run",
    "_payload",
    "_read_event_records",
    "_read_json_file",
    "_record_run_id",
    "_state_run_id",
]


def _has_run_finished(records: list[dict[str, Any]], run_id: str | None) -> bool:
    for record in reversed(records):
        if record.get("name") == "run_finished" and tail_records._matches_run(
            record, run_id
        ):
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
                except json.JSONDecodeError, RecursionError:
                    continue
                if isinstance(raw, dict):
                    records.append(raw)
        yield records


class _TailFollowLoop:
    def __init__(
        self,
        *,
        state_path: Path,
        batches: Iterator[list[dict[str, Any]]],
        run_id: str | None,
        targets: dict[str, dict[str, Any]],
        offsets: dict[str, int],
        out: tail_models.TextSink,
        poll_interval: float,
        stop_event: threading.Event,
        initial_finished: bool,
        include_events: bool = True,
    ) -> None:
        self._state_path = state_path
        # The `follow_events` generator `tail --json` follows: it survives a
        # truncated or rotated events file and holds back a torn last line.
        self._batches = batches
        self._run_id = run_id
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
        state_payload = tail_records._read_json_file(self._state_path)
        new_records = next(self._batches)
        if self._run_id is None:
            self._run_id = tail_records._latest_run_id(state_payload, new_records)

        run_records = tail_records._event_records_for_run(new_records, self._run_id)
        self._targets.update(tail_logs._discover_targets(run_records, state_payload))

        finished = False
        for record in new_records:
            if not tail_records._matches_run(record, self._run_id):
                continue
            if self._include_events:
                tail_logs._print_lines(
                    [tail_event_messages._event_line(record)], self._out
                )
            tail_logs._add_log_targets(self._targets, tail_records._payload(record))
            if record.get("name") == "run_finished":
                finished = True

        for target in list(self._targets.values()):
            tail_logs._print_lines(
                tail_logs._read_new_log_text(target, self._offsets), self._out
            )

        if finished or _state_run_finished(state_payload):
            for target in list(self._targets.values()):
                tail_logs._print_lines(
                    tail_logs._read_new_log_text(target, self._offsets, final=True),
                    self._out,
                )
            return True
        return False


class TailFollower:
    """Cancellable in-process follower for new events and log text."""

    def __init__(
        self,
        plan_dir: str | Path,
        *,
        state_dir: str | Path | None = None,
        out: tail_models.TextSink | None = None,
        poll_interval: float = tail_models.POLL_INTERVAL_SECONDS,
        include_events: bool = True,
        initial_log_lines: int = 0,
        initial_log_scope: str = "all",
    ) -> None:
        if initial_log_lines < 0:
            raise tail_models.TailError("initial_log_lines must be non-negative")
        if initial_log_scope not in ("all", "active"):
            raise tail_models.TailError("initial_log_scope must be 'all' or 'active'")
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
            raise tail_models.TailError("tail follower is already running")

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
            raise tail_models.TailError("tail follower is already running")

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
        except BaseException as error:  # noqa: BLE001
            self._thread_error = error
        finally:
            self._done_event.set()

    def _build_loop_from_eof(self) -> _TailFollowLoop:
        snapshot = plans_snapshot.inspect_plan(self._plan_dir)
        plan_state = state.plan_state_dir(snapshot.plan_dir, self._state_dir)
        state_path = plan_state / events.STATE_FILE_NAME
        # The first batch is the initial read, so no record lands between
        # reading the journal and starting to follow it.
        events_path = plan_state / events.EVENTS_FILE_NAME
        tail_records._require_readable_events(events_path)
        batches = follow_events(events_path)
        records = next(batches)
        state_payload = tail_records._read_json_file(state_path)
        run_id = tail_records._latest_run_id(state_payload, records)
        run_records = tail_records._event_records_for_run(records, run_id)
        targets = tail_logs._discover_targets(run_records, state_payload)
        offsets: dict[str, int] = {}
        initial_finished = _has_run_finished(records, run_id) or _state_run_finished(
            state_payload
        )
        if self._initial_log_lines > 0:
            self._emit_initial_log_replay(
                state_payload, targets, final=initial_finished
            )
        tail_logs._set_offsets_to_last_line(targets, offsets)
        return _TailFollowLoop(
            state_path=state_path,
            batches=batches,
            run_id=run_id,
            targets=targets,
            offsets=offsets,
            out=self._out,
            poll_interval=self._poll_interval,
            stop_event=self._stop_event,
            initial_finished=initial_finished,
            include_events=self._include_events,
        )

    def _emit_initial_log_replay(
        self,
        state_payload: dict[str, Any] | None,
        targets: dict[str, dict[str, Any]],
        *,
        final: bool,
    ) -> None:
        if self._initial_log_scope == "active":
            replay_targets = tail_logs._active_targets(state_payload)
        else:
            replay_targets = targets
        if not replay_targets:
            return
        lines: list[str] = []
        for target in sorted(
            replay_targets.values(),
            key=lambda value: (
                tail_logs._int_sort_key(value.get("turn_index")),
                tail_logs._session_sort_key(
                    value.get("session_id") or value.get("session")
                ),
                str(value.get("stream")),
            ),
        ):
            lines.extend(tail_logs._read_log_lines(target, final=final))
        if lines:
            tail_logs._print_lines(lines[-self._initial_log_lines :], self._out)


def tail_plan(
    plan_dir: str | Path,
    *,
    state_dir: str | Path | None = None,
    lines: int = tail_models.DEFAULT_LINES,
    follow: bool = True,
    out: tail_models.TextSink = sys.stdout,
    poll_interval: float = tail_models.POLL_INTERVAL_SECONDS,
) -> None:
    """Print recent activity and optionally follow live events/logs."""
    if lines <= 0:
        raise tail_models.TailInputError(
            flag_refusal("--lines", POSITIVE_INTEGER, lines)
        )

    snapshot = plans_snapshot.inspect_plan(plan_dir)
    plan_state = state.plan_state_dir(snapshot.plan_dir, state_dir)
    state_path = plan_state / events.STATE_FILE_NAME
    events_path = plan_state / events.EVENTS_FILE_NAME
    tail_records._require_readable_events(events_path)
    batches = follow_events(events_path)
    records = next(batches)
    state_payload = tail_records._read_json_file(state_path)
    run_id = tail_records._latest_run_id(state_payload, records)
    targets: dict[str, dict[str, Any]] = {}
    offsets: dict[str, int] = {}
    initial_finished = _has_run_finished(records, run_id) or _state_run_finished(
        state_payload
    )

    # A line still being written is left for the follow to print whole; only a
    # replay nothing follows prints it as it stands.
    tail_logs._emit_recent(
        records=records,
        state_payload=state_payload,
        run_id=run_id,
        lines=lines,
        out=out,
        targets=targets,
        offsets=offsets,
        final=not follow or initial_finished,
    )

    if not follow:
        return

    stop_event = threading.Event()
    follow_loop = _TailFollowLoop(
        state_path=state_path,
        batches=batches,
        run_id=run_id,
        targets=targets,
        offsets=offsets,
        out=out,
        poll_interval=poll_interval,
        stop_event=stop_event,
        initial_finished=initial_finished,
    )
    follow_loop.run()


def tail_plan_json(
    plan_dir: str | Path,
    *,
    state_dir: str | Path | None = None,
    lines: int = tail_models.DEFAULT_LINES,
    follow: bool = True,
    out: tail_models.TextSink = sys.stdout,
    poll_interval: float = tail_models.POLL_INTERVAL_SECONDS,
    stop_event: threading.Event | None = None,
) -> None:
    """Print the latest run's event records as JSON lines, then follow them.

    The structured twin of :func:`tail_plan`: one ``events.jsonl`` record per
    line, the last ``lines`` of the run first, then each new one until the run
    finishes. Harness log lines are text and stay with the text form. The
    first batch of :func:`follow_events` is the initial read, so no record
    lands between reading the journal and starting to follow it.
    """
    if lines <= 0:
        raise tail_models.TailInputError(
            flag_refusal("--lines", POSITIVE_INTEGER, lines)
        )

    snapshot = plans_snapshot.inspect_plan(plan_dir)
    plan_state = state.plan_state_dir(snapshot.plan_dir, state_dir)
    state_path = plan_state / events.STATE_FILE_NAME
    events_path = plan_state / events.EVENTS_FILE_NAME
    tail_records._require_readable_events(events_path)
    batches = follow_events(events_path)
    records = next(batches)
    run_id = tail_records._latest_run_id(
        tail_records._read_json_file(state_path), records
    )

    def emit(batch: list[dict[str, Any]]) -> bool:
        finished = False
        for record in batch:
            if not tail_records._matches_run(record, run_id):
                continue
            out.write(json.dumps(record, sort_keys=True) + "\n")
            finished = finished or record.get("name") == "run_finished"
        out.flush()
        return finished

    recent = [record for record in records if tail_records._matches_run(record, run_id)]
    finished = emit(recent[-lines:]) or _has_run_finished(records, run_id)
    stop = stop_event or threading.Event()
    while follow and not finished and not stop.is_set():
        if stop.wait(poll_interval):
            break
        finished = emit(next(batches)) or _state_run_finished(
            tail_records._read_json_file(state_path)
        )


def tail_from_args(args: argparse.Namespace) -> int:
    """Tail plan activity from CLI arguments."""
    as_json = bool(getattr(args, "json", False))
    if not cli_common.resolve_plan_dir_argument(
        args, command_name="tail", text_label="tail"
    ):
        return 1
    try:
        (tail_plan_json if as_json else tail_plan)(
            args.plan_dir,
            state_dir=args.state_dir,
            lines=args.lines,
            follow=not args.no_follow,
        )
    except (plans_errors.PlanError, tail_models.TailError, OSError) as error:
        if as_json:
            responses.print_json(
                responses.error_payload(
                    "tail",
                    reason=(
                        "plan-not-found"
                        if isinstance(error, plans_errors.PlanNotFoundError)
                        else "malformed-plan"
                        if isinstance(error, plans_errors.PlanError)
                        else "input-error"
                        if isinstance(error, tail_models.TailInputError)
                        else "tail-error"
                        if isinstance(error, tail_models.TailError)
                        else "os-error"
                    ),
                    error=str(error),
                )
            )
        else:
            print(f"{program_name()} tail: {error}", file=sys.stderr)
        return 1
    return 0
