"""The thread-safe live state of one running harness turn.

``LiveTurn._lock`` is always taken before ``TurnPersistence.lock``, never the
reverse, and neither is held across a ``ps`` sample or an AgentMux
subprocess."""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from . import (
    clock,
    config,
    events,
    harnesses,
    process_tree,
    turn_execution_constants,
    turn_execution_hang_sentinel,
    turn_execution_protocols,
    turn_execution_signals,
)


class LiveTurn:
    """Thread-safe live state and periodic-work manager for one harness turn."""

    def __init__(
        self,
        *,
        payload: dict[str, Any],
        event_path: Path,
        progress: turn_execution_protocols.TurnProgress,
        active_turn: dict[str, Any],
        persistence: turn_execution_protocols.TurnPersistence,
        heartbeat_interval: float = turn_execution_constants.HEARTBEAT_INTERVAL_SECONDS,
        poll_callback: Callable[[], None] | None = None,
        poll_interval: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        wait: Callable[[float], bool] | None = None,
        hang_sentinel: turn_execution_hang_sentinel.HangSentinel | None = None,
        input_wait_probe: Callable[[], bool] | None = None,
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
            turn_execution_hang_sentinel.HangSentinel(monotonic=monotonic)
            if hang_sentinel is None
            else hang_sentinel
        )
        self._input_wait_probe = input_wait_probe
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
            turn_execution_signals.clear_live_action_required(self._payload)
            self._payload["active_turn"] = dict(self._active_turn)
            self._persistence.write_state(self._payload)
        self._progress.turn_started(self._active_turn)
        self._thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._thread.start()

    def note_harness_pid(self, pid: int) -> None:
        self._hang_sentinel.note_harness_pid(pid)

    def note_final_result(self) -> None:
        self._hang_sentinel.note_final_result()

    def note_transport(self, transport: harnesses.HarnessTransport | None) -> None:
        self._hang_sentinel.note_input_channel(transport)

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
        request_started = stream_name == "stdout" and any(
            line == "claude status: requesting" or line.startswith("claude init")
            for line in text.splitlines()
        )
        hang_retracted = self._hang_sentinel.note_activity(
            request_started=request_started
        )
        hang_progress_cleared = False
        hang_cleared_payload: dict[str, Any] | None = None
        active_turn_snapshot: dict[str, Any] | None = None
        with self._lock, self._persistence.lock:
            self._last_activity_at = now
            self._active_turn["last_activity_at"] = clock.isoformat(now)
            self._active_turn["last_activity_stream"] = stream_name
            self._active_turn["last_activity_preview"] = (
                turn_execution_signals.preview_text(text)
            )
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
            shutdown = self._active_turn.get("harness_shutdown")
            if isinstance(shutdown, dict):
                self._active_turn["harness_shutdown"] = {
                    **shutdown,
                    "canceled_by": "activity",
                }
            if hang_retracted is not None:
                self._active_turn.pop("hang_suspected", None)
                hang_cleared_payload = self._hang_event_payload(hang_retracted, now)

            signal = turn_execution_signals.live_action_signal(stream_name, text)
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
            turn_execution_protocols.notify_turn_hang_cleared(
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
            recovery = (hang_retracted or {}).get("recovery") or (
                active_turn.get("harness_shutdown") or {}
            ).get("recovery")
            if (
                isinstance(recovery, dict)
                and recovery.get("policy")
                == process_tree.POST_RESULT_ROOT_RECOVERY_POLICY
            ):
                active_turn["harness_shutdown"] = {
                    **(active_turn.get("harness_shutdown") or {}),
                    "result": "exited-after-terminal-result",
                    "recovery": recovery,
                }
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
        assessment: turn_execution_hang_sentinel.HangAssessment | None = None
        if not self._stop.is_set():
            with self._lock:
                idle_seconds = clock.duration_seconds(self._last_activity_at, now)
            input_wait_active = False
            if self._input_wait_probe is not None:
                with contextlib.suppress(Exception):
                    input_wait_active = self._input_wait_probe()
            assessment = self._hang_sentinel.evaluate(
                idle_seconds=idle_seconds,
                now=now,
                input_wait_active=input_wait_active,
            )
            assessment = self._hang_sentinel.recover_post_result(assessment)
        stdin_hold_reason = (
            None if self._stop.is_set() else self._hang_sentinel.input_hold_reason()
        )
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
            if stdin_hold_reason is not None:
                # Why a harness that already answered still has its stdin
                # open -- the one fact that explains an idle post-result root.
                heartbeat["stdin_hold_reason"] = stdin_hold_reason
            self._active_turn["last_heartbeat_at"] = heartbeat["created_at"]
            self._active_turn["heartbeat_count"] = heartbeat_count
            self._active_turn["elapsed_seconds"] = heartbeat["elapsed_seconds"]
            self._active_turn["idle_seconds"] = heartbeat["idle_seconds"]
            if assessment is not None:
                if assessment.input_wait_active:
                    self._active_turn["input_wait_active"] = True
                    heartbeat["input_wait_active"] = True
                else:
                    self._active_turn.pop("input_wait_active", None)
                if assessment.suspected is not None:
                    self._active_turn["hang_suspected"] = assessment.suspected
                    heartbeat["hang_suspected"] = assessment.suspected
                    recovery = assessment.suspected.get("recovery")
                    if (
                        isinstance(recovery, dict)
                        and recovery.get("policy")
                        == process_tree.POST_RESULT_ROOT_RECOVERY_POLICY
                    ):
                        self._active_turn["harness_shutdown"] = {
                            "result": "recovery-attempted",
                            "recovery": recovery,
                        }
                else:
                    self._active_turn.pop("hang_suspected", None)
                    shutdown = self._active_turn.get("harness_shutdown")
                    if isinstance(shutdown, dict) and (
                        assessment.retracted is not None or assessment.input_wait_active
                    ):
                        self._active_turn["harness_shutdown"] = {
                            **shutdown,
                            "canceled_by": (
                                "input-wait"
                                if assessment.input_wait_active
                                else "cpu-progress"
                            ),
                        }
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
            turn_execution_protocols.notify_turn_heartbeat(
                self._payload,
                active_turn_snapshot,
                self._persistence,
                cancelled=self._stop.is_set,
            )
            if assessment is not None and assessment.transition == "suspected":
                block = assessment.suspected
                assert block is not None
                turn_execution_protocols.notify_turn_hang_suspected(
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
                turn_execution_protocols.notify_turn_hang_cleared(
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
