"""The run's terminal surface: one stderr handle, one lock, one voice.

A live run talks to exactly one place — the operator's terminal — from more
threads than the loop that drives it. The main loop announces sessions,
verification checks and commits; a heartbeat thread stamps liveness while a
turn is still running; the keyboard/control listener echoes what it accepted;
the status key prints a whole snapshot; and the optional inline tail streams a
second run's log lines into the same terminal. Every one of those writes the
*same* process-global handle, and any two of them that write it unserialized
produce a line torn between two threads. This module owns that serialization,
so the loop is left deciding *what* an operator is told rather than
reimplementing *how* two writers share one terminal.

Four things live here:

* **The stderr handle.** :class:`_StderrOutputCoordinator` is the run's single
  output sink and the only holder of ``sys.stderr`` in the run's live output
  path. :class:`_CoordinatedStderrStream` is the file-like proxy it hands to
  collaborators — the control listener and the tail follower — so they write
  through the same lock instead of reaching for the real stream.
* **The progress voice.** :class:`_ProgressReporter` renders every run,
  turn, verification, commit and finish line an operator reads on a non-JSON
  run. It satisfies :class:`~agent_session_loop.turn_execution.TurnProgress`
  structurally, which is how the turn boundary reports without importing the
  loop.
* **The inline tail.** :class:`_InlineTailController` owns the optional
  in-process follower a run starts and stops from the control plane, including
  its bounded stop.
* **The compact status projection.** :func:`_compact_status_lines` and the
  ``_compact_*`` helpers beneath it turn a live run payload, its control block
  and a plan snapshot into the handful of lines the status key prints.

Nothing here imports the runner, and nothing here writes a durable record: this
module is the one boundary in the run whose entire output is ephemeral. The two
collaborators ``commit_settlement`` owns — the payload key terminalization
writes still-owed commit groups under, and the labeller that names one of those
groups — are handed to :class:`_ProgressReporter` by ``run_loop`` at
construction as ``unresolved_groups_key`` and ``commit_group_label``, which is
what lets this boundary render a commit failure without knowing the commit
lifecycle's vocabulary.

Four invariants survive the move and are load-bearing:

* **One sink, one lock.** Every write to the terminal goes through
  :class:`_StderrOutputCoordinator` and takes its single output lock, so a
  progress line, a control echo, a status snapshot and a tail line can never
  interleave inside one another. The lock is held only across the write; no
  caller may hold it across anything that blocks on a plan, a mailbox or a
  process.
* **Buffering is per-thread and line-oriented.**
  :meth:`_StderrOutputCoordinator.write_unprefixed` accumulates a partial line
  against the writing thread's own identity and takes the output lock only once
  a whole line exists, so one thread's half-written line cannot be split by
  another thread's flush.
* **Output never ends a run.** A ``BrokenPipeError`` or ``OSError`` on the sink
  — an escalating helper that exited and abandoned this run's pipe, a closed
  terminal — disables the sink for the rest of the run instead of raising. The
  run's durable truth lives in ``state.json``, the event log and the plan
  files; losing the terminal loses none of it.
* **Projections never mutate.** :func:`_compact_status_lines` and the
  ``_compact_*`` helpers read the payload, the control block and the plan
  snapshot and return text. They take no lock and write nothing, which is what
  makes the status key safe to press from the control-poller thread while the
  loop is mid-turn.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

from . import clock, plans, tail
from .formatting import (
    format_duration_seconds,
    format_turn_finished_message,
    format_turn_started_message,
)

#: How long the inline tail follower is given to wind down before the run stops
#: waiting for it and says so. A tail is an operator convenience; a run must
#: never end late because one would not stop.
INLINE_TAIL_JOIN_TIMEOUT_SECONDS = 1.0
#: How much of the active session's log the inline tail replays when it starts,
#: so enabling it mid-turn shows context rather than the next line only.
INLINE_TAIL_INITIAL_LOG_LINES = 40

#: How :class:`_ProgressReporter` names one commit group in the sentences it
#: prints. The run's own labeller is the commit lifecycle's to own, so it
#: enters at construction rather than being imported.
CommitGroupLabeler = Callable[[Mapping[str, Any]], str]

#: The payload key a run's terminalization writes its still-owed commit groups
#: under. The runner owns the constant every other reader uses; this is the
#: default :class:`_ProgressReporter` falls back to when a caller names none,
#: and ``tests/test_asl_run_reporting.py`` pins the two to the same string.
DEFAULT_UNRESOLVED_COMMIT_GROUPS_KEY = "unresolved_commit_groups"


def default_commit_group_label(summary: Mapping[str, Any]) -> str:
    """Name one commit group the way the run's own summaries name it.

    The reporter needs a label before the commit lifecycle is a module of its
    own, so this is the fallback a caller that names no labeller gets. It reads
    only the two fields any commit-group summary carries; the equivalence
    against the runner's labeller is asserted rather than assumed.
    """
    session_id = summary.get("session_id")
    scope = "end-of-run" if session_id is None else f"session {session_id}"
    alias = summary.get("failed_alias")
    return f"{scope} ({alias})" if alias else scope


def _local_timestamp(moment: datetime | None = None) -> str:
    value = datetime.now().astimezone() if moment is None else moment.astimezone()
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _format_local_datetime(value: Any) -> str:
    parsed = clock.parse_datetime(value)
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
        label = f"session {session}"
    elif scope == "end":
        label = "end"
    else:
        label = str(scope or "unknown")
    alias = commit.get("repository_alias") or commit.get("repo")
    return f"{label}, repository {alias}" if alias else label


def _note_lines(payload: dict[str, Any], *, prefix: str) -> list[str]:
    note = payload.get("note")
    if not isinstance(note, str) or note == "":
        return []
    lines = note.splitlines()
    if not lines:
        lines = [""]
    return [f"{prefix}: {line}" for line in lines]


class _CoordinatedStderrStream:
    """Line-buffering stream proxy that shares the run's output lock.

    Handed to the collaborators that write the terminal on their own schedule
    — the control listener and the inline tail follower — because both accept
    a file-like sink and would otherwise default to the real ``sys.stderr``,
    which is a second unserialized writer racing the coordinator for a line.
    """

    def __init__(self, coordinator: _StderrOutputCoordinator) -> None:
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
    """Shared stderr output path for progress, controls, status, and tails.

    The only holder of ``sys.stderr`` in the run's live output path. An
    explicit ``stream`` replaces it for tests; ``None`` means the process's own
    handle, resolved per write rather than captured at construction so a
    redirection installed after the run started is still honoured.
    """

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
        except OSError, ValueError:
            return False

    def fileno(self) -> int:
        return self._target_stream().fileno()

    def emit_line(self, message: str) -> None:
        if not self.enabled:
            return
        with self._lock:
            try:
                print(
                    f"[{_local_timestamp()}] {message}",
                    file=self._target_stream(),
                    flush=True,
                )
            except BrokenPipeError, OSError:
                # The sink reader is gone (e.g. an escalating helper exited
                # and abandoned this run's pipe). Disable the sink for the
                # rest of the run; durable truth stays in state.json /
                # events.jsonl / plan files.
                self.enabled = False

    def emit_lines(self, messages: list[str]) -> None:
        if not self.enabled:
            return
        with self._lock:
            try:
                for message in messages:
                    print(
                        f"[{_local_timestamp()}] {message}",
                        file=self._target_stream(),
                        flush=True,
                    )
            except BrokenPipeError, OSError:
                self.enabled = False

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
                try:
                    stream = self._target_stream()
                    for line in complete_lines:
                        stream.write(line)
                    stream.flush()
                except BrokenPipeError, OSError:
                    self.enabled = False
        return len(text)

    def flush_thread(self) -> None:
        if not self.enabled:
            return

        thread_id = threading.get_ident()
        with self._buffer_lock:
            buffered = self._thread_buffers.pop(thread_id, "")
        with self._lock:
            try:
                stream = self._target_stream()
                if buffered:
                    stream.write(buffered)
                stream.flush()
            except BrokenPipeError, OSError:
                self.enabled = False


class _ProgressReporter:
    """Human-facing progress output for non-JSON runs.

    Satisfies :class:`~agent_session_loop.turn_execution.TurnProgress`
    structurally, so the turn boundary reports through this object without
    either module importing the other. Every method here is a sentence and
    nothing more: the reporter decides how a fact reads, never whether the
    fact is true, and it is deliberately called outside every lock the run
    holds so a slow terminal can never stall a durable write.
    """

    def __init__(
        self,
        *,
        enabled: bool,
        output: _StderrOutputCoordinator | None = None,
        commit_group_label: CommitGroupLabeler = default_commit_group_label,
        unresolved_groups_key: str = DEFAULT_UNRESOLVED_COMMIT_GROUPS_KEY,
    ) -> None:
        self.enabled = enabled
        self.output = (
            _StderrOutputCoordinator(enabled=enabled) if output is None else output
        )
        # The commit lifecycle's vocabulary enters here rather than being
        # imported: the run owns both the key its terminalization writes owed
        # groups under and the labeller that names one, and this module has no
        # business knowing either.
        self._commit_group_label = commit_group_label
        self._unresolved_groups_key = unresolved_groups_key

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

    def orphaned_state(self, candidates: list[dict[str, Any]]) -> None:
        if not candidates:
            return
        count = len(candidates)
        director = "y" if count == 1 else "ies"
        self._emit(
            f"warning: {count} orphaned state director{director} from a moved "
            "plan match this plan's content"
        )
        for candidate in candidates:
            self._emit(
                f"  {candidate['state_dir']} "
                f"(old plan {candidate['old_plan_dir']}); "
                f"migrate with: {candidate['migrate_command']}"
            )

    def turn_started(self, active_turn: dict[str, Any]) -> None:
        self._emit(format_turn_started_message(active_turn))

    def heartbeat(self, heartbeat: dict[str, Any]) -> None:
        stream = heartbeat.get("last_activity_stream") or "none"
        self._emit(
            f"session {_format_session_label(heartbeat)} heartbeat "
            f"(elapsed {format_duration_seconds(heartbeat['elapsed_seconds'])}, "
            f"idle {format_duration_seconds(heartbeat['idle_seconds'])}, "
            f"last activity "
            f"{_format_local_datetime(heartbeat.get('last_activity_at'))}, "
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
        self._emit(format_turn_finished_message(turn))

    def verification_started(self, payload: dict[str, Any]) -> None:
        check_count = payload.get("check_count")
        plural = "" if check_count == 1 else "s"
        self._emit(
            f"verification started for session {_format_session_label(payload)} "
            f"turn {payload.get('turn_index')} ({check_count} check{plural})"
        )

    def verification_check_started(self, payload: dict[str, Any]) -> None:
        self._emit(
            f"verification check {payload['check_index'] + 1}/"
            f"{payload['check_count']} started: "
            f"{payload['name']} ({payload['kind']})"
        )

    def verification_heartbeat(self, payload: dict[str, Any]) -> None:
        self._emit(
            f"verification heartbeat for session {_format_session_label(payload)} "
            f"check {payload['check_index'] + 1}/{payload['check_count']} "
            f"{payload['name']} "
            f"(elapsed {format_duration_seconds(payload['elapsed_seconds'])}, "
            f"check elapsed "
            f"{format_duration_seconds(payload['check_elapsed_seconds'])})"
        )

    def verification_check_finished(self, payload: dict[str, Any]) -> None:
        self._emit(
            f"verification check {payload['check_index'] + 1}/"
            f"{payload['check_count']} finished: "
            f"{payload['name']} ({payload['kind']}) "
            f"status {payload['status']}, "
            f"duration {format_duration_seconds(payload['duration_seconds'])}"
        )

    def verification_check_skipped(self, payload: dict[str, Any]) -> None:
        dependencies = ", ".join(payload.get("on_failure_of") or [])
        self._emit(
            f"verification check {payload['check_index'] + 1}/"
            f"{payload['check_count']} skipped: "
            f"{payload['name']} ({payload['kind']}), "
            f"condition {payload['reason']}, on failure of {dependencies}"
        )

    def verification_finished(self, payload: dict[str, Any]) -> None:
        outcome = "passed" if payload.get("passed") else "failed"
        skipped = int(payload.get("skipped_count", 0) or 0)
        skipped_suffix = f", {skipped} skipped" if skipped else ""
        gate_count = int(payload.get("gate_count", payload["check_count"]))
        gate_plural = "" if gate_count == 1 else "s"
        self._emit(
            f"verification {outcome} for session {_format_session_label(payload)} "
            f"({payload['passed_count']}/{payload['check_count']} checks passed, "
            f"{gate_count} gate{gate_plural}"
            f"{skipped_suffix}, "
            f"duration {format_duration_seconds(payload['duration_seconds'])})"
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

    def commit_failed(self, record: Mapping[str, Any]) -> None:
        """Report a commit failure the run absorbed rather than paused on."""
        detail = record.get("error") or record.get("message") or "unknown error"
        disposition = (
            "retrying this group at the next commit boundary"
            if record.get("retrying")
            else "leaving these changes uncommitted"
        )
        self._emit(
            f"commit failed: {self._commit_group_label(record)}: {detail}; "
            f"the run continues, {disposition}"
        )

    def commit_group_recovered(self, summary: Mapping[str, Any]) -> None:
        self._emit(f"commit group recovered: {self._commit_group_label(summary)}")

    def run_finished(self, payload: dict[str, Any]) -> None:
        verb = "run finished" if payload["ok"] else "run paused"
        reason = payload.get("pause_reason") or payload.get("reason") or "unknown"
        for line in _note_lines(payload, prefix="note"):
            self._emit(line)
        unresolved = payload.get(self._unresolved_groups_key)
        if isinstance(unresolved, list) and unresolved:
            # A non-fatal warning, printed before the verdict line rather than
            # instead of it: the run really did finish, and it really did lose
            # these commits. Saying only one of the two would be a lie either
            # way, so the summary says both.
            labels = ", ".join(
                self._commit_group_label(summary)
                for summary in unresolved
                if isinstance(summary, Mapping)
            )
            self._emit(
                f"warning: {len(unresolved)} commit group(s) were never "
                f"completed: {labels}; their changes are still uncommitted"
            )
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
                include_events=False,
                initial_log_lines=INLINE_TAIL_INITIAL_LOG_LINES,
                initial_log_scope="active",
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
        return plans.status_counts(snapshot)
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
    if snapshot is not None:
        next_pending = snapshot.next_pending
        return next_pending.id if next_pending is not None else None
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


def _compact_total_duration_seconds(
    payload: dict[str, Any],
    *,
    now: datetime | None = None,
) -> float | None:
    started_at = clock.parse_datetime(payload.get("run_started_at"))
    if started_at is None:
        return None
    finished_at = clock.parse_datetime(payload.get("run_finished_at"))
    if finished_at is None:
        finished_at = clock.utc_now() if now is None else now
    return clock.duration_seconds(started_at, finished_at)


def _compact_status_lines(
    *,
    payload: dict[str, Any],
    control: dict[str, Any],
    snapshot: plans.PlanSnapshot | None,
    plan_error: str | None = None,
    now: datetime | None = None,
) -> list[str]:
    """Build a short live status snapshot without mutating runner state."""

    active_turn = payload.get("active_turn")
    if not isinstance(active_turn, dict):
        active_turn = None

    active_verification = payload.get("active_verification")
    if not isinstance(active_verification, dict):
        active_verification = None

    fallback_snapshot = payload.get("final_snapshot") or payload.get("initial_snapshot")
    if not isinstance(fallback_snapshot, dict):
        fallback_snapshot = None
    counts = _compact_status_counts(snapshot, fallback_snapshot)
    total_sessions = sum(counts.values()) if counts is not None else None
    next_pending_id = _compact_next_pending_id(snapshot, fallback_snapshot)
    turns = payload.get("turns")
    turn_count = len(turns) if isinstance(turns, list) else 0
    next_turn_index = turn_count + 1
    total_duration_seconds = _compact_total_duration_seconds(payload, now=now)

    if counts is None:
        counts_text = "sessions unavailable"
    else:
        counts_text = (
            f"sessions {counts['complete']}/{total_sessions} complete, "
            f"{counts['pending']} pending"
        )

    if active_verification is not None:
        check_index = active_verification.get("current_check_index")
        check_count_value = active_verification.get("check_count")
        if check_index is None:
            check_position = (
                f"-/{check_count_value}" if check_count_value is not None else "-"
            )
        else:
            check_position = (
                f"{int(check_index) + 1}/{check_count_value}"
                if check_count_value is not None
                else str(int(check_index) + 1)
            )
        check_name = active_verification.get("current_check_name") or "pending"
        current_text = (
            f"status: verifying session "
            f"{_format_session_label(active_verification)} "
            f"turn {active_verification.get('turn_index')} "
            f"check {check_position} {check_name}; {counts_text}"
        )
        elapsed = active_verification.get("elapsed_seconds")
        check_elapsed = active_verification.get("check_elapsed_seconds")
        timing_parts = []
        if elapsed is not None:
            timing_parts.append(f"elapsed {format_duration_seconds(elapsed)}")
        if check_elapsed is not None:
            timing_parts.append(
                f"check elapsed {format_duration_seconds(check_elapsed)}"
            )
        if total_duration_seconds is not None:
            timing_parts.append(
                f"total {format_duration_seconds(total_duration_seconds)}"
            )
        if timing_parts:
            current_text += f"; {', '.join(timing_parts)}"
    elif active_turn is not None:
        current_text = (
            f"status: active session {_format_session_label(active_turn)} "
            f"turn {active_turn.get('turn_index')}; {counts_text}"
        )
        elapsed = active_turn.get("elapsed_seconds")
        idle = active_turn.get("idle_seconds")
        timing_parts = []
        if elapsed is not None or idle is not None:
            timing_parts.extend(
                (
                    f"elapsed {format_duration_seconds(elapsed)}",
                    f"idle {format_duration_seconds(idle)}",
                )
            )
        if total_duration_seconds is not None:
            timing_parts.append(
                f"total {format_duration_seconds(total_duration_seconds)}"
            )
        if timing_parts:
            current_text += f"; {', '.join(timing_parts)}"
    elif next_pending_id is not None:
        total_suffix = "" if total_sessions is None else f"/{total_sessions}"
        state_text = "idle" if turn_count else "no active turn"
        current_text = (
            f"status: {state_text}; next session {next_pending_id}{total_suffix} "
            f"turn {next_turn_index}; {counts_text}"
        )
        if total_duration_seconds is not None:
            current_text += f"; total {format_duration_seconds(total_duration_seconds)}"
    else:
        current_text = f"status: no active turn; {counts_text}; next session none"
        if total_duration_seconds is not None:
            current_text += f"; total {format_duration_seconds(total_duration_seconds)}"

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
        f"status: controls stop-after-current {stop_state}; live tail {tail_state}"
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
    lines.extend(_note_lines(payload, prefix="status note"))
    if plan_error is not None:
        lines.append(f"status: plan inspection error: {plan_error}")
    lines.extend([metadata_line, control_line, paths_line])
    return lines
