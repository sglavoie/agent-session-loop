"""AgentMux transport mechanics: bounded retry, acknowledgements, legacy fallback.

``agent_mux_delivery`` owns the lifecycle vocabulary and the notification
seam; this module owns one event's delivery to the ``amx`` binary and the
sanitized outcome record built from its attempts.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

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
