"""Reliable, fail-open delivery of ASL lifecycle events to AgentMux.

The runner owns lifecycle truth and source-sequence persistence.  This module
owns the transport mechanics: stable request identities, strict JSON
acknowledgements, bounded retry, legacy-binary fallback, and sanitized ASL-side
outcome records.  Delivery failures are always evidence, never run failures.
"""

from __future__ import annotations

import contextlib
import math
import os
import shutil
import subprocess
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from . import agent_mux_projection, clock, events
from .agent_mux_delivery_transport import EMIT_LOCK as EMIT_LOCK
from .agent_mux_delivery_transport import MAX_ATTEMPTS as MAX_ATTEMPTS
from .agent_mux_delivery_transport import RETRY_BACKOFF_SECONDS as RETRY_BACKOFF_SECONDS
from .agent_mux_delivery_transport import STATUS_ACCEPTED as STATUS_ACCEPTED
from .agent_mux_delivery_transport import STATUS_DUPLICATE as STATUS_DUPLICATE
from .agent_mux_delivery_transport import (
    STATUS_LEGACY_UNACKNOWLEDGED as STATUS_LEGACY_UNACKNOWLEDGED,
)
from .agent_mux_delivery_transport import (
    STATUS_PERMANENT_FAILURE as STATUS_PERMANENT_FAILURE,
)
from .agent_mux_delivery_transport import (
    STATUS_RETRYABLE_EXHAUSTED as STATUS_RETRYABLE_EXHAUSTED,
)
from .agent_mux_delivery_transport import (
    STATUS_RETRYABLE_FAILURE as STATUS_RETRYABLE_FAILURE,
)
from .agent_mux_delivery_transport import STATUS_UNAVAILABLE as STATUS_UNAVAILABLE
from .agent_mux_delivery_transport import AttemptSummary as AttemptSummary
from .agent_mux_delivery_transport import _deliver as _deliver
from .agent_mux_delivery_transport import _outcome_payload as _outcome_payload
from .agent_mux_delivery_transport import reset_legacy_cache as reset_legacy_cache
from .agent_mux_delivery_transport import retry_sleep as retry_sleep
from .agent_mux_delivery_transport import stable_request_id as stable_request_id

START_EVENT = "AgentSessionLoopStarted"
TURN_START_EVENT = "AgentSessionLoopTurnStarted"
TURN_HEARTBEAT_EVENT = "AgentSessionLoopTurnHeartbeat"
TURN_FINISH_EVENT = "AgentSessionLoopTurnFinished"
TURN_HANG_SUSPECTED_EVENT = "AgentSessionLoopTurnHangSuspected"
TURN_HANG_CLEARED_EVENT = "AgentSessionLoopTurnHangCleared"
VERIFICATION_START_EVENT = "AgentSessionLoopVerificationStarted"
VERIFICATION_HEARTBEAT_EVENT = "AgentSessionLoopVerificationHeartbeat"
VERIFICATION_FINISH_EVENT = "AgentSessionLoopVerificationFinished"
WAIT_START_EVENT = "AgentSessionLoopWaitStarted"
WAIT_END_EVENT = "AgentSessionLoopWaitEnded"
MAILBOX_QUESTION_EVENT = "AgentSessionLoopMailboxQuestion"
MAILBOX_REPLY_EVENT = "AgentSessionLoopMailboxReply"
MAILBOX_RESOLUTION_EVENT = "AgentSessionLoopMailboxResolution"
PAUSE_EVENT = "AgentSessionLoopPaused"
FINISH_EVENT = "AgentSessionLoopFinished"
SOURCE = "agent_session_loop"

START_REASON = "run_started"
TURN_START_REASON = "turn_started"
TURN_HEARTBEAT_REASON = "turn_heartbeat"
TURN_FINISH_REASON = "turn_finished"
TURN_HANG_SUSPECTED_REASON = "turn_hang_suspected"
TURN_HANG_CLEARED_REASON = "turn_hang_cleared"
VERIFICATION_START_REASON = "verification_started"
VERIFICATION_HEARTBEAT_REASON = "verification_heartbeat"
VERIFICATION_FINISH_REASON = "verification_finished"
WAIT_START_REASON = "wait_started"
WAIT_END_REASON = "wait_ended"
MAILBOX_QUESTION_REASON = "mailbox_question_surfaced"
MAILBOX_REPLY_REASON = "mailbox_reply_appended"
MAILBOX_RESOLUTION_REASON = "mailbox_resolution_appended"
PAUSE_REASON = "run_paused"
FINISH_REASON = "run_finished"

# Phase vocabulary is owned by ``agent_mux_projection`` so the durable
# projection and this notification stream cannot disagree about what a phase
# name means. The mapping below says which phase each boundary *announces*;
# the projection derives the same vocabulary from persisted run state instead.
PHASE_BY_EVENT: dict[str, str] = {
    START_EVENT: agent_mux_projection.PHASE_BETWEEN,
    TURN_START_EVENT: agent_mux_projection.PHASE_TURN,
    TURN_HEARTBEAT_EVENT: agent_mux_projection.PHASE_TURN,
    TURN_FINISH_EVENT: agent_mux_projection.PHASE_BETWEEN,
    TURN_HANG_SUSPECTED_EVENT: agent_mux_projection.PHASE_TURN,
    TURN_HANG_CLEARED_EVENT: agent_mux_projection.PHASE_TURN,
    VERIFICATION_START_EVENT: agent_mux_projection.PHASE_VERIFICATION,
    VERIFICATION_HEARTBEAT_EVENT: agent_mux_projection.PHASE_VERIFICATION,
    VERIFICATION_FINISH_EVENT: agent_mux_projection.PHASE_BETWEEN,
    WAIT_START_EVENT: agent_mux_projection.PHASE_WAITING,
    WAIT_END_EVENT: agent_mux_projection.PHASE_BETWEEN,
    # The three mailbox boundaries are emitted by the CLI rather than by the
    # loop, and every one of them concerns a run that is blocked on an answer:
    # the question that blocks it, the reply that will clear it, and the
    # resolution that ends the wait. None of them observes the run resuming, so
    # none may announce a phase that claims it has.
    MAILBOX_QUESTION_EVENT: agent_mux_projection.PHASE_WAITING,
    MAILBOX_REPLY_EVENT: agent_mux_projection.PHASE_WAITING,
    MAILBOX_RESOLUTION_EVENT: agent_mux_projection.PHASE_WAITING,
    PAUSE_EVENT: agent_mux_projection.PHASE_PAUSED,
    FINISH_EVENT: agent_mux_projection.PHASE_FINISHED,
}

NOTIFY_TIMEOUT_SECONDS = 1.0
NOTIFY_TIMEOUT_ENV_VAR = "ASL_AGENT_MUX_NOTIFY_TIMEOUT_SECONDS"
NOTIFY_ENV_VAR = "ASL_AGENT_MUX_NOTIFY"
NOTIFY_OFF = "off"


def loop_harness(
    payload: Mapping[str, Any],
    active_turn: Mapping[str, Any] | None = None,
) -> str | None:
    """Return the harness active at this lifecycle boundary."""
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


def build_loop_payload(
    payload: Mapping[str, Any],
    *,
    reason: str,
    active_turn: Mapping[str, Any] | None = None,
    event_name: str | None = None,
    sequence: int | None = None,
    emitted_at: datetime | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the bounded lifecycle payload consumed by ``hook-indicator``.

    ``event_name`` is what makes the result a schema-2 payload: it supplies the
    declared event, the phase that boundary announces, and — with the version
    and contract-set identity beside it — the block that lets a consumer decide
    whether it understands the message before it reads the rest.
    """
    hook_payload: dict[str, Any] = {
        "source": SOURCE,
        "reason": reason,
        "run_id": payload.get("run_id"),
        "plan_dir": payload.get("plan_dir"),
        "cwd": payload.get("cwd"),
        "state_path": payload.get("state_path"),
        "harness": loop_harness(payload, active_turn),
    }
    if event_name is not None:
        hook_payload.update(agent_mux_projection.hook_identity(event_name))
        phase = PHASE_BY_EVENT.get(event_name)
        if phase is not None:
            hook_payload["phase"] = phase
    if sequence is not None:
        hook_payload["source_sequence"] = str(int(sequence))
        hook_payload["source_event_id"] = f"{payload.get('run_id')}:{int(sequence)}"
    if emitted_at is not None:
        hook_payload["emitted_time"] = str(int(emitted_at.timestamp()))
    if payload.get("kitty_window_id") is not None:
        hook_payload["kitty_window_id"] = payload.get("kitty_window_id")
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
            "heartbeat_count",
        ):
            if active_turn.get(key) is not None:
                hook_payload[key] = active_turn.get(key)
        if active_turn.get("last_activity_at") is not None:
            hook_payload["last_output_at"] = active_turn.get("last_activity_at")
    if extra:
        for key, value in extra.items():
            if value is not None:
                hook_payload[key] = value
    return hook_payload


def notify_timeout(environ: Mapping[str, str] | None = None) -> float:
    """Return the finite positive per-attempt timeout, defaulting to one second."""
    source = os.environ if environ is None else environ
    raw = source.get(NOTIFY_TIMEOUT_ENV_VAR)
    if raw is None:
        return NOTIFY_TIMEOUT_SECONDS
    try:
        value = float(raw.strip())
    except ValueError:
        return NOTIFY_TIMEOUT_SECONDS
    if value <= 0 or not math.isfinite(value):
        return NOTIFY_TIMEOUT_SECONDS
    return value


def notify_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Return false only for the explicit, case-insensitive ``off`` setting."""
    source = os.environ if environ is None else environ
    raw = source.get(NOTIFY_ENV_VAR)
    return raw is None or raw.strip().lower() != NOTIFY_OFF


def notify(
    event_name: str,
    payload: Mapping[str, Any],
    *,
    reason: str,
    active_turn: Mapping[str, Any] | None,
    extra: Mapping[str, Any] | None,
    enabled: bool,
    timeout: float,
    allocate_sequence: Callable[[], int],
    record_outcome: Callable[[dict[str, Any]], None],
    resolve_binary: Callable[[str], str | None],
    run_command: Callable[..., subprocess.CompletedProcess[str]],
    sleep: Callable[[float], None] = retry_sleep,
    emitted_at: Callable[[], datetime],
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any] | None:
    """Deliver one lifecycle event and append exactly one ASL outcome record.

    All transport and outcome-persistence failures are contained here.  The
    caller may safely ignore the return value; it exists for focused tests.
    """
    if not enabled:
        return None
    try:
        with EMIT_LOCK:
            if cancelled is not None and cancelled():
                return None
            sequence = allocate_sequence()
            hook_payload = build_loop_payload(
                payload,
                reason=reason,
                active_turn=active_turn,
                event_name=event_name,
                sequence=sequence,
                emitted_at=emitted_at(),
                extra=extra,
            )
            source_event_id = str(hook_payload["source_event_id"])
            request_id = stable_request_id(source_event_id)
            amx_path = resolve_binary("amx")
            if amx_path is None:
                attempts = [
                    AttemptSummary(
                        attempt=1,
                        mode="discovery",
                        status=STATUS_UNAVAILABLE,
                        retryable=False,
                        reason="agent_mux_missing",
                        error_classification="missing_binary",
                    )
                ]
            else:
                attempts = _deliver(
                    amx_path,
                    event_name,
                    hook_payload,
                    request_id,
                    timeout=timeout,
                    run_command=run_command,
                    sleep=sleep,
                )
            outcome = _outcome_payload(event_name, hook_payload, request_id, attempts)
            # Observability stays fail-open.
            with contextlib.suppress(Exception):
                record_outcome(outcome)
            return outcome
    except Exception:  # noqa: BLE001 - AgentMux is never a run dependency
        return None


class StatePersistence(Protocol):
    """The run's serialized state writer, as the notification seam needs it.

    Declared structurally so the turn-execution boundary and this transport can
    allocate sequences and record outcomes without importing the runner that
    owns the concrete implementation.
    """

    @property
    def lock(self) -> AbstractContextManager[Any]: ...

    def append_event(
        self, event_path: Path, name: str, payload: dict[str, Any]
    ) -> None: ...

    def write_state(self, payload: dict[str, Any]) -> None: ...


def allocate_sequence(
    payload: dict[str, Any],
    persistence: StatePersistence,
) -> int:
    """Allocate the next per-run AgentMux sequence and persist it.

    Allocation happens at the persistence lock seam so the counter in
    ``state.json`` stays monotonic across every producer thread.
    """
    with persistence.lock:
        sequence = int(payload.get("agent_mux_sequence") or 0) + 1
        payload["agent_mux_sequence"] = sequence
        persistence.write_state(payload)
    return sequence


def notify_loop(
    event_name: str,
    payload: dict[str, Any],
    *,
    reason: str,
    active_turn: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
    persistence: StatePersistence | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> None:
    """Emit one run-lifecycle notification from a live run payload.

    This is the seam every phase of a run notifies through, so sequence
    allocation and outcome recording are decided once rather than per emitter.
    Without a ``persistence`` the sequence is bumped on the in-memory payload
    only, which is what an unpersisted run (tests, ``--no-state``) needs.
    """

    def allocate() -> int:
        if persistence is not None:
            return allocate_sequence(payload, persistence)
        sequence = int(payload.get("agent_mux_sequence") or 0) + 1
        payload["agent_mux_sequence"] = sequence
        return sequence

    def record_outcome(outcome: dict[str, Any]) -> None:
        raw_event_path = payload.get("events_path") or payload.get("event_path")
        if not isinstance(raw_event_path, str | Path) or not str(raw_event_path):
            return
        event_path = Path(raw_event_path)
        if persistence is not None:
            persistence.append_event(
                event_path, events.AGENT_MUX_DELIVERY_EVENT, outcome
            )
        else:
            events.append_event(
                event_path,
                events.Event(name=events.AGENT_MUX_DELIVERY_EVENT, payload=outcome),
            )

    notify(
        event_name,
        payload,
        reason=reason,
        active_turn=active_turn,
        extra=extra,
        enabled=notify_enabled(),
        timeout=notify_timeout(),
        allocate_sequence=allocate,
        record_outcome=record_outcome,
        resolve_binary=shutil.which,
        run_command=subprocess.run,
        sleep=retry_sleep,
        emitted_at=clock.utc_now,
        cancelled=cancelled,
    )
