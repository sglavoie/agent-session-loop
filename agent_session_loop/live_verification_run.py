"""The lifecycle and heartbeat of a live `Verify` recipe record."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from typing import Any

from . import agent_mux_delivery, clock, events
from .live_verification_state import _LiveVerificationState


class _LiveVerification(_LiveVerificationState):
    """Thread-safe live state and heartbeat manager for one verification run.

    Mirrors the run loop's own ``_LiveTurn`` for the post-turn verification
    phase so the operator surfaces (state, events, progress) can show
    verification as an active phase with heartbeats instead of leaving the
    runner appearing idle while a long `Verify` recipe runs.
    """

    def start(self) -> None:
        started_at = clock.utc_now()
        with self._lock:
            self._started_at = started_at
            state_payload = self._state_payload(now=started_at)
            event_payload = self._identity_payload() | {
                "check_count": self._check_count,
                "check_names": list(self._check_names),
                "check_kinds": list(self._check_kinds),
                "check_repos": list(self._check_repos),
                "started_at": clock.isoformat(started_at),
                "session_label": self._session_label,
            }
            if self._parallel:
                event_payload["parallel"] = True
            with self._persistence.lock:
                self._payload["active_verification"] = state_payload
                self._persistence.append_event(
                    self._event_path,
                    events.VERIFICATION_STARTED_EVENT,
                    event_payload,
                )
                self._persistence.write_state(self._payload)
        self._progress.verification_started(event_payload)
        # Notify before the heartbeat thread exists so VerificationStarted
        # always carries a lower sequence than any verification heartbeat.
        self._notify_agent_mux(
            agent_mux_delivery.VERIFICATION_START_EVENT,
            agent_mux_delivery.VERIFICATION_START_REASON,
            {
                "check_count": self._check_count,
                "started_at": clock.isoformat(started_at),
            },
        )
        self._thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._thread.start()

    def check_started(
        self,
        *,
        check_index: int,
        name: str,
        kind: str,
        argv: tuple[str, ...] | None,
        cwd: str | None,
        repo: str | None = None,
    ) -> None:
        moment = clock.utc_now()
        with self._lock:
            if self._finished:
                return
            self._active_checks[check_index] = (name, kind, moment)
            self._display_most_recent_active_check()
            state_payload = self._state_payload(now=moment)
            event_payload = self._identity_payload() | {
                "check_index": check_index,
                "check_count": self._check_count,
                "name": name,
                "kind": kind,
                "repo": repo,
                "argv": list(argv) if argv is not None else None,
                "cwd": cwd,
                "started_at": clock.isoformat(moment),
            }
            with self._persistence.lock:
                self._payload["active_verification"] = state_payload
                self._persistence.append_event(
                    self._event_path,
                    events.VERIFICATION_CHECK_STARTED_EVENT,
                    event_payload,
                )
                self._persistence.write_state(self._payload)
        self._emit_progress_unless_finished(
            self._progress.verification_check_started, event_payload
        )

    def check_finished(
        self,
        *,
        check_index: int,
        name: str,
        kind: str,
        role: str,
        status: str,
        duration_seconds: float,
        failure_reason: str | None,
        returncode: int | None,
        repo: str | None = None,
        failure_diagnostic: dict[str, Any] | None = None,
    ) -> None:
        moment = clock.utc_now()
        with self._lock:
            if self._finished:
                return
            # Under a parallel recipe a background check can finish while a
            # different one is still running: hand the display slot to another
            # live check so heartbeats keep flowing, and only clear the check
            # timer once the last active check retires.
            if self._active_checks.pop(check_index, None) is not None:
                if self._active_checks:
                    if check_index == self._current_check_index:
                        self._display_most_recent_active_check()
                else:
                    self._check_started_at = None
                    self._current_check_index = check_index
                    self._current_check_name = name
                    self._current_check_kind = kind
            state_payload = self._state_payload(now=moment)
            event_payload = self._identity_payload() | {
                "check_index": check_index,
                "check_count": self._check_count,
                "name": name,
                "kind": kind,
                "role": role,
                "repo": repo,
                "status": status,
                "duration_seconds": duration_seconds,
                "failure_reason": failure_reason,
                "returncode": returncode,
                "failure_diagnostic": failure_diagnostic,
                "finished_at": clock.isoformat(moment),
            }
            with self._persistence.lock:
                self._payload["active_verification"] = state_payload
                self._persistence.append_event(
                    self._event_path,
                    events.VERIFICATION_CHECK_FINISHED_EVENT,
                    event_payload,
                )
                self._persistence.write_state(self._payload)
        self._emit_progress_unless_finished(
            self._progress.verification_check_finished, event_payload
        )

    def check_skipped(
        self,
        *,
        check_index: int,
        name: str,
        kind: str,
        on_failure_of: tuple[str, ...],
        reason: str,
        repo: str | None = None,
    ) -> None:
        moment = clock.utc_now()
        with self._lock:
            if self._finished:
                return
            event_payload = self._identity_payload() | {
                "check_index": check_index,
                "check_count": self._check_count,
                "name": name,
                "kind": kind,
                "repo": repo,
                "on_failure_of": list(on_failure_of),
                "reason": reason,
                "skipped_at": clock.isoformat(moment),
            }
            with self._persistence.lock:
                self._persistence.append_event(
                    self._event_path,
                    events.VERIFICATION_CHECK_SKIPPED_EVENT,
                    event_payload,
                )
        self._emit_progress_unless_finished(
            self._progress.verification_check_skipped, event_payload
        )

    def finish(
        self,
        *,
        passed: bool,
        passed_count: int,
        reason: str,
        skipped_count: int = 0,
        executed_count: int | None = None,
        gate_count: int | None = None,
        diagnostic_count: int = 0,
        error: str | None = None,
        acceptance: Mapping[str, Any] | None = None,
    ) -> None:
        with self._lock:
            # Claim the terminal transition before anything that blocks. A
            # parallel recipe that was abandoned mid-flight leaves command
            # checks running, and their late callbacks — including any that
            # land while the heartbeat thread is being joined — must not
            # resurrect `active_verification` or append behind
            # `verification_finished`.
            self._finished = True
            self._active_checks.clear()
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=1)
        moment = clock.utc_now()
        with self._lock:
            duration = (
                clock.duration_seconds(self._started_at, moment)
                if self._started_at is not None
                else 0.0
            )
            event_payload = self._identity_payload() | {
                "check_count": self._check_count,
                "executed_count": (
                    self._check_count - skipped_count
                    if executed_count is None
                    else executed_count
                ),
                "passed_count": passed_count,
                "skipped_count": skipped_count,
                "gate_count": self._check_count if gate_count is None else gate_count,
                "diagnostic_count": diagnostic_count,
                "passed": passed,
                "reason": reason,
                "duration_seconds": duration,
                "finished_at": clock.isoformat(moment),
            }
            if error is not None:
                event_payload["error"] = error
            if acceptance is not None:
                event_payload["acceptance"] = dict(acceptance)
            with self._persistence.lock:
                self._payload["active_verification"] = None
                self._persistence.append_event(
                    self._event_path,
                    events.VERIFICATION_FINISHED_EVENT,
                    event_payload,
                )
                self._persistence.write_state(self._payload)
        with self._progress_lock:
            self._progress.verification_finished(event_payload)
        self._notify_agent_mux(
            agent_mux_delivery.VERIFICATION_FINISH_EVENT,
            agent_mux_delivery.VERIFICATION_FINISH_REASON,
            {
                "check_count": self._check_count,
                "executed_count": (
                    self._check_count - skipped_count
                    if executed_count is None
                    else executed_count
                ),
                "passed_count": passed_count,
                "skipped_count": skipped_count,
                "gate_count": self._check_count if gate_count is None else gate_count,
                "diagnostic_count": diagnostic_count,
                "passed": passed,
                "heartbeat_count": self._heartbeat_count,
                **({"acceptance": dict(acceptance)} if acceptance is not None else {}),
            },
        )

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self._heartbeat_interval):
            heartbeat = self._record_heartbeat()
            if heartbeat is not None:
                self._emit_progress_unless_finished(
                    self._progress.verification_heartbeat, heartbeat
                )

    def _record_heartbeat(self) -> dict[str, Any] | None:
        moment = clock.utc_now()
        with self._lock:
            if (
                self._finished
                or self._started_at is None
                or self._check_started_at is None
            ):
                return None
            self._heartbeat_count += 1
            self._last_heartbeat_at = moment
            state_payload = self._state_payload(now=moment)
            heartbeat = self._identity_payload() | {
                "check_index": (
                    self._current_check_index
                    if self._current_check_index is not None
                    else 0
                ),
                "check_count": self._check_count,
                "name": (
                    self._current_check_name
                    if self._current_check_name is not None
                    else ""
                ),
                "kind": (
                    self._current_check_kind
                    if self._current_check_kind is not None
                    else ""
                ),
                "elapsed_seconds": state_payload["elapsed_seconds"],
                "check_elapsed_seconds": (
                    state_payload["check_elapsed_seconds"] or 0.0
                ),
                "heartbeat_count": self._heartbeat_count,
                "created_at": clock.isoformat(moment),
            }
            with self._persistence.lock:
                self._payload["active_verification"] = state_payload
                self._persistence.append_event(
                    self._event_path,
                    events.VERIFICATION_HEARTBEAT_EVENT,
                    heartbeat,
                )
                self._persistence.write_state(self._payload)
        # Notify outside both locks; skip once verification is stopping so a
        # late heartbeat cannot outrace the verification-finished notification.
        if not self._stop.is_set():
            self._notify_agent_mux(
                agent_mux_delivery.VERIFICATION_HEARTBEAT_EVENT,
                agent_mux_delivery.VERIFICATION_HEARTBEAT_REASON,
                {
                    "check_index": heartbeat["check_index"],
                    "check_count": heartbeat["check_count"],
                    "check_name": heartbeat["name"],
                    "check_kind": heartbeat["kind"],
                    "heartbeat_count": heartbeat["heartbeat_count"],
                    "last_heartbeat_at": heartbeat["created_at"],
                },
                cancelled=self._stop.is_set,
            )
        return heartbeat
