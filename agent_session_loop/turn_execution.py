"""The typed boundary between the run loop and one executed turn.

A turn is the run's only irreversible act: it launches an agent against the
plan, watches it, and writes the artifacts every later guard, audit and
supervisor reads. This module owns that act end to end -- launch, heartbeat,
hang detection, process result, artifact capture and the two terminal
transitions (publication and rejection) -- so the run loop is left deciding
*whether* a turn may proceed rather than reimplementing *how* one runs.

Every transition hands back a frozen record (:class:`TurnExecution`,
:class:`TurnFinalization`, :class:`TurnCommit`) instead of mutating the
caller's dictionaries in place, so what a phase produced is readable from its
return value rather than reconstructed from whichever keys appeared.

Nothing here imports the runner. The two collaborators ``run_loop`` owns --
serialized persistence and human-facing progress -- enter through the
:class:`TurnPersistence` and :class:`TurnProgress` protocols, which is what
lets the boundary be typed without a cycle.

Two ordering rules survive the move and are load-bearing:

* ``LiveTurn._lock`` is always taken before ``TurnPersistence.lock``, never the
  reverse, and neither is held across a ``ps`` sample or an AgentMux
  subprocess.
* :func:`commit_turn` is the only emitter of ``turn_finished``; it writes
  ``result.json`` and the run state before the event so the event can never
  name a result that disagrees with what is on disk.
"""

from __future__ import annotations

import contextlib
import math
import os
import threading
import time
from collections.abc import Callable, Mapping, MutableMapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol

from . import (
    agent_mux_delivery,
    clock,
    config,
    durability,
    events,
    guidance_delivery,
    harness_failures,
    harnesses,
    mailbox,
    plans,
    process_tree,
)

INTERRUPTED_EXIT = 130
HEARTBEAT_INTERVAL_SECONDS = 30.0
HANG_TRIGGER_IDLE_SECONDS = 120.0
HANG_CONFIRM_SAMPLES = 2
HANG_CPU_EPSILON_SECONDS = 0.5
HANG_RESULT_GRACE_SECONDS = 45.0
HANG_TRIGGER_IDLE_ENV_VAR = "ASL_HANG_TRIGGER_IDLE_SECONDS"
HANG_CONFIRM_SAMPLES_ENV_VAR = "ASL_HANG_CONFIRM_SAMPLES"
HANG_RESULT_GRACE_ENV_VAR = "ASL_HANG_RESULT_GRACE_SECONDS"
HANG_REASON_RESULT_RECEIVED = "result_received_process_alive"
HANG_REASON_ZERO_CPU = "zero_cpu_idle"
LIVE_CONTROL_POLL_INTERVAL_SECONDS = 5.0
# Streams whose control markers the runner is willing to act on. The agent's
# final message and stdout are what the agent chose to say; stderr is a
# transcript of everything that happened to it.
TRUSTED_RUNNER_SIGNAL_SOURCES: tuple[str, ...] = ("last_message", "stdout")
RUNNER_STATUS_SOURCE_ABSENT = "absent"
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
RESULT_SCHEMA_VERSION = 2


class TurnPersistence(Protocol):
    """The run's serialized payload writer, as the turn boundary needs it.

    Declared structurally rather than imported so this module never depends on
    the runner that owns the concrete implementation. ``lock`` is the run's
    reentrant mutex: every state write and event append below is taken under
    it, and it is always acquired *after* ``LiveTurn._lock``.
    """

    @property
    def lock(self) -> AbstractContextManager[Any]: ...

    def append_event(
        self, event_path: Path, name: str, payload: dict[str, Any]
    ) -> None: ...

    def write_state(self, payload: dict[str, Any]) -> None: ...


class TurnProgress(Protocol):
    """The run's human-facing progress reporter, as the turn boundary calls it.

    Progress output is deliberately outside every lock the turn holds, so a
    slow terminal can never stall a state write.
    """

    def turn_started(self, active_turn: dict[str, Any]) -> None: ...

    def turn_finished(self, turn: dict[str, Any]) -> None: ...

    def heartbeat(self, heartbeat: dict[str, Any]) -> None: ...

    def action_required(self, signal: dict[str, Any]) -> None: ...


def notify_turn_started(
    payload: dict[str, Any],
    active_turn: dict[str, Any],
    persistence: TurnPersistence | None = None,
) -> None:
    agent_mux_delivery.notify_loop(
        agent_mux_delivery.TURN_START_EVENT,
        payload,
        reason=agent_mux_delivery.TURN_START_REASON,
        active_turn=active_turn,
        persistence=persistence,
    )


def notify_turn_heartbeat(
    payload: dict[str, Any],
    active_turn: dict[str, Any],
    persistence: TurnPersistence | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> None:
    agent_mux_delivery.notify_loop(
        agent_mux_delivery.TURN_HEARTBEAT_EVENT,
        payload,
        reason=agent_mux_delivery.TURN_HEARTBEAT_REASON,
        active_turn=active_turn,
        persistence=persistence,
        cancelled=cancelled,
    )


def notify_turn_finished(
    payload: dict[str, Any],
    finished_turn: dict[str, Any],
    persistence: TurnPersistence | None = None,
) -> None:
    agent_mux_delivery.notify_loop(
        agent_mux_delivery.TURN_FINISH_EVENT,
        payload,
        reason=agent_mux_delivery.TURN_FINISH_REASON,
        active_turn=finished_turn,
        persistence=persistence,
    )


def notify_turn_hang_suspected(
    payload: dict[str, Any],
    active_turn: dict[str, Any],
    *,
    hang_reason: str,
    hang_idle_seconds: float | None,
    harness_pid: int | None,
    persistence: TurnPersistence | None = None,
) -> None:
    # Bounded scalars only; the sampled process tree stays in
    # state.json / events.jsonl rather than riding the notification.
    agent_mux_delivery.notify_loop(
        agent_mux_delivery.TURN_HANG_SUSPECTED_EVENT,
        payload,
        reason=agent_mux_delivery.TURN_HANG_SUSPECTED_REASON,
        active_turn=active_turn,
        extra={
            "hang_reason": hang_reason,
            "hang_idle_seconds": hang_idle_seconds,
            "harness_pid": harness_pid,
        },
        persistence=persistence,
    )


def notify_turn_hang_cleared(
    payload: dict[str, Any],
    active_turn: dict[str, Any],
    *,
    hang_reason: str,
    hang_idle_seconds: float | None,
    harness_pid: int | None,
    persistence: TurnPersistence | None = None,
) -> None:
    agent_mux_delivery.notify_loop(
        agent_mux_delivery.TURN_HANG_CLEARED_EVENT,
        payload,
        reason=agent_mux_delivery.TURN_HANG_CLEARED_REASON,
        active_turn=active_turn,
        extra={
            "hang_reason": hang_reason,
            "hang_idle_seconds": hang_idle_seconds,
            "harness_pid": harness_pid,
        },
        persistence=persistence,
    )


def reply_argv_placeholder(reply_length: int) -> str:
    return f"<asl reply prompt redacted: {reply_length} chars>"


def sanitize_reply_argv(
    argv: list[str] | tuple[str, ...],
    *,
    full_prompt: str,
    reply_length: int,
) -> list[str]:
    placeholder = reply_argv_placeholder(reply_length)
    return [placeholder if token == full_prompt else token for token in argv]


def resolution_argv_placeholder(
    action: mailbox.ResolutionAction | str,
    note_length: int | None,
) -> str:
    note_label = "omitted" if note_length is None else f"{note_length} chars"
    return f"<asl resolution prompt redacted: action={action}, note={note_label}>"


def sanitize_resolution_argv(
    argv: list[str] | tuple[str, ...],
    *,
    full_prompt: str,
    action: mailbox.ResolutionAction | str,
    note_length: int | None,
) -> list[str]:
    placeholder = resolution_argv_placeholder(action, note_length)
    return [placeholder if token == full_prompt else token for token in argv]


def persisted_result_argv(
    argv: list[str] | tuple[str, ...],
    *,
    live_turn: Mapping[str, Any],
    unredacted_prompt: str | None,
) -> list[str] | tuple[str, ...]:
    """Redact reply and resolution prompts before persisting result argv."""
    if unredacted_prompt is None:
        return argv

    persisted_argv: list[str] = [
        "<asl prompt redacted>" if token == unredacted_prompt else token
        for token in argv
    ]
    prompt_kind = live_turn.get("prompt_kind")
    reply_source = live_turn.get("reply_source")
    reply_length = (
        reply_source.get("reply_length") if isinstance(reply_source, dict) else None
    )
    if prompt_kind == "reply" and type(reply_length) is int:
        return sanitize_reply_argv(
            argv,
            full_prompt=unredacted_prompt,
            reply_length=reply_length,
        )

    resolution_source = live_turn.get("resolution_source")
    action = (
        resolution_source.get("action") if isinstance(resolution_source, dict) else None
    )
    note_length = (
        resolution_source.get("note_length")
        if isinstance(resolution_source, dict)
        else None
    )
    if (
        prompt_kind == "resolution"
        and action in mailbox.RESOLUTION_ACTIONS
        and (note_length is None or type(note_length) is int)
    ):
        return sanitize_resolution_argv(
            argv,
            full_prompt=unredacted_prompt,
            action=action,
            note_length=note_length,
        )
    return persisted_argv


def signal_line_candidates(line: str) -> list[str]:
    stripped = line.strip()
    candidates = [stripped]
    for rendered_prefix in ("claude text: ",):
        if stripped.startswith(rendered_prefix):
            candidates.append(stripped[len(rendered_prefix) :].strip())
    return candidates


def extract_prefixed_line(text: str, prefix: str) -> str | None:
    for line in text.splitlines():
        for candidate in signal_line_candidates(line):
            if candidate.startswith(prefix):
                return candidate[len(prefix) :].strip()
    return None


def is_noop_runner_pause(message: str) -> bool:
    """Return True only for the exact no-op ``RUNNER_PAUSE: none`` marker.

    Agents are documented never to emit ``RUNNER_PAUSE: none``, but a stray
    placeholder must not fail closed into a real pause. The match is
    case-insensitive on the trimmed detail and matches nothing else, so every
    genuine pause reason keeps its fail-closed behavior.
    """
    return message.strip().casefold() == "none"


def runner_signal_lines(text: str) -> list[tuple[str, str, str]]:
    signals: list[tuple[str, str, str]] = []
    for line in text.splitlines():
        for candidate in signal_line_candidates(line):
            if candidate.startswith(events.RUNNER_STATUS_PREFIX):
                signals.append(
                    (
                        "status",
                        candidate[len(events.RUNNER_STATUS_PREFIX) :].strip(),
                        candidate,
                    )
                )
                break
            if candidate.startswith(events.RUNNER_PAUSE_PREFIX):
                message = candidate[len(events.RUNNER_PAUSE_PREFIX) :].strip()
                if is_noop_runner_pause(message):
                    break
                signals.append(("pause", message, candidate))
                break
    return signals


def signal_source_trusted(source: str) -> bool:
    """Whether a stream may drive runner control state rather than only inform.

    ``stderr`` carries echoed prompts, tool arguments and file diffs, so a
    control marker seen there attests to nothing about what the agent decided.
    Both the post-turn signal read and the live stream watch route their source
    decision through here so the two cannot disagree about which stream is
    control and which is transcript.
    """
    return source in TRUSTED_RUNNER_SIGNAL_SOURCES


def runner_signals(result: harnesses.HarnessResult) -> dict[str, Any]:
    degraded: dict[str, Any] | None = None
    for source, text in (
        ("last_message", result.last_message),
        ("stdout", result.stdout),
        ("stderr", result.stderr),
    ):
        source_signals = runner_signal_lines(text)
        if not source_signals:
            continue
        signal_kind, message, line = source_signals[-1]
        if not signal_source_trusted(source):
            degraded = {
                "source": source,
                "kind": signal_kind,
                "preview": preview_text(line),
            }
            continue
        signals: dict[str, Any] = {}
        if signal_kind == "status":
            signals["runner_status"] = message
            signals["runner_status_source"] = source
            if message.strip().lower() in {"pause", "paused"}:
                signals["runner_pause"] = "Runner status reported paused"
                signals["runner_pause_source"] = source
        else:
            signals["runner_pause"] = message
            signals["runner_pause_source"] = source
        return signals

    # Absence is itself evidence: an explicit record says the trusted streams
    # were read and carried no marker, and a marker refused on an untrusted
    # stream is demoted beside it rather than dropped.
    absent: dict[str, Any] = {"runner_status_source": RUNNER_STATUS_SOURCE_ABSENT}
    if degraded is not None:
        absent["runner_status_degraded"] = degraded
    return absent


def runner_pause_reason(message: str) -> str:
    """Resolve a structured RUNNER_PAUSE reason, preserving legacy fallback."""
    text = message.strip()
    for separator in (" - ", ": "):
        prefix, found, _detail = text.partition(separator)
        if found and prefix in events.PAUSE_REASONS:
            return prefix
    return "runner-pause"


def preview_text(text: str, *, limit: int = 160) -> str:
    collapsed = " ".join(text.strip().split())
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[: limit - 3]}..."


def approval_match_is_content(line: str, position: int) -> bool:
    """Whether an approval phrase is text the agent wrote, not a prompt to it.

    Workers are launched unable to ask: codex gets ``-s workspace-write`` and
    claude ``--permission-mode dontAsk``. So a session editing approval-handling
    code streams the phrase far more often than a harness ever asks for one, and
    a bare substring match reads the agent's own diff as an interrupted run. A
    real prompt stands on its own line; a phrase inside a string literal or
    behind a diff marker is content the turn produced.
    """
    if line.lstrip()[:1] in {"+", "-", ">"}:
        return True
    prefix = line[:position]
    return prefix.count('"') % 2 == 1 or prefix.count("'") % 2 == 1


def live_action_signal(
    stream_name: harnesses.StreamName,
    text: str,
) -> dict[str, Any] | None:
    # Only the prefix branch is source-gated: an approval prompt on stderr is a
    # report about the harness, not an assertion the agent made about itself.
    if signal_source_trusted(stream_name):
        pause = extract_prefixed_line(text, events.RUNNER_PAUSE_PREFIX)
        if pause is not None and not is_noop_runner_pause(pause):
            return {
                "reason": "runner-pause",
                "message": pause,
                "source": stream_name,
                "matched": events.RUNNER_PAUSE_PREFIX,
            }

        status = extract_prefixed_line(text, events.RUNNER_STATUS_PREFIX)
        if status is not None and status.strip().lower() in {"pause", "paused"}:
            return {
                "reason": "runner-status-paused",
                "message": status,
                "source": stream_name,
                "matched": events.RUNNER_STATUS_PREFIX,
            }

    for line in text.splitlines():
        lowered = line.lower()
        for pattern in APPROVAL_SIGNAL_PATTERNS:
            position = lowered.find(pattern)
            if position < 0 or approval_match_is_content(line, position):
                continue
            return {
                "reason": "approval-required",
                "message": preview_text(text),
                "source": stream_name,
                "matched": pattern,
            }

    return None


#: The reasons :func:`live_action_signal` can produce. Each names an
#: intervention the agent asked for mid-turn, so each is only current while the
#: run is still stopped on it. A reason the runner itself declared — review
#: findings above all — is not a live signal and is never cleared here. The set
#: lives in :mod:`agent_session_loop.events` because the supervisor audit
#: retires the same reasons on the bundle's status projection.
LIVE_ACTION_REQUIRED_REASONS = events.LIVE_ACTION_REQUIRED_REASONS


def clear_live_action_required(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Drop a live action signal the run has already moved past.

    ``action_required`` on the run root is the current unresolved intervention,
    not the run's history: the turn result keeps the historical record. Starting
    another turn or finishing unpaused both prove the signal was answered, so
    leaving it set would make a terminal, unpaused run report a pending ask.
    Returns the cleared record, or ``None`` when nothing was cleared.
    """

    current = payload.get("action_required")
    if not isinstance(current, Mapping):
        return None
    if current.get("reason") not in LIVE_ACTION_REQUIRED_REASONS:
        return None
    payload["action_required"] = None
    return dict(current)


def prepare_stream_logs(paths: dict[str, Path]) -> harnesses.StreamCallback:
    def stream_callback(stream_name: harnesses.StreamName, text: str) -> None:
        durability.append_text(paths[stream_name], text)

    return stream_callback


def create_turn_logs(paths: dict[str, Path]) -> None:
    """Materialize the turn's empty logs at the launch boundary.

    Created any earlier, a pause from one of the pre-launch guards leaves
    behind a turn directory holding nothing but two empty logs, which reads
    downstream as a turn that ran and wrote no `result.json`.
    """
    durability.write_text(paths["stdout"], "")
    durability.write_text(paths["stderr"], "")


def live_stream_callback(
    log_stream_callback: harnesses.StreamCallback,
    live_turn: LiveTurn,
) -> harnesses.StreamCallback:
    def stream_callback(stream_name: harnesses.StreamName, text: str) -> None:
        log_stream_callback(stream_name, text)
        live_turn.record_activity(stream_name, text)

    return stream_callback


def turn_paths(run_dir: Path, *, turn_index: int, session_id: str) -> dict[str, Path]:
    artifact_dir = (
        run_dir / f"turn-{turn_index:04d}-{plans.session_path_label(session_id)}"
    )
    return {
        "artifact_dir": artifact_dir,
        "stdout": artifact_dir / "stdout.log",
        "stderr": artifact_dir / "stderr.log",
        "last_message": artifact_dir / "last-message.txt",
        "result": artifact_dir / "result.json",
    }


def turn_status_result(
    *,
    result: harnesses.HarnessResult,
    status_changes: tuple[dict[str, Any], ...],
    pending_completed: tuple[str, ...],
    session_id: str,
    runner_signals: dict[str, Any],
    plan_error: str | None,
) -> str:
    if result.interrupted:
        return "interrupted"
    if result.timed_out:
        return "timeout"
    if plan_error is not None:
        return "malformed-plan"
    runner_pause = runner_signals.get("runner_pause")
    if runner_pause is not None:
        return runner_pause_reason(str(runner_pause))
    if result.returncode != 0:
        return harness_failures.classify_harness_failure(result).reason
    if not status_changes:
        return "no-status-change"
    if len(status_changes) > 1:
        return "multiple-status-changes"
    expected_change = plans.session_status_change_payload(
        session_id,
        before="pending",
        after="complete",
    )
    if status_changes != (expected_change,):
        return "unexpected-status-change"
    if pending_completed == (session_id,):
        return "complete"
    return "unknown"


def observed_model_payload(observed_model: str | None) -> dict[str, str | None]:
    """The model the harness reported executing, beside the requested one."""
    return {
        "observed_model": observed_model,
        "observed_model_source": (
            None if observed_model is None else config.MODEL_SOURCE_HARNESS_REPORTED
        ),
    }


def write_turn_artifacts(
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
    unredacted_prompt: str | None = None,
) -> dict[str, Any]:
    durability.write_text(paths["stdout"], result.stdout)
    durability.write_text(paths["stderr"], result.stderr)
    durability.write_text(paths["last_message"], result.last_message)

    status_changes = plans.status_changes(before, after) if after is not None else ()
    pending_completed = (
        plans.pending_completed(before, after) if after is not None else ()
    )
    signals = runner_signals(result)
    status_result = turn_status_result(
        result=result,
        status_changes=status_changes,
        pending_completed=pending_completed,
        session_id=session_id,
        runner_signals=signals,
        plan_error=plan_error,
    )
    persisted_argv = persisted_result_argv(
        result.argv,
        live_turn=live_turn,
        unredacted_prompt=unredacted_prompt,
    )
    payload = {
        "version": RESULT_SCHEMA_VERSION,
        "run_id": run_id,
        "turn_index": turn_index,
        **plans.session_identity_payload(session_id),
        "session_kind": live_turn.get(
            "session_kind", plans.SESSION_KIND_IMPLEMENTATION
        ),
        "total_sessions": live_turn.get("total_sessions"),
        "artifact_dir": str(paths["artifact_dir"]),
        "harness": result.name,
        "model": live_turn.get("model"),
        "model_source": live_turn.get("model_source"),
        **observed_model_payload(
            result.observed_model or live_turn.get("observed_model")
        ),
        "effort": live_turn.get("effort"),
        "effort_source": live_turn.get("effort_source"),
        "spark_route": live_turn.get("spark_route"),
        "argv": persisted_argv,
        "returncode": result.returncode,
        "timed_out": result.timed_out,
        "interrupted": result.interrupted,
        "stdout_path": str(paths["stdout"]),
        "stderr_path": str(paths["stderr"]),
        "last_message_path": str(paths["last_message"]),
        "result_path": str(paths["result"]),
        "status_changes": list(status_changes),
        "pending_completed": [
            plans.session_compat_value(completed_id)
            for completed_id in pending_completed
        ],
        "pending_completed_ids": list(pending_completed),
        "status_result": status_result,
        "started_at": live_turn["started_at"],
        "finished_at": live_turn["finished_at"],
        "duration_seconds": live_turn["duration_seconds"],
        "total_duration_seconds": live_turn.get("total_duration_seconds"),
        "idle_seconds_since_previous_turn": live_turn[
            "idle_seconds_since_previous_turn"
        ],
        "last_activity_at": live_turn.get("last_activity_at"),
        "last_activity_stream": live_turn.get("last_activity_stream"),
        "last_activity_preview": live_turn.get("last_activity_preview"),
        "last_heartbeat_at": live_turn.get("last_heartbeat_at"),
        "heartbeat_count": live_turn.get("heartbeat_count", 0),
        "before": plans.snapshot_payload(before),
        "after": plans.snapshot_payload(after) if after is not None else None,
    }
    if result.stream_io is not None:
        payload["stream_io"] = dict(result.stream_io)
    if (
        result.returncode != 0
        and not result.timed_out
        and signals.get("runner_pause") is None
    ):
        payload["failure_classification"] = harness_failures.classify_harness_failure(
            result
        ).to_dict()
    degradation = harness_failures.classify_harness_degradation(result)
    if degradation is not None:
        payload["harness_degradation"] = degradation.to_dict()
    if live_turn.get("action_required") is not None:
        payload["action_required"] = live_turn["action_required"]
    if plan_error is not None:
        payload["plan_error"] = plan_error
    prompt_kind = live_turn.get("prompt_kind")
    if prompt_kind is not None:
        payload["prompt_kind"] = prompt_kind
    reply_source = live_turn.get("reply_source")
    if reply_source is not None:
        payload["reply_source"] = reply_source
    resolution_source = live_turn.get("resolution_source")
    if resolution_source is not None:
        payload["resolution_source"] = resolution_source
    payload.update(signals)
    # Events are always fullsynced, so a non-durable result would let the
    # event stating this turn ended outlive the artifact it points at.
    durability.write_json_file(paths["result"], payload, durable=True)
    return payload


@dataclass(frozen=True)
class TurnFinalization:
    """What the runner knows once the harness process for a turn has exited.

    This owns the execution half of a turn's ending: the raw harness result,
    the plan re-read that follows it, and the artifacts written from both. It
    exposes no way to announce accepted completion, because at this point
    nothing has been accepted — every guard that could still reject the turn
    runs after the object is built.
    """

    result: harnesses.HarnessResult
    after: plans.PlanSnapshot | None
    plan_error: str | None
    turn_payload: dict[str, Any]


def finalize_turn_execution(
    *,
    event_path: Path,
    persistence: TurnPersistence,
    plan_cache: plans.PlanSnapshotCache,
    result: harnesses.HarnessResult,
    before: plans.PlanSnapshot,
    paths: dict[str, Path],
    run_id: str,
    turn_index: int,
    session_id: str,
    live_turn: dict[str, Any],
    unredacted_prompt: str | None = None,
    pending_exception_guard: MutableMapping[str, Any] | None = None,
) -> TurnFinalization:
    """Collect a finished harness process into a :class:`TurnFinalization`.

    The plan is re-read here because the harness edits it in place, and a
    handoff that no longer parses is carried as ``plan_error`` rather than
    raised: the run loop decides what an unreadable plan means. The
    ``turn_execution_finished`` event is appended last so it never names
    artifacts that are not on disk yet.
    """
    plan_error: str | None = None
    after: plans.PlanSnapshot | None
    try:
        after = plan_cache.get_fresh(before.plan_dir)
    except plans.PlanError as error:
        after = None
        plan_error = str(error)
    if pending_exception_guard is not None:
        pending_exception_guard["after"] = after
    turn_payload = write_turn_artifacts(
        paths=paths,
        result=result,
        before=before,
        after=after,
        run_id=run_id,
        turn_index=turn_index,
        session_id=session_id,
        live_turn=live_turn,
        plan_error=plan_error,
        unredacted_prompt=unredacted_prompt,
    )
    if pending_exception_guard is not None:
        pending_exception_guard["turn_payload"] = turn_payload
    execution_payload: dict[str, Any] = {
        "run_id": run_id,
        "turn_index": turn_index,
        **plans.session_identity_payload(session_id),
        "harness": result.name,
        "returncode": result.returncode,
        "timed_out": result.timed_out,
        "interrupted": result.interrupted,
        "aborted": result.aborted,
        "started_at": live_turn["started_at"],
        "finished_at": live_turn["finished_at"],
        "duration_seconds": live_turn["duration_seconds"],
        "artifact_dir": str(paths["artifact_dir"]),
        "stdout_path": str(paths["stdout"]),
        "stderr_path": str(paths["stderr"]),
        "last_message_path": str(paths["last_message"]),
        "result_path": str(paths["result"]),
        "plan_readable": after is not None,
    }
    if result.stream_io is not None:
        execution_payload["stream_io"] = dict(result.stream_io)
    if plan_error is not None:
        execution_payload["plan_error"] = plan_error
    persistence.append_event(
        event_path,
        events.TURN_EXECUTION_FINISHED_EVENT,
        execution_payload,
    )
    return TurnFinalization(
        result=result,
        after=after,
        plan_error=plan_error,
        turn_payload=turn_payload,
    )


def replace_turn_record(payload: dict[str, Any], committed: dict[str, Any]) -> None:
    """Swap a turn's in-flight state record for its committed form."""
    turns = payload.setdefault("turns", [])
    turn_index = committed.get("turn_index")
    for position, record in enumerate(turns):
        if isinstance(record, Mapping) and record.get("turn_index") == turn_index:
            turns[position] = committed
            return
    turns.append(committed)


class TurnCommitRejected(Exception):
    """The plan stopped agreeing with the accepted snapshot before publication."""

    def __init__(self, *, reason: str, message: str, detail: dict[str, Any]) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.detail = detail


def publication_snapshot(
    plan_cache: plans.PlanSnapshotCache,
    accepted: plans.PlanSnapshot,
) -> plans.PlanSnapshot:
    """Re-read the plan so the published snapshot describes it as it now is.

    Runner bookkeeping between the last guard and the commit point — the
    readiness refresh above all — rewrites plan files, so ``accepted`` was
    hashed before the plan reached the state the turn is about to be published
    against. The statuses must not have moved: the guards passed on
    ``accepted``, so a different status mapping is drift rather than
    bookkeeping, and the turn is refused instead of silently republished.
    """
    try:
        recomputed = plan_cache.get_fresh(accepted.plan_dir)
    except plans.PlanError as error:
        raise TurnCommitRejected(
            reason="malformed-plan",
            message="the plan could not be re-read before publishing the turn",
            detail={"error": str(error)},
        ) from error
    if plans.status_map(recomputed) != plans.status_map(accepted):
        raise TurnCommitRejected(
            reason="unexpected-status-change",
            message=(
                "session statuses moved between acceptance and publication of the turn"
            ),
            detail={
                "accepted_content_sha256": accepted.content_sha256,
                "recomputed_content_sha256": recomputed.content_sha256,
                "status_changes": list(plans.status_changes(accepted, recomputed)),
            },
        )
    return recomputed


@dataclass(frozen=True)
class TurnCommit:
    """The published statement that a turn was accepted, and what it published.

    ``record`` is the turn record as operators and audits will read it; it is
    read-only because amending a published turn would rewrite a statement
    already consumed. ``published`` is the plan snapshot that record describes,
    carried beside it so a caller updating the run's final snapshot uses the
    same read the publication was built from rather than a later one.
    """

    published: plans.PlanSnapshot
    record: Mapping[str, Any]


def commit_turn(
    *,
    event_path: Path,
    payload: dict[str, Any],
    persistence: TurnPersistence,
    progress: TurnProgress,
    turn_payload: dict[str, Any],
    live_turn: dict[str, Any],
    accepted: plans.PlanSnapshot,
    plan_cache: plans.PlanSnapshotCache,
    paths: dict[str, Path],
) -> TurnCommit:
    """Publish the single immutable statement that a turn was accepted.

    This is the only place ``turn_finished`` is emitted. Every guard able to
    reject the turn, and every runner-owned plan mutation the turn's result
    describes, has already run, so the published snapshot is recomputed here,
    once, from a fresh read — the guards passed on ``accepted``, but the plan
    on disk has moved on since it was hashed. ``result.json`` and the run state
    are written first so the event can never name a result that disagrees with
    them, and downstream consumers hear about the turn only once that event is
    durable.

    The re-read happens before the persistence lock is entered, and this
    function never takes ``LiveTurn._lock``, so lock ordering is unchanged.

    The returned record is read-only: a later phase that tried to amend a
    published turn would be rewriting a statement operators and audits have
    already read.
    """
    published = publication_snapshot(plan_cache, accepted)
    committed = dict(turn_payload)
    committed["after"] = plans.snapshot_payload(published)
    committed["committed"] = True
    with persistence.lock:
        durability.write_json_file(paths["result"], committed, durable=True)
        replace_turn_record(payload, committed)
        persistence.write_state(payload)
        persistence.append_event(event_path, events.TURN_FINISHED_EVENT, committed)
    progress.turn_finished(committed)
    notify_turn_finished(payload, live_turn, persistence)
    return TurnCommit(published=published, record=MappingProxyType(committed))


def reopened_snapshot_identity(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """The projection a status reversion left behind.

    A reversion that names only the transition leaves a replaying consumer
    holding a state it cannot check; the digest and counts of the plan as
    reinspected after the rewrite are what make the replay land on the run's
    own authoritative projection.
    """

    return {
        "content_sha256": snapshot.get("content_sha256"),
        "counts": snapshot.get("counts"),
    }


def record_turn_rejected(
    *,
    event_path: Path,
    payload: dict[str, Any],
    persistence: TurnPersistence,
    turn_payload: Mapping[str, Any],
    session_id: str,
    guard: str,
    reason: str,
    reopened: bool = False,
    detail: Mapping[str, Any] | None = None,
) -> None:
    """Record that a post-execution guard refused an already-executed turn.

    Without this the only trace of a refusal is a pause reason plus a plan that
    silently disagrees with the last turn record. ``session_reopened`` is
    appended beside it when the guard also returned the session to pending, so
    the reverse transition the plan made is replayable from the event stream.

    That event carries the plan digest and status counts the revert produced, so
    a consumer replaying the stream lands on the same projection the run
    reports rather than on a state it has to trust the plan file to confirm.
    Every reopening guard refreshes ``final_snapshot`` from the reinspected plan
    before recording the rejection, so it is the resulting snapshot by
    construction.
    """
    rejection: dict[str, Any] = {
        "run_id": payload.get("run_id"),
        "turn_index": turn_payload.get("turn_index"),
        **plans.session_identity_payload(session_id),
        "guard": guard,
        "reason": reason,
        "reopened": reopened,
    }
    if detail:
        rejection["detail"] = dict(detail)
    persistence.append_event(event_path, events.TURN_REJECTED_EVENT, rejection)
    if not reopened:
        return
    final_snapshot = payload.get("final_snapshot")
    persistence.append_event(
        event_path,
        events.SESSION_REOPENED_EVENT,
        {
            "run_id": payload.get("run_id"),
            "turn_index": turn_payload.get("turn_index"),
            **plans.session_identity_payload(session_id),
            "from_status": "complete",
            "to_status": "pending",
            "guard": guard,
            "reason": reason,
            **reopened_snapshot_identity(
                final_snapshot if isinstance(final_snapshot, Mapping) else {}
            ),
        },
    )


def _hang_env_seconds(name: str, default: float) -> float:
    """Return a finite positive float override from ``name`` or ``default``.

    Modeled on :func:`agent_mux_delivery.notify_timeout`: anything unparsable,
    non-positive, or non-finite fails safe onto the default so a bad
    override can never disable or destabilize hang detection.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw.strip())
    except ValueError:
        return default
    if value <= 0 or not math.isfinite(value):
        return default
    return value


def _hang_trigger_idle_seconds() -> float:
    return _hang_env_seconds(HANG_TRIGGER_IDLE_ENV_VAR, HANG_TRIGGER_IDLE_SECONDS)


def _hang_result_grace_seconds() -> float:
    return _hang_env_seconds(HANG_RESULT_GRACE_ENV_VAR, HANG_RESULT_GRACE_SECONDS)


def _hang_confirm_samples() -> int:
    raw = os.environ.get(HANG_CONFIRM_SAMPLES_ENV_VAR)
    if raw is None:
        return HANG_CONFIRM_SAMPLES
    try:
        value = int(raw.strip())
    except ValueError:
        return HANG_CONFIRM_SAMPLES
    return value if value >= 1 else HANG_CONFIRM_SAMPLES


@dataclass
class HangAssessment:
    """One heartbeat-tick verdict from :class:`HangSentinel`.

    ``suspected`` is the current (possibly refreshed) suspicion block or
    ``None``; ``transition`` is ``"suspected"``/``"cleared"`` only on the tick
    the state actually flips; ``retracted`` carries the old block on
    ``"cleared"`` so the event and notification can name what was withdrawn.
    ``generation`` is the sentinel activity generation the verdict sampled;
    ``confirm_assessment`` rejects the verdict at fold time when output has
    resumed since, so a stale suspicion can never be persisted after a newer
    clear.

    ``cpu_progress`` is the tick's raw CPU sampling result, published whenever
    a process tree was sampled — including ticks that raise no suspicion at
    all. A supervisor deciding on idle time alone has no ``suspected`` block to
    read, and a silent turn whose tree is burning CPU is working, not hung. Its
    ``cpu_total_seconds`` is accumulated by process identity rather than summed
    over the live tree, so consecutive ticks prove the delta and its
    non-negativity. ``None`` means no fresh sample this tick and must never be
    read as "not progressing".
    """

    suspected: dict[str, Any] | None = None
    transition: str | None = None
    retracted: dict[str, Any] | None = None
    generation: int = 0
    cpu_progress: dict[str, Any] | None = None


class HangSentinel:
    """Classify a silent harness turn as suspected-hung from heartbeat ticks.

    Fed by the harness observation hooks (``note_harness_pid`` /
    ``note_final_result``) and by ``note_activity`` on resumed output;
    ``evaluate`` runs once per heartbeat tick. ``evaluate`` spawns ``ps``
    through ``sample_tree`` and must never be called under
    ``LiveTurn._lock``.
    """

    def __init__(
        self,
        *,
        sample_tree: Callable[
            [int], list[dict[str, Any]] | None
        ] = process_tree.sample_process_tree,
        monotonic: Callable[[], float] = time.monotonic,
        trigger_idle_seconds: float | None = None,
        confirm_samples: int | None = None,
        cpu_epsilon_seconds: float = HANG_CPU_EPSILON_SECONDS,
        result_grace_seconds: float | None = None,
    ) -> None:
        self._sample_tree = sample_tree
        self._monotonic = monotonic
        self._trigger_idle_seconds = (
            _hang_trigger_idle_seconds()
            if trigger_idle_seconds is None
            else trigger_idle_seconds
        )
        self._confirm_samples = (
            _hang_confirm_samples() if confirm_samples is None else confirm_samples
        )
        self._cpu_epsilon_seconds = cpu_epsilon_seconds
        self._result_grace_seconds = (
            _hang_result_grace_seconds()
            if result_grace_seconds is None
            else result_grace_seconds
        )
        self._lock = threading.Lock()
        self._harness_pid: int | None = None
        self._result_seen_at: datetime | None = None
        self._result_seen_monotonic: float | None = None
        self._last_cpu_seconds: float | None = None
        self._cpu_high_water: dict[process_tree.CpuIdentity, float] = {}
        self._zero_cpu_streak = 0
        self._suspected: dict[str, Any] | None = None
        self._recovery_attempted = False
        # Bumped on every note_activity; assessments carry the generation
        # they sampled so any verdict outlived by resumed output is discarded
        # (mid-sample by evaluate, post-return by confirm_assessment).
        self._activity_generation = 0
        # True once the current episode's suspected event was confirmed for
        # persistence; only announced episodes yield a retracted block (and
        # thus a cleared event), keeping suspected/cleared pairs matched.
        self._announced = False

    def note_harness_pid(self, pid: int) -> None:
        with self._lock:
            if pid == self._harness_pid:
                return
            # A different root makes the accumulated identities another tree's:
            # the dead root's frozen CPU would inflate the total forever and
            # mask a real hang. Re-notifying the same pid changes nothing, so a
            # repeat call cannot keep resetting the streak.
            self._harness_pid = pid
            self._cpu_high_water = {}
            self._last_cpu_seconds = None
            self._zero_cpu_streak = 0
            self._recovery_attempted = False

    def note_final_result(self) -> None:
        with self._lock:
            if self._result_seen_monotonic is None:
                self._result_seen_at = clock.utc_now()
                self._result_seen_monotonic = self._monotonic()

    def note_activity(self) -> dict[str, Any] | None:
        """Retract suspicion on resumed output; return the retracted block.

        Returns ``None`` for an unannounced episode (its suspected event was
        never confirmed for persistence) so no orphan cleared event is
        emitted for a suspicion nobody was ever told about.
        """
        with self._lock:
            self._activity_generation += 1
            retracted = self._suspected if self._announced else None
            self._suspected = None
            self._announced = False
            self._zero_cpu_streak = 0
            self._last_cpu_seconds = None
            self._cpu_high_water = {}
            self._recovery_attempted = False
            return retracted

    def note_turn_exit(self) -> dict[str, Any] | None:
        """Retract an announced suspicion when the harness process exits.

        A terminal process exit is conclusive recovery evidence even when the
        SIGTERM-unblocked harness emits no additional output chunk. A turn that
        stays alive never reaches this hook and retains its unresolved block.
        """
        with self._lock:
            retracted = self._suspected if self._announced else None
            self._suspected = None
            self._announced = False
            return dict(retracted) if retracted is not None else None

    def recover_post_result(self, assessment: HangAssessment) -> HangAssessment:
        """Attempt one verified-leaf SIGTERM for a terminal-result suspicion.

        Revalidation samples outside the sentinel lock. The generation is then
        checked again and signals are delivered while holding the lock, so
        output that resumed during the sample invalidates the assessment before
        any process can be touched. Generic idle suspicions never enter here.
        """
        block = assessment.suspected
        if block is None or block.get("reason") != HANG_REASON_RESULT_RECEIVED:
            return assessment
        with self._lock:
            if (
                assessment.generation != self._activity_generation
                or self._suspected is None
                or self._recovery_attempted
            ):
                return assessment
            self._recovery_attempted = True
            pid = self._harness_pid
            prior_tree = tuple(block.get("process_tree") or ())
            attempted_at = str(
                block.get("last_checked_at") or clock.isoformat(clock.utc_now())
            )

        if pid is None:
            return assessment
        recovery, candidates = process_tree.prepare_verified_leaf_recovery(
            root_pid=pid,
            prior_tree=prior_tree,
            attempted_at=attempted_at,
        )

        with self._lock:
            if (
                assessment.generation != self._activity_generation
                or self._suspected is None
            ):
                # Output resumed while the process table was sampled. The
                # activity path already cleared the episode and re-armed a
                # future one, so this stale assessment publishes nothing.
                return HangAssessment(generation=assessment.generation)
            recovery = process_tree.signal_verified_leaf_recovery(recovery, candidates)
            recovered_block = dict(self._suspected)
            recovered_block["recovery"] = recovery
            self._suspected = recovered_block
            return HangAssessment(
                suspected=dict(recovered_block),
                transition=assessment.transition,
                retracted=assessment.retracted,
                generation=assessment.generation,
                cpu_progress=assessment.cpu_progress,
            )

    def confirm_assessment(self, assessment: HangAssessment) -> bool:
        """Atomically re-validate an assessment immediately before folding.

        Returns ``False`` when output resumed after the assessment sampled
        its activity generation — the verdict is stale and must be discarded
        wholesale. On success marks a live suspicion announced, so a
        subsequent ``note_activity`` retraction emits its paired cleared
        event strictly after the suspected event the caller is persisting
        (the caller holds the fold lock across confirm and persist).
        """
        with self._lock:
            if assessment.generation != self._activity_generation:
                return False
            if self._suspected is not None:
                self._announced = True
            return True

    def evaluate(self, *, idle_seconds: float, now: datetime) -> HangAssessment:
        with self._lock:
            pid = self._harness_pid
            result_seen_at = self._result_seen_at
            result_seen_monotonic = self._result_seen_monotonic
            previous = self._suspected
            generation = self._activity_generation

        result_grace_hit = (
            result_seen_monotonic is not None
            and self._monotonic() - result_seen_monotonic >= self._result_grace_seconds
        )
        should_sample = pid is not None and (
            result_grace_hit
            or idle_seconds >= self._trigger_idle_seconds
            or previous is not None
        )
        tree: list[dict[str, Any]] | None = None
        tree_error: str | None = None
        if should_sample:
            assert pid is not None
            # The ps spawn happens here, outside every lock.
            tree = self._sample_tree(pid)
            if tree is None:
                tree_error = "process tree sample failed"
            else:
                # Sampling only happens while the turn is silent or suspected
                # hung, which is exactly when the post-turn sweep needs a view
                # of descendants that may since have left the process group.
                process_tree.record_process_tree(pid, tree)

        with self._lock:
            if generation != self._activity_generation:
                # note_activity ran while ps sampled; the idle reading and
                # any verdict built from it are stale — discard this tick.
                return HangAssessment(generation=generation)

            cpu_delta: float | None = None
            cpu_positive = False
            cpu_total: float | None = None
            if tree is not None:
                # Accumulated per process identity, never summed over the live
                # tree: a descendant that exits between two samples must not
                # subtract the CPU it burned, which would read as stillness.
                cpu_total, self._cpu_high_water = process_tree.accumulate_cpu_seconds(
                    tree, self._cpu_high_water
                )
                if self._last_cpu_seconds is not None:
                    cpu_delta = cpu_total - self._last_cpu_seconds
                    if cpu_delta <= self._cpu_epsilon_seconds:
                        self._zero_cpu_streak += 1
                    else:
                        self._zero_cpu_streak = 0
                        cpu_positive = True
                self._last_cpu_seconds = cpu_total
            elif should_sample:
                # A failed sample never confirms: demand a fresh consecutive
                # run of successful zero-delta samples.
                self._zero_cpu_streak = 0
                self._last_cpu_seconds = None
                self._cpu_high_water = {}

            checked_at = clock.isoformat(now)
            cpu_progress: dict[str, Any] | None = None
            if tree is not None:
                cpu_progress = {
                    "harness_pid": pid,
                    # The first successful sample has no predecessor to compare
                    # against, so progress is unknown rather than absent.
                    "progressing": cpu_positive if cpu_delta is not None else None,
                    "cpu_delta_seconds": cpu_delta,
                    "cpu_total_seconds": cpu_total,
                    "tracked_processes": len(self._cpu_high_water),
                    "sampled_processes": len(tree),
                    "confirm_samples": self._zero_cpu_streak,
                    "last_checked_at": checked_at,
                }

            # Both classes demand the same proof: a confirmed run of zero-CPU
            # samples. A received harness result is sticky, so without this the
            # suspicion re-raises on every later tick and the CPU retraction
            # below is unreachable — a worker still burning CPU behind a
            # buffering pipe reads as hung.
            confirmed = self._zero_cpu_streak >= self._confirm_samples
            reason: str | None = None
            if result_grace_hit and confirmed:
                reason = HANG_REASON_RESULT_RECEIVED
            elif (
                pid is not None
                and idle_seconds >= self._trigger_idle_seconds
                and confirmed
            ):
                reason = HANG_REASON_ZERO_CPU

            if reason is not None:
                block: dict[str, Any] = {
                    "reason": reason,
                    "detected_at": (
                        previous["detected_at"] if previous is not None else checked_at
                    ),
                    "idle_seconds": idle_seconds,
                    "harness_pid": pid,
                    "result_seen_at": (
                        clock.isoformat(result_seen_at)
                        if result_seen_at is not None
                        else None
                    ),
                    "confirm_samples": self._zero_cpu_streak,
                    "cpu_delta_seconds": cpu_delta,
                    "process_tree": tree,
                    "last_checked_at": checked_at,
                }
                if tree_error is not None:
                    block["process_tree_error"] = tree_error
                self._suspected = block
                return HangAssessment(
                    suspected=dict(block),
                    transition="suspected" if previous is None else None,
                    generation=generation,
                    cpu_progress=cpu_progress,
                )
            if previous is not None and cpu_positive:
                self._suspected = None
                self._announced = False
                retracted = dict(previous)
                retracted["cpu_delta_seconds"] = cpu_delta
                retracted["last_checked_at"] = checked_at
                return HangAssessment(
                    transition="cleared",
                    retracted=retracted,
                    generation=generation,
                    cpu_progress=cpu_progress,
                )
            if previous is not None:
                # A ps failure (or a streak broken by one) refreshes rather
                # than retracts: absence of evidence is not activity.
                refreshed = dict(previous)
                refreshed["idle_seconds"] = idle_seconds
                refreshed["confirm_samples"] = self._zero_cpu_streak
                refreshed["cpu_delta_seconds"] = cpu_delta
                refreshed["process_tree"] = tree
                refreshed["last_checked_at"] = checked_at
                refreshed.pop("process_tree_error", None)
                if tree_error is not None:
                    refreshed["process_tree_error"] = tree_error
                self._suspected = refreshed
                return HangAssessment(
                    suspected=dict(refreshed),
                    generation=generation,
                    cpu_progress=cpu_progress,
                )
            return HangAssessment(generation=generation, cpu_progress=cpu_progress)


class LiveTurn:
    """Thread-safe live state and periodic-work manager for one harness turn."""

    def __init__(
        self,
        *,
        payload: dict[str, Any],
        event_path: Path,
        progress: TurnProgress,
        active_turn: dict[str, Any],
        persistence: TurnPersistence,
        heartbeat_interval: float = HEARTBEAT_INTERVAL_SECONDS,
        poll_callback: Callable[[], None] | None = None,
        poll_interval: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        wait: Callable[[float], bool] | None = None,
        hang_sentinel: HangSentinel | None = None,
        wakeup: threading.Event | None = None,
    ) -> None:
        self._payload = payload
        self._event_path = event_path
        self._progress = progress
        self._active_turn = dict(active_turn)
        self._persistence = persistence
        self._heartbeat_interval = heartbeat_interval
        self._poll_callback = poll_callback
        self._poll_interval = poll_interval
        self._monotonic = monotonic
        self._hang_sentinel = (
            HangSentinel(monotonic=monotonic)
            if hang_sentinel is None
            else hang_sentinel
        )
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wakeup = wakeup
        # Two Events cannot be waited on together, so the wakeup is the one
        # this loop blocks on and ``stop()`` sets it as well. Without a wakeup
        # the seam keeps its original ``_stop.wait`` behavior.
        default_wait = self._stop.wait if wakeup is None else wakeup.wait
        self._wait = default_wait if wait is None else wait
        self._thread: threading.Thread | None = None
        self._started_at = (
            clock.parse_datetime(active_turn["started_at"]) or clock.utc_now()
        )
        self._last_activity_at = self._started_at

    def start(self) -> None:
        with self._lock, self._persistence.lock:
            # This turn is the resolution of whatever the last one asked for:
            # the run would not have reached another turn otherwise.
            clear_live_action_required(self._payload)
            self._payload["active_turn"] = dict(self._active_turn)
            self._persistence.write_state(self._payload)
        self._progress.turn_started(self._active_turn)
        self._thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._thread.start()

    def note_harness_pid(self, pid: int) -> None:
        self._hang_sentinel.note_harness_pid(pid)

    def note_final_result(self) -> None:
        self._hang_sentinel.note_final_result()

    def note_observed_model(self, model: str) -> None:
        with self._lock:
            if self._active_turn.get("observed_model") is not None:
                return
            self._active_turn["observed_model"] = model
            self._active_turn["observed_model_source"] = (
                config.MODEL_SOURCE_HARNESS_REPORTED
            )
            snapshot = dict(self._active_turn)
            with self._persistence.lock:
                self._payload["active_turn"] = snapshot
                self._persistence.write_state(self._payload)

    def record_activity(self, stream_name: harnesses.StreamName, text: str) -> None:
        signal_to_report: dict[str, Any] | None = None
        now = clock.utc_now()
        hang_retracted = self._hang_sentinel.note_activity()
        hang_progress_cleared = False
        hang_cleared_payload: dict[str, Any] | None = None
        active_turn_snapshot: dict[str, Any] | None = None
        with self._lock, self._persistence.lock:
            self._last_activity_at = now
            self._active_turn["last_activity_at"] = clock.isoformat(now)
            self._active_turn["last_activity_stream"] = stream_name
            self._active_turn["last_activity_preview"] = preview_text(text)
            self._active_turn["elapsed_seconds"] = clock.duration_seconds(
                self._started_at,
                now,
            )
            self._active_turn["idle_seconds"] = 0.0
            char_key = f"{stream_name}_chars"
            self._active_turn[char_key] = int(self._active_turn.get(char_key, 0)) + len(
                text
            )
            # Resumed output resets the sentinel's CPU baseline, so whatever
            # was published is no longer a fresh sample.
            hang_progress_cleared = "hang_cpu_progress" in self._active_turn
            self._active_turn.pop("hang_cpu_progress", None)
            if hang_retracted is not None:
                self._active_turn.pop("hang_suspected", None)
                hang_cleared_payload = self._hang_event_payload(hang_retracted, now)

            signal = live_action_signal(stream_name, text)
            if signal is not None and self._active_turn.get("action_required") is None:
                signal_to_report = self._action_required_payload(signal, now)
                self._active_turn["action_required"] = signal_to_report
            active_turn_snapshot = dict(self._active_turn)
            # Ordinary output stays live in the shared payload and is durably
            # coalesced by the next heartbeat. The whole callback mutation is
            # under the persistence lock so a concurrent control write cannot
            # serialize older counters or previews after this callback.
            self._payload["active_turn"] = active_turn_snapshot
            if signal_to_report is not None:
                self._payload["action_required"] = signal_to_report
                self._persistence.append_event(
                    self._event_path,
                    events.ACTION_REQUIRED_EVENT,
                    signal_to_report,
                )
            if hang_cleared_payload is not None:
                self._persistence.append_event(
                    self._event_path,
                    events.TURN_HANG_CLEARED_EVENT,
                    hang_cleared_payload,
                )
            if (
                signal_to_report is not None
                or hang_cleared_payload is not None
                or hang_progress_cleared
            ):
                self._persistence.write_state(self._payload)

        if signal_to_report is not None:
            self._progress.action_required(signal_to_report)
        if hang_retracted is not None and not self._stop.is_set():
            notify_turn_hang_cleared(
                self._payload,
                active_turn_snapshot,
                hang_reason=hang_retracted["reason"],
                hang_idle_seconds=hang_retracted.get("idle_seconds"),
                harness_pid=hang_retracted.get("harness_pid"),
                persistence=self._persistence,
            )

    def stop(self) -> None:
        self._stop.set()
        if self._wakeup is not None:
            # The poll thread blocks on the wakeup, so shutdown has to wake it
            # for the join below to return promptly.
            self._wakeup.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            # A poll callback deliberately runs without ``self._lock``. Wait
            # for it to finish before the caller clears ``active_turn`` so it
            # cannot persist control state after the turn has finished.
            self._thread.join()

    def finish(self, finished_at: datetime) -> dict[str, Any]:
        self.stop()
        hang_retracted = self._hang_sentinel.note_turn_exit()
        with self._lock:
            active_turn = dict(self._active_turn)
            active_turn["finished_at"] = clock.isoformat(finished_at)
            active_turn["duration_seconds"] = clock.duration_seconds(
                self._started_at,
                finished_at,
            )
            active_turn["idle_seconds"] = clock.duration_seconds(
                self._last_activity_at,
                finished_at,
            )
            active_turn.pop("hang_suspected", None)
            self._active_turn = active_turn
            with self._persistence.lock:
                if hang_retracted is not None:
                    self._persistence.append_event(
                        self._event_path,
                        events.TURN_HANG_CLEARED_EVENT,
                        self._hang_event_payload(hang_retracted, finished_at),
                    )
                self._payload["active_turn"] = None
                self._persistence.write_state(self._payload)
            return active_turn

    def _heartbeat_loop(self) -> None:
        started = self._monotonic()
        next_heartbeat = started + self._heartbeat_interval
        polling_enabled = (
            self._poll_callback is not None
            and self._poll_interval is not None
            and self._poll_interval > 0
        )
        next_poll = (
            started + self._poll_interval
            if polling_enabled and self._poll_interval is not None
            else None
        )

        while not self._stop.is_set():
            deadline = (
                next_heartbeat if next_poll is None else min(next_heartbeat, next_poll)
            )
            delay = max(0.0, deadline - self._monotonic())
            woken = self._wait(delay)
            if self._stop.is_set() or (woken and self._wakeup is None):
                return
            if woken:
                # A wakeup landed: a producer just queued a control command, so
                # poll now instead of waiting out the backstop interval. The
                # clear happens before the poll so a wakeup racing it is not
                # swallowed, and the interval reschedules from this wake.
                assert self._wakeup is not None
                self._wakeup.clear()
                if next_poll is not None:
                    next_poll = self._monotonic()

            now = self._monotonic()
            if now >= next_heartbeat:
                next_heartbeat = self._advance_deadline(
                    next_heartbeat,
                    self._heartbeat_interval,
                    now,
                )
                heartbeat = self._record_heartbeat()
                self._progress.heartbeat(heartbeat)

            if self._stop.is_set() or next_poll is None:
                continue
            now = self._monotonic()
            if now >= next_poll:
                assert self._poll_interval is not None
                next_poll = self._advance_deadline(
                    next_poll,
                    self._poll_interval,
                    now,
                )
                # Never hold ``self._lock`` while polling. The callback owns
                # any serialization its mutation domain requires.
                assert self._poll_callback is not None
                self._poll_callback()

    @staticmethod
    def _advance_deadline(
        deadline: float,
        interval: float,
        now: float,
    ) -> float:
        """Advance an expired deadline to the first interval after ``now``."""
        elapsed_intervals = int((now - deadline) // interval) + 1
        return deadline + (elapsed_intervals * interval)

    def _record_heartbeat(self) -> dict[str, Any]:
        now = clock.utc_now()
        # Hang-sentinel sampling spawns ps, so it stays outside ``self._lock``
        # (same discipline as the amx notify below) and is skipped once
        # ``_stop`` is set so ``stop()``/``finish()`` never wait behind ps.
        assessment: HangAssessment | None = None
        if not self._stop.is_set():
            with self._lock:
                idle_seconds = clock.duration_seconds(self._last_activity_at, now)
            assessment = self._hang_sentinel.evaluate(
                idle_seconds=idle_seconds,
                now=now,
            )
            assessment = self._hang_sentinel.recover_post_result(assessment)
        hang_event: tuple[str, dict[str, Any]] | None = None
        with self._lock:
            if (
                assessment is not None
                and assessment.suspected is not None
                and not self._hang_sentinel.confirm_assessment(assessment)
            ):
                # Output resumed between evaluate() and this fold: the
                # verdict is stale. Folding it would re-add a retracted
                # block or persist suspected after a newer clear. Cleared
                # verdicts are exempt — they are the sole carrier of their
                # retraction and can never invert the event order.
                assessment = None
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
                "created_at": clock.isoformat(now),
                "elapsed_seconds": clock.duration_seconds(self._started_at, now),
                "last_activity_at": self._active_turn.get("last_activity_at"),
                "last_activity_stream": self._active_turn.get("last_activity_stream"),
                "idle_seconds": clock.duration_seconds(self._last_activity_at, now),
                "heartbeat_count": heartbeat_count,
            }
            if self._active_turn.get("session_number") is not None:
                heartbeat["session_number"] = self._active_turn["session_number"]
            self._active_turn["last_heartbeat_at"] = heartbeat["created_at"]
            self._active_turn["heartbeat_count"] = heartbeat_count
            self._active_turn["elapsed_seconds"] = heartbeat["elapsed_seconds"]
            self._active_turn["idle_seconds"] = heartbeat["idle_seconds"]
            if assessment is not None:
                if assessment.suspected is not None:
                    self._active_turn["hang_suspected"] = assessment.suspected
                    heartbeat["hang_suspected"] = assessment.suspected
                else:
                    self._active_turn.pop("hang_suspected", None)
                if assessment.cpu_progress is not None:
                    self._active_turn["hang_cpu_progress"] = assessment.cpu_progress
                    heartbeat["hang_cpu_progress"] = assessment.cpu_progress
                else:
                    self._active_turn.pop("hang_cpu_progress", None)
                if assessment.transition == "suspected":
                    assert assessment.suspected is not None
                    hang_event = (
                        events.TURN_HANG_SUSPECTED_EVENT,
                        self._hang_event_payload(assessment.suspected, now),
                    )
                elif assessment.transition == "cleared":
                    assert assessment.retracted is not None
                    hang_event = (
                        events.TURN_HANG_CLEARED_EVENT,
                        self._hang_event_payload(assessment.retracted, now),
                    )
            active_turn_snapshot = dict(self._active_turn)
            with self._persistence.lock:
                self._payload["active_turn"] = active_turn_snapshot
                if hang_event is not None:
                    self._persistence.append_event(
                        self._event_path,
                        hang_event[0],
                        hang_event[1],
                    )
                self._persistence.append_event(
                    self._event_path,
                    events.TURN_HEARTBEAT_EVENT,
                    heartbeat,
                )
                self._persistence.write_state(self._payload)
        # Notify outside both locks so a slow amx subprocess can never stall
        # activity recording or state writes; skip once the turn is stopping
        # so a late heartbeat cannot outrace the turn-finished notification.
        if not self._stop.is_set():
            notify_turn_heartbeat(
                self._payload,
                active_turn_snapshot,
                self._persistence,
                cancelled=self._stop.is_set,
            )
            if assessment is not None and assessment.transition == "suspected":
                block = assessment.suspected
                assert block is not None
                notify_turn_hang_suspected(
                    self._payload,
                    active_turn_snapshot,
                    hang_reason=block["reason"],
                    hang_idle_seconds=block.get("idle_seconds"),
                    harness_pid=block.get("harness_pid"),
                    persistence=self._persistence,
                )
            elif assessment is not None and assessment.transition == "cleared":
                retracted = assessment.retracted
                assert retracted is not None
                notify_turn_hang_cleared(
                    self._payload,
                    active_turn_snapshot,
                    hang_reason=retracted["reason"],
                    hang_idle_seconds=retracted.get("idle_seconds"),
                    harness_pid=retracted.get("harness_pid"),
                    persistence=self._persistence,
                )
        return heartbeat

    def _hang_event_payload(
        self,
        block: dict[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        """Build a hang lifecycle event payload; call under ``self._lock``.

        ``idle_seconds`` is the turn's current idle reading (0.0 on the
        activity-retraction path), distinct from the possibly-stale value
        inside ``block``, so cleared-event consumers can replace a pre-clear
        idle sample instead of acting on it.
        """
        payload = {
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
            "created_at": clock.isoformat(now),
            "idle_seconds": self._active_turn.get("idle_seconds"),
            "hang_suspected": dict(block),
        }
        cpu_progress = self._active_turn.get("hang_cpu_progress")
        if cpu_progress is not None:
            payload["hang_cpu_progress"] = dict(cpu_progress)
        if self._active_turn.get("session_number") is not None:
            payload["session_number"] = self._active_turn["session_number"]
        return payload

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
            "created_at": clock.isoformat(now),
            "reason": signal["reason"],
            "message": signal["message"],
            "source": signal["source"],
            "matched": signal["matched"],
        } | (
            {"session_number": self._active_turn["session_number"]}
            if self._active_turn.get("session_number") is not None
            else {}
        )


@dataclass(frozen=True)
class TurnIdentity:
    """Which turn of which run against which session this is."""

    run_id: str
    turn_index: int
    session_id: str


@dataclass(frozen=True)
class TurnLaunch:
    """The harness invocation one turn is, decided before anything starts.

    ``build_invocation`` is deferred rather than an ``argv`` field because it
    is only needed on the interrupt path, where no process ever reported one;
    building it eagerly would render a command line for every turn to describe
    the few that are interrupted.

    What it renders there is a description of the turn rather than the command
    the turn ran: the real invocation is built inside ``HarnessAdapter.run``,
    which adds what only it knows. For claude that now includes the conversation
    identifier, so a rebuilt argv names a fresh session the interrupted turn
    never used. Read the conversation off ``HarnessResult.conversation_id``,
    never off an argv rendered here.
    """

    harness: harnesses.HarnessName
    cwd: Path
    plan_dir: Path
    prompt: str
    unsafe_bypass: bool
    timeout: int | None
    model: str | None
    effort: str | None
    abort: harnesses.AbortSignal
    #: The turn-scoped channel live guidance is delivered through. Handed over
    #: unopened: the harness opens it around the process it owns and closes it
    #: when that process is gone, so this object never outlives its turn.
    live_delivery: guidance_delivery.LiveDeliveryEndpoint | None
    stream_callback: harnesses.StreamCallback | None
    build_invocation: Callable[[], harnesses.HarnessInvocation]
    #: The complete child environment for this turn, or ``None`` to inherit
    #: this process's. It carries the run's guard-bin ``PATH`` prefix, which is
    #: what makes a command guard reach the harness and its descendants.
    env: Mapping[str, str] | None = None
    #: Called once the harness process exists, which for a harness that takes
    #: its prompt up front is the moment the prompt was accepted. It is the
    #: earliest point at which guidance carried in that prompt can honestly be
    #: called delivered, and the latest at which it is still this turn's news.
    on_prompt_accepted: Callable[[], None] | None = None
    #: Called with the harness process's pid the moment it exists, ahead of
    #: ``on_prompt_accepted``. It is how a turn-scoped controller learns which
    #: process it is permitted to stop, and it has to be stated at spawn rather
    #: than looked up later: a pid discovered at a deadline names whatever holds
    #: that number by then. Failures propagate -- a controller that could not be
    #: bound would leave an accepted correction with nothing to interrupt.
    on_worker: Callable[[int], None] | None = None
    #: The native conversation this turn continues, or ``None`` to let the
    #: harness open one. Exact-or-refusal: an adapter that cannot restore this
    #: identifier fails the turn rather than running a fresh conversation, so a
    #: caller is never told it continued something it did not.
    resume_conversation_id: str | None = None
    #: Called with the identifier the turn actually runs on, on the harness's
    #: own driver thread and before any work is submitted. The durable write
    #: belongs here, ahead of the turn, so a crash mid-turn still leaves the
    #: conversation findable. Never called by an adapter that cannot report one.
    on_conversation: harnesses.ConversationCallback | None = None


@dataclass(frozen=True)
class TurnExecution:
    """What a launched turn left behind once its process was gone.

    ``live_turn`` is the finished live record, not the mutable one the loop
    kept during the turn: the heartbeat thread is joined and ``active_turn``
    is cleared by the time this exists, so nothing can still be writing to it.
    """

    result: harnesses.HarnessResult
    live_turn: dict[str, Any]
    finished_at: datetime


class TurnExecutor:
    """Runs one turn from launch to publication behind one typed boundary.

    Constructed per turn, because ``identity`` and ``paths`` are what make a
    turn that turn. The three methods are the run loop's whole vocabulary for
    executing one: :meth:`launch` spends the turn, :meth:`finalize` collects
    the exited process into artifacts, and :meth:`commit` publishes the
    accepted result. Everything the loop does between them -- the guards --
    is orchestration this class deliberately knows nothing about.
    """

    def __init__(
        self,
        *,
        identity: TurnIdentity,
        paths: dict[str, Path],
        event_path: Path,
        persistence: TurnPersistence,
        progress: TurnProgress,
        plan_cache: plans.PlanSnapshotCache,
    ) -> None:
        self.identity = identity
        self.paths = paths
        self.event_path = event_path
        self.persistence = persistence
        self.progress = progress
        self.plan_cache = plan_cache

    def launch(
        self,
        launch: TurnLaunch,
        *,
        live_turn: LiveTurn,
        active_turn: dict[str, Any],
        payload: dict[str, Any],
        after_stop: Callable[[], None] | None = None,
        on_failure: Callable[[], None] | None = None,
    ) -> TurnExecution:
        """Spend the turn: start the harness, watch it, and collect its exit.

        The logs are materialized, ``turn_started`` appended and the live turn
        started in that order, so a reader that has seen the event finds the
        files the event names. An interrupt is turned into a result rather
        than propagated -- the run has to record what it spent before it stops
        -- while any other exception tears the live turn down through
        ``on_failure`` and re-raises, because the loop cannot describe a turn
        whose failure it does not understand.
        """
        create_turn_logs(self.paths)
        self.persistence.append_event(self.event_path, "turn_started", active_turn)
        live_turn.start()
        notify_turn_started(payload, active_turn, self.persistence)

        def on_spawn(pid: int) -> None:
            live_turn.note_harness_pid(pid)
            if launch.on_worker is not None:
                launch.on_worker(pid)
            if launch.on_prompt_accepted is not None:
                # Suppressed rather than propagated: recording a delivery is
                # bookkeeping about a turn that has already started, and a
                # ledger failure must not become a spawn failure.
                with contextlib.suppress(Exception):
                    launch.on_prompt_accepted()

        try:
            result = harnesses.run_harness(
                launch.harness,
                cwd=launch.cwd,
                plan_dir=launch.plan_dir,
                prompt=launch.prompt,
                unsafe_bypass=launch.unsafe_bypass,
                last_message_path=self.paths["last_message"],
                timeout=launch.timeout,
                stream_callback=launch.stream_callback,
                model=launch.model,
                effort=launch.effort,
                on_spawn=on_spawn,
                on_final_result=live_turn.note_final_result,
                on_observed_model=live_turn.note_observed_model,
                abort=launch.abort,
                live_delivery=launch.live_delivery,
                env=launch.env,
                on_conversation=launch.on_conversation,
                resume_conversation_id=launch.resume_conversation_id,
            )
        except KeyboardInterrupt:
            result = harnesses.HarnessResult(
                name=launch.harness,
                argv=launch.build_invocation().argv,
                returncode=INTERRUPTED_EXIT,
                stdout="",
                stderr="",
                interrupted=True,
            )
        except Exception:
            live_turn.stop()
            live_turn.finish(clock.utc_now())
            if on_failure is not None:
                on_failure()
            raise
        live_turn.stop()
        # The poll thread is joined by now, so no later tick can arm a signal
        # this finished turn would never read. ``after_stop`` is where the
        # caller disarms whatever it handed the turn, and it runs before
        # ``finish`` clears ``active_turn``.
        if after_stop is not None:
            after_stop()
        finished_at = clock.utc_now()
        live_turn_payload = live_turn.finish(finished_at)
        run_started_at = clock.parse_datetime(payload.get("run_started_at"))
        if run_started_at is not None:
            live_turn_payload["total_duration_seconds"] = clock.duration_seconds(
                run_started_at,
                finished_at,
            )
        return TurnExecution(
            result=result,
            live_turn=live_turn_payload,
            finished_at=finished_at,
        )

    def finalize(
        self,
        execution: TurnExecution,
        *,
        before: plans.PlanSnapshot,
        unredacted_prompt: str | None = None,
        pending_exception_guard: MutableMapping[str, Any] | None = None,
    ) -> TurnFinalization:
        """Collect the exited process into artifacts and a re-read plan."""
        return finalize_turn_execution(
            event_path=self.event_path,
            persistence=self.persistence,
            plan_cache=self.plan_cache,
            result=execution.result,
            before=before,
            paths=self.paths,
            run_id=self.identity.run_id,
            turn_index=self.identity.turn_index,
            session_id=self.identity.session_id,
            live_turn=execution.live_turn,
            unredacted_prompt=unredacted_prompt,
            pending_exception_guard=pending_exception_guard,
        )

    def commit(
        self,
        *,
        payload: dict[str, Any],
        turn_payload: dict[str, Any],
        live_turn: dict[str, Any],
        accepted: plans.PlanSnapshot,
    ) -> TurnCommit:
        """Publish the accepted turn, or raise :class:`TurnCommitRejected`."""
        return commit_turn(
            event_path=self.event_path,
            payload=payload,
            persistence=self.persistence,
            progress=self.progress,
            turn_payload=turn_payload,
            live_turn=live_turn,
            accepted=accepted,
            plan_cache=self.plan_cache,
            paths=self.paths,
        )
