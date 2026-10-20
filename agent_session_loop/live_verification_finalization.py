"""The live record a run publishes during its terminal phase."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from . import clock, events, run_persistence, turn_execution_constants

#: The same cadence as verification's, for the run's terminal phase, which holds
#: the plan lock with no turn and no verification in flight.
FINALIZATION_HEARTBEAT_INTERVAL_SECONDS = 30.0
#: The one finalization phase this program publishes today: the terminal
#: supervisor audit. It is a phase name rather than a boolean because a run's
#: terminal work is expected to grow more of them.
FINALIZATION_PHASE_ASSURANCE_AUDIT = "assurance-audit"


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
