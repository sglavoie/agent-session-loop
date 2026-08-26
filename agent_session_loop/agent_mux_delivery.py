"""Reliable, fail-open delivery of ASL lifecycle events to AgentMux.

The runner owns lifecycle truth and source-sequence persistence.  This module
owns the transport mechanics: stable request identities, strict JSON
acknowledgements, bounded retry, legacy-binary fallback, and sanitized ASL-side
outcome records.  Delivery failures are always evidence, never run failures.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from . import agent_mux_projection, clock, events

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
MAX_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = (0.1, 0.3)

STATUS_ACCEPTED = "accepted"
STATUS_DUPLICATE = "duplicate"
STATUS_RETRYABLE_FAILURE = "retryable_failure"
STATUS_PERMANENT_FAILURE = "permanent_failure"
STATUS_RETRYABLE_EXHAUSTED = "retryable_exhausted"
STATUS_LEGACY_UNACKNOWLEDGED = "legacy_unacknowledged"
STATUS_UNAVAILABLE = "unavailable"

_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@+-]{0,127}$")
_UNSUPPORTED_FLAG_MARKERS = (
    "unknown flag: --harness",
    "unknown flag: --request-id",
    "unknown flag: --json",
    "unknown flag: --strict",
    "flag provided but not defined: -harness",
    "flag provided but not defined: -request-id",
    "flag provided but not defined: -json",
    "flag provided but not defined: -strict",
)

# Serializing sequence allocation with dispatch prevents a heartbeat thread
# from delivering a lower source sequence after a terminal lifecycle event.
EMIT_LOCK = threading.Lock()
_LEGACY_BINARIES: set[str] = set()


@dataclass(frozen=True)
class AttemptSummary:
    """One sanitized transport attempt suitable for ``events.jsonl``."""

    attempt: int
    mode: str
    status: str
    retryable: bool
    reason: str
    error_classification: str
    return_code: int | None = None
    trace_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "attempt": self.attempt,
            "mode": self.mode,
            "status": self.status,
            "retryable": self.retryable,
            "reason": self.reason,
            "error_classification": self.error_classification,
        }
        if self.return_code is not None:
            payload["return_code"] = self.return_code
        if self.trace_id is not None:
            payload["trace_id"] = self.trace_id
        return payload


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


def stable_request_id(source_event_id: str) -> str:
    """Derive the retry-stable request UUID from the ASL source event ID."""
    if not source_event_id:
        raise ValueError("source_event_id is required")
    return str(uuid.uuid5(uuid.NAMESPACE_URL, source_event_id))


def retry_sleep(seconds: float) -> None:
    """Sleep between retry attempts; kept as a narrow test seam."""
    time.sleep(seconds)


def reset_legacy_cache() -> None:
    """Clear process-local capability memory (tests only)."""
    with EMIT_LOCK:
        _LEGACY_BINARIES.clear()


def _safe_token(value: Any, fallback: str = "unspecified") -> str:
    if not isinstance(value, str):
        return fallback
    token = value.strip()
    if not _SAFE_TOKEN_RE.fullmatch(token):
        return fallback
    return token


def _safe_trace_id(value: Any) -> str | None:
    token = _safe_token(value, "")
    return token or None


def _unsupported_flags(stderr: str) -> bool:
    lowered = stderr.lower()
    return any(marker in lowered for marker in _UNSUPPORTED_FLAG_MARKERS)


def _decode_result(stdout: str) -> dict[str, Any] | None:
    text = stdout.strip()
    if not text:
        return None
    candidates = [text]
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) > 1:
        candidates.append(lines[-1])
    for candidate in candidates:
        try:
            decoded = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, dict):
            return decoded
    return None


def _acknowledged_command(
    amx_path: str,
    event_name: str,
    harness: str,
    request_id: str,
) -> list[str]:
    # Keep the event positional last for compatibility with older test and
    # wrapper tooling that identifies the lifecycle event via argv[-1].
    return [
        amx_path,
        "hook-indicator",
        "--harness",
        harness,
        "--request-id",
        request_id,
        "--json",
        "--strict",
        event_name,
    ]


def _invoke(
    command: list[str],
    *,
    hook_payload: Mapping[str, Any],
    timeout: float,
    run_command: Callable[..., subprocess.CompletedProcess[str]],
) -> tuple[subprocess.CompletedProcess[str] | None, AttemptSummary | None]:
    try:
        completed = run_command(
            command,
            input=json.dumps(hook_payload, separators=(",", ":")),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, AttemptSummary(
            attempt=0,
            mode="acknowledged",
            status=STATUS_RETRYABLE_FAILURE,
            retryable=True,
            reason="attempt_timeout",
            error_classification="timeout",
        )
    except OSError, subprocess.SubprocessError:
        return None, AttemptSummary(
            attempt=0,
            mode="acknowledged",
            status=STATUS_RETRYABLE_FAILURE,
            retryable=True,
            reason="process_unavailable",
            error_classification="process_error",
        )
    return completed, None


def _classify_acknowledged(
    completed: subprocess.CompletedProcess[str],
    *,
    request_id: str,
) -> AttemptSummary:
    result = _decode_result(completed.stdout or "")
    if result is None:
        retryable = completed.returncode != 2
        return AttemptSummary(
            attempt=0,
            mode="acknowledged",
            status=(
                STATUS_RETRYABLE_FAILURE if retryable else STATUS_PERMANENT_FAILURE
            ),
            retryable=retryable,
            reason="invalid_acknowledgement",
            error_classification="invalid_acknowledgement",
            return_code=completed.returncode,
        )

    returned_request_id = result.get("request_id")
    if (
        isinstance(returned_request_id, str)
        and returned_request_id
        and returned_request_id != request_id
    ):
        return AttemptSummary(
            attempt=0,
            mode="acknowledged",
            status=STATUS_RETRYABLE_FAILURE,
            retryable=True,
            reason="request_id_mismatch",
            error_classification="invalid_acknowledgement",
            return_code=completed.returncode,
            trace_id=_safe_trace_id(result.get("trace_id")),
        )

    status = _safe_token(result.get("status"), "invalid_acknowledgement")
    reason = _safe_token(result.get("reason"))
    trace_id = _safe_trace_id(result.get("trace_id"))
    if status in (STATUS_ACCEPTED, STATUS_DUPLICATE) and completed.returncode == 0:
        return AttemptSummary(
            attempt=0,
            mode="acknowledged",
            status=status,
            retryable=False,
            reason=reason,
            error_classification="none",
            return_code=completed.returncode,
            trace_id=trace_id,
        )
    if status == STATUS_PERMANENT_FAILURE or completed.returncode == 2:
        return AttemptSummary(
            attempt=0,
            mode="acknowledged",
            status=STATUS_PERMANENT_FAILURE,
            retryable=False,
            reason=reason,
            error_classification="permanent_outcome",
            return_code=completed.returncode,
            trace_id=trace_id,
        )
    if (
        status == STATUS_RETRYABLE_FAILURE
        or result.get("retryable") is True
        or completed.returncode == 75
    ):
        return AttemptSummary(
            attempt=0,
            mode="acknowledged",
            status=STATUS_RETRYABLE_FAILURE,
            retryable=True,
            reason=reason,
            error_classification="retryable_outcome",
            return_code=completed.returncode,
            trace_id=trace_id,
        )
    return AttemptSummary(
        attempt=0,
        mode="acknowledged",
        status=STATUS_RETRYABLE_FAILURE,
        retryable=True,
        reason="unexpected_exit",
        error_classification="unexpected_exit",
        return_code=completed.returncode,
        trace_id=trace_id,
    )


def _legacy_attempt(
    amx_path: str,
    event_name: str,
    *,
    attempt: int,
    hook_payload: Mapping[str, Any],
    timeout: float,
    run_command: Callable[..., subprocess.CompletedProcess[str]],
) -> AttemptSummary:
    completed, failure = _invoke(
        [amx_path, "hook-indicator", event_name],
        hook_payload=hook_payload,
        timeout=timeout,
        run_command=run_command,
    )
    if failure is not None:
        return AttemptSummary(
            attempt=attempt,
            mode="legacy",
            status=STATUS_UNAVAILABLE,
            retryable=False,
            reason=failure.reason,
            error_classification=failure.error_classification,
        )
    assert completed is not None
    if completed.returncode == 0:
        return AttemptSummary(
            attempt=attempt,
            mode="legacy",
            status=STATUS_LEGACY_UNACKNOWLEDGED,
            retryable=False,
            reason="legacy_binary",
            error_classification="legacy_binary",
            return_code=0,
        )
    return AttemptSummary(
        attempt=attempt,
        mode="legacy",
        status=STATUS_UNAVAILABLE,
        retryable=False,
        reason="legacy_process_exit",
        error_classification="legacy_process_exit",
        return_code=completed.returncode,
    )


def _deliver(
    amx_path: str,
    event_name: str,
    hook_payload: Mapping[str, Any],
    request_id: str,
    *,
    timeout: float,
    run_command: Callable[..., subprocess.CompletedProcess[str]],
    sleep: Callable[[float], None],
) -> list[AttemptSummary]:
    if amx_path in _LEGACY_BINARIES:
        return [
            _legacy_attempt(
                amx_path,
                event_name,
                attempt=1,
                hook_payload=hook_payload,
                timeout=timeout,
                run_command=run_command,
            )
        ]

    attempts: list[AttemptSummary] = []
    harness = str(hook_payload.get("harness") or "")
    command = _acknowledged_command(amx_path, event_name, harness, request_id)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        completed, failure = _invoke(
            command,
            hook_payload=hook_payload,
            timeout=timeout,
            run_command=run_command,
        )
        if failure is not None:
            summary = AttemptSummary(
                attempt=attempt,
                mode=failure.mode,
                status=failure.status,
                retryable=failure.retryable,
                reason=failure.reason,
                error_classification=failure.error_classification,
            )
        else:
            assert completed is not None
            if _unsupported_flags(completed.stderr or ""):
                attempts.append(
                    AttemptSummary(
                        attempt=attempt,
                        mode="acknowledged",
                        status=STATUS_UNAVAILABLE,
                        retryable=False,
                        reason="legacy_binary",
                        error_classification="legacy_binary",
                        return_code=completed.returncode,
                    )
                )
                _LEGACY_BINARIES.add(amx_path)
                attempts.append(
                    _legacy_attempt(
                        amx_path,
                        event_name,
                        attempt=attempt + 1,
                        hook_payload=hook_payload,
                        timeout=timeout,
                        run_command=run_command,
                    )
                )
                return attempts
            classified = _classify_acknowledged(completed, request_id=request_id)
            summary = AttemptSummary(
                attempt=attempt,
                mode=classified.mode,
                status=classified.status,
                retryable=classified.retryable,
                reason=classified.reason,
                error_classification=classified.error_classification,
                return_code=classified.return_code,
                trace_id=classified.trace_id,
            )
        attempts.append(summary)
        if not summary.retryable:
            return attempts
        if attempt < MAX_ATTEMPTS:
            sleep(RETRY_BACKOFF_SECONDS[attempt - 1])
    return attempts


def _outcome_payload(
    event_name: str,
    hook_payload: Mapping[str, Any],
    request_id: str,
    attempts: list[AttemptSummary],
) -> dict[str, Any]:
    final = attempts[-1]
    final_status = final.status
    if final.retryable and len(attempts) >= MAX_ATTEMPTS:
        final_status = STATUS_RETRYABLE_EXHAUSTED
    return {
        "run_id": hook_payload.get("run_id"),
        "agent_mux_event": event_name,
        "harness": hook_payload.get("harness"),
        "source_event_id": hook_payload.get("source_event_id"),
        "source_sequence": hook_payload.get("source_sequence"),
        "request_id": request_id,
        "trace_id": final.trace_id,
        "attempt_count": len(attempts),
        "attempts": [attempt.to_dict() for attempt in attempts],
        "final_status": final_status,
        "reason": final.reason,
        "error_classification": final.error_classification,
        "acknowledged": final.status in (STATUS_ACCEPTED, STATUS_DUPLICATE),
    }


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
