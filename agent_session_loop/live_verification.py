"""The two live records a run publishes when no turn is in flight.

A run that is neither launching a harness nor between sessions is not
necessarily idle. Two phases can hold the plan lock for many minutes with no
turn running at all: the post-turn `Verify` recipe, which may shell out to a
whole test suite, and the terminal work after the last turn, whose supervisor
audit is itself a harness turn with its own answer and delivery budgets. From
the outside both are indistinguishable from a hang unless something publishes
them. This module is that something. It owns the two thread-safe holders that
announce those phases while they run — :class:`_LiveVerification` and
:class:`_LiveFinalization` — so the loop is left deciding *when* a phase begins
and ends rather than reimplementing *how* a background thread stamps liveness
onto a record other threads are also writing.

Both are per-run mutable holders rather than process globals: one is
constructed per verification recipe, one per terminal phase, and each is handed
the collaborators it writes through instead of reaching for them. The durable
records they own are:

* ``state.json``'s ``active_verification`` key and the five verification events
  — started, check-started, check-finished, check-skipped, finished — plus the
  ``verification_heartbeat`` its background thread appends on a cadence;
* ``state.json``'s ``active_finalization`` key and the three finalization
  events — started, heartbeat, finished.

Every one of those writes goes through the :class:`_RunPersistence` handle the
caller supplied, under that handle's lock, so this module never opens a file of
its own. Verification additionally announces itself to the AgentMux projection
through ``run_persistence._notify_agent_mux_loop``, looked up on the module at
call time rather than bound at definition, so a patched notifier is still the
one reached; finalization publishes no notification at all, because its whole
audience is the state record an operator or a supervisor reads.

Five invariants survive the move and are load-bearing:

* **The terminal transition is claimed before anything that blocks.**
  :meth:`_LiveVerification.finish` sets ``_finished`` and clears the active
  checks under ``_lock`` *before* it stops and joins the heartbeat thread. A
  parallel recipe that was abandoned mid-flight leaves command checks running,
  and their late callbacks — including any that land while the thread is being
  joined — must not resurrect ``active_verification`` or append behind
  ``verification_finished``.
* **State and output take different locks.** ``_lock`` guards the live state
  and the event append; ``_progress_lock`` orders terminal output only. Keeping
  them distinct is what stops a slow stderr pipe from stalling a worker
  callback's state mutation under a parallel recipe. The persistence handle's
  lock is always the innermost of the three.
* **Notifications are ordered against the live record and taken outside every
  lock.** The verification-started notification is emitted before the heartbeat
  thread exists, so it always carries a lower sequence than any heartbeat, and
  a heartbeat notification is skipped once ``_stop`` is set so a late one
  cannot outrace the verification-finished notification.
* **A published phase is closed exactly once.**
  :meth:`_LiveFinalization.finish` returns immediately on a second call, which
  is what lets a cancellable terminal phase be closed by whichever of the audit
  and the abort path reaches it first without appending two
  ``finalization_finished`` records.
* **A parallel recipe always has something to display.** More than one check is
  live at a time, so the display slot is drawn from the active set: a
  background check that finishes hands the slot to another live check, and the
  check timer is cleared only when the last one retires. Without that a
  heartbeat mid-recipe would report no running check at all.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from . import (
    agent_mux_delivery,
    clock,
    events,
    plans_payloads,
    run_persistence,
    run_reporting,
    turn_execution_constants,
)

#: How often a running `Verify` recipe stamps liveness onto ``state.json`` and
#: the event log. A whole test suite can legitimately run for minutes without
#: producing a single check transition, so the cadence is what separates a slow
#: recipe from a stalled one.
VERIFICATION_HEARTBEAT_INTERVAL_SECONDS = 30.0
#: The same cadence for the run's terminal phase, which holds the plan lock
#: with no turn and no verification in flight.
FINALIZATION_HEARTBEAT_INTERVAL_SECONDS = 30.0
#: The one finalization phase this program publishes today: the terminal
#: supervisor audit. It is a phase name rather than a boolean because a run's
#: terminal work is expected to grow more of them.
FINALIZATION_PHASE_ASSURANCE_AUDIT = "assurance-audit"


class _LiveVerification:
    """Thread-safe live state and heartbeat manager for one verification run.

    Mirrors the run loop's own ``_LiveTurn`` for the post-turn verification
    phase so the operator surfaces (state, events, progress) can show
    verification as an active phase with heartbeats instead of leaving the
    runner appearing idle while a long `Verify` recipe runs.
    """

    def __init__(
        self,
        *,
        event_path: Path,
        payload: dict[str, Any],
        progress: run_reporting._ProgressReporter,
        persistence: run_persistence._RunPersistence,
        run_id: str | None,
        turn_index: int,
        session_id: str,
        session_label: str,
        total_sessions: int | None,
        check_count: int,
        check_names: tuple[str, ...],
        check_kinds: tuple[str, ...],
        check_repos: tuple[str | None, ...] = (),
        parallel: bool = False,
        heartbeat_interval: float = VERIFICATION_HEARTBEAT_INTERVAL_SECONDS,
    ) -> None:
        self._event_path = event_path
        self._payload = payload
        self._progress = progress
        self._persistence = persistence
        self._run_id = run_id
        self._turn_index = turn_index
        self._session_id = session_id
        self._session_label = session_label
        self._total_sessions = total_sessions
        self._check_count = check_count
        self._check_names = check_names
        self._check_kinds = check_kinds
        self._check_repos = check_repos if check_repos else (None,) * check_count
        self._parallel = parallel
        self._heartbeat_interval = heartbeat_interval
        self._lock = threading.Lock()
        # Ordering lock for progress output only. Kept distinct from ``_lock``
        # so a slow stderr pipe can never stall a worker callback's state
        # mutation and event append under a parallel recipe.
        self._progress_lock = threading.Lock()
        self._stop = threading.Event()
        self._finished = False
        self._thread: threading.Thread | None = None
        self._started_at: datetime | None = None
        self._check_started_at: datetime | None = None
        self._current_check_index: int | None = None
        self._current_check_name: str | None = None
        self._current_check_kind: str | None = None
        # index -> (name, kind, started_at) for every check that has started
        # and not yet finished; under a parallel recipe more than one is live
        # at a time and the display slot is drawn from this set.
        self._active_checks: dict[int, tuple[str, str, datetime]] = {}
        self._heartbeat_count = 0
        self._last_heartbeat_at: datetime | None = None

    def _emit_progress_unless_finished(
        self,
        emit: Callable[[dict[str, Any]], None],
        payload: dict[str, Any],
    ) -> None:
        """Write progress only while verification is still live.

        ``finish`` claims ``_finished`` before it takes this same lock for the
        terminal message, so a callback either writes ahead of that message or
        is dropped; no check progress can land behind it.
        """
        with self._progress_lock:
            if self._finished:
                return
            emit(payload)

    def _display_most_recent_active_check(self) -> None:
        index = next(reversed(self._active_checks))
        name, kind, started_at = self._active_checks[index]
        self._current_check_index = index
        self._current_check_name = name
        self._current_check_kind = kind
        self._check_started_at = started_at

    def _identity_payload(self) -> dict[str, Any]:
        identity: dict[str, Any] = {
            "run_id": self._run_id,
            "turn_index": self._turn_index,
            "session": plans_payloads.session_compat_value(self._session_id),
            "session_id": self._session_id,
            "total_sessions": self._total_sessions,
        }
        return identity

    def _state_payload(self, *, now: datetime | None = None) -> dict[str, Any]:
        moment = clock.utc_now() if now is None else now
        elapsed = (
            clock.duration_seconds(self._started_at, moment)
            if self._started_at is not None
            else 0.0
        )
        check_elapsed = (
            clock.duration_seconds(self._check_started_at, moment)
            if self._check_started_at is not None
            else None
        )
        active_state = self._identity_payload()
        active_state.update(
            {
                "check_count": self._check_count,
                "check_names": list(self._check_names),
                "check_kinds": list(self._check_kinds),
                "check_repos": list(self._check_repos),
                "started_at": (
                    clock.isoformat(self._started_at)
                    if self._started_at is not None
                    else None
                ),
                "current_check_index": self._current_check_index,
                "current_check_name": self._current_check_name,
                "current_check_kind": self._current_check_kind,
                "current_check_started_at": (
                    clock.isoformat(self._check_started_at)
                    if self._check_started_at is not None
                    else None
                ),
                "elapsed_seconds": elapsed,
                "check_elapsed_seconds": check_elapsed,
                "heartbeat_count": self._heartbeat_count,
                "last_heartbeat_at": (
                    clock.isoformat(self._last_heartbeat_at)
                    if self._last_heartbeat_at is not None
                    else None
                ),
            }
        )
        return active_state

    def _notify_agent_mux(
        self,
        event_name: str,
        reason: str,
        extra: dict[str, Any],
        cancelled: Callable[[], bool] | None = None,
    ) -> None:
        # Looked up on the module per call rather than bound at definition, so
        # a patched loop notifier is still the one this phase announces to.
        run_persistence._notify_agent_mux_loop(
            event_name,
            self._payload,
            reason=reason,
            extra=self._identity_payload() | extra,
            persistence=self._persistence,
            cancelled=cancelled,
        )

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


class _LiveFinalization:
    """Thread-safe live state and heartbeat manager for a run's terminal work.

    Mirrors :class:`_LiveVerification` for the phase between the last turn and
    ``run_finished``. The terminal supervisor audit is a harness turn with its
    own answer and delivery budgets, so a run can legitimately spend many
    minutes here holding the plan lock with no turn and no verification in
    flight — a state indistinguishable from a hang until this phase publishes
    itself. ``poll`` is the same control-queue drain the turn loop ticks, which
    is what keeps a second abort observable once the loop has exited.
    """

    def __init__(
        self,
        *,
        event_path: Path,
        payload: dict[str, Any],
        persistence: run_persistence._RunPersistence,
        run_id: str | None,
        phase: str,
        cancellable: bool,
        poll: Callable[[], None] | None = None,
        heartbeat_interval: float = FINALIZATION_HEARTBEAT_INTERVAL_SECONDS,
        poll_interval: float = (
            turn_execution_constants.LIVE_CONTROL_POLL_INTERVAL_SECONDS
        ),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._event_path = event_path
        self._payload = payload
        self._persistence = persistence
        self._run_id = run_id
        self._phase = phase
        self._cancellable = cancellable
        self._poll = poll
        self._heartbeat_interval = heartbeat_interval
        self._poll_interval = poll_interval
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._finished = False
        self._thread: threading.Thread | None = None
        self._started_at: datetime | None = None
        self._heartbeat_count = 0
        self._last_heartbeat_at: datetime | None = None

    def _state_payload(self, *, now: datetime | None = None) -> dict[str, Any]:
        moment = clock.utc_now() if now is None else now
        return {
            "run_id": self._run_id,
            "phase": self._phase,
            "started_at": (
                clock.isoformat(self._started_at)
                if self._started_at is not None
                else None
            ),
            "elapsed_seconds": (
                clock.duration_seconds(self._started_at, moment)
                if self._started_at is not None
                else 0.0
            ),
            "heartbeat_count": self._heartbeat_count,
            "last_heartbeat_at": (
                clock.isoformat(self._last_heartbeat_at)
                if self._last_heartbeat_at is not None
                else None
            ),
            "cancellable": self._cancellable,
        }

    def start(self) -> None:
        started_at = clock.utc_now()
        with self._lock:
            self._started_at = started_at
            state_payload = self._state_payload(now=started_at)
            with self._persistence.lock:
                self._payload["active_finalization"] = state_payload
                self._persistence.append_event(
                    self._event_path,
                    events.FINALIZATION_STARTED_EVENT,
                    {
                        "run_id": self._run_id,
                        "phase": self._phase,
                        "started_at": clock.isoformat(started_at),
                        "cancellable": self._cancellable,
                    },
                )
                self._persistence.write_state(self._payload)
        self._thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._thread.start()

    def finish(self, *, cancelled: bool = False) -> None:
        with self._lock:
            if self._finished:
                return
            self._finished = True
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=1)
        moment = clock.utc_now()
        with self._lock:
            event_payload = {
                "run_id": self._run_id,
                "phase": self._phase,
                "cancelled": cancelled,
                "heartbeat_count": self._heartbeat_count,
                "duration_seconds": (
                    clock.duration_seconds(self._started_at, moment)
                    if self._started_at is not None
                    else 0.0
                ),
                "finished_at": clock.isoformat(moment),
            }
            with self._persistence.lock:
                self._payload["active_finalization"] = None
                self._persistence.append_event(
                    self._event_path,
                    events.FINALIZATION_FINISHED_EVENT,
                    event_payload,
                )
                self._persistence.write_state(self._payload)

    def _heartbeat_loop(self) -> None:
        started = self._monotonic()
        next_heartbeat = started + self._heartbeat_interval
        next_poll = started + self._poll_interval if self._poll is not None else None
        while True:
            deadline = (
                next_heartbeat if next_poll is None else min(next_heartbeat, next_poll)
            )
            if self._stop.wait(max(0.0, deadline - self._monotonic())):
                return
            now = self._monotonic()
            if now >= next_heartbeat:
                next_heartbeat = now + self._heartbeat_interval
                self._record_heartbeat()
            if next_poll is not None and now >= next_poll:
                next_poll = now + self._poll_interval
                assert self._poll is not None
                self._poll()

    def _record_heartbeat(self) -> None:
        moment = clock.utc_now()
        with self._lock:
            if self._finished or self._started_at is None:
                return
            self._heartbeat_count += 1
            self._last_heartbeat_at = moment
            state_payload = self._state_payload(now=moment)
            with self._persistence.lock:
                self._payload["active_finalization"] = state_payload
                self._persistence.append_event(
                    self._event_path,
                    events.FINALIZATION_HEARTBEAT_EVENT,
                    state_payload | {"created_at": clock.isoformat(moment)},
                )
                self._persistence.write_state(self._payload)
