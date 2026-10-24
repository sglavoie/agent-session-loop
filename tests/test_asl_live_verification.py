"""The two live records a run publishes when no turn is in flight.

The behavioural coverage of what those records *say* lives where it always has
— ``tests/test_asl_harness_*.py`` drives whole verification recipes
and reads their state, events and progress lines back, and
``tests/test_asl_assurance_lifecycle_*.py`` owns the terminal phase an audit runs
inside. What is asserted here is the contract those modules cannot state,
because they exercise the run rather than the seam:

* the module does not import the runner, and needs nothing from it;
* it opens no file of its own — every durable record it owns is written through
  the ``_RunPersistence`` handle the caller supplied, under that handle's lock,
  and a second write path here would be a write outside the run's only mutex;
* verification announces through ``run_persistence._notify_agent_mux_loop``
  looked up per call rather than bound at definition, so a patched loop
  notifier is still the one reached;
* finalization publishes no notification at all, so its audience is exactly the
  state record and nothing else; and
* the two ordering guarantees that make a heartbeat trustworthy: a heartbeat
  notification is dropped once the phase is stopping, and a published phase is
  closed exactly once however many callers reach its close.

The seam group is the reason this file exists. These two classes are the only
thing standing between "a run is working" and "a run has hung" for an operator
watching from outside, and every failure mode here is silent: a record that
stops being stamped looks like a hang, a phase closed twice looks like a
lifecycle that never happened, and a notification that outraces its own
finished event tells a supervisor the opposite of the truth.
"""

from __future__ import annotations

import ast
import inspect
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    agent_mux_delivery,
    events,
    live_verification,
    live_verification_finalization,
    live_verification_run,
    live_verification_state,
    run_persistence,
    run_reporting,
    runner,
)
from tests.support import assert_no_runner_import, read_jsonl, wait_until

ASL_TEST_PORTABLE = True

#: The symbols this module took from the runner. The program's no-alias rule
#: says the runner keeps none of them behind, so a re-export added later to
#: rescue a stale patch target fails here rather than in review.
MOVED_SYMBOLS = (
    "_LiveVerification",
    "_LiveFinalization",
    "VERIFICATION_HEARTBEAT_INTERVAL_SECONDS",
    "FINALIZATION_HEARTBEAT_INTERVAL_SECONDS",
    "FINALIZATION_PHASE_ASSURANCE_AUDIT",
)

#: The two durable writes this boundary makes. Both are methods on the run's
#: persistence handle; nothing here may reach a filesystem another way.
DURABLE_WRITE_METHODS = ("append_event", "write_state")


class _RecordingNotifier:
    """A stand-in for ``run_persistence._notify_agent_mux_loop`` that records."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(
        self,
        event_name: str,
        payload: dict[str, Any],
        *,
        reason: str,
        extra: dict[str, Any] | None = None,
        persistence: run_persistence._RunPersistence | None = None,
        cancelled: Any = None,
    ) -> None:
        self.calls.append((event_name, dict(extra or {})))

    @property
    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


def _durable_write_receivers(source: str) -> set[str]:
    """Every receiver a durable-write method is called on in ``source``."""
    receivers: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in DURABLE_WRITE_METHODS:
            receivers.add(ast.unparse(func.value))
    return receivers


class LiveVerificationBoundaryTests(unittest.TestCase):
    """``live_verification`` is runner-free and writes through one handle."""

    def setUp(self) -> None:
        self.source = "\n".join(
            Path(module.__file__ or "").read_text(encoding="utf-8")
            for module in (
                live_verification,
                live_verification_finalization,
                live_verification_run,
                live_verification_state,
            )
        )

    def test_live_verification_does_not_import_the_runner(self) -> None:
        assert_no_runner_import(self, live_verification)

    def test_the_module_opens_no_file_of_its_own(self) -> None:
        # Unlike `run_reporting` this boundary is not ephemeral: it owns two
        # state keys and eight event names. What makes that safe from a
        # heartbeat thread is that none of it is written here -- it all goes
        # through the run's single mutex, which the handle owns.
        for reach in ("write_text(", "open(", "mkdir(", "unlink(", "durability."):
            self.assertNotIn(reach, self.source, msg=reach)

    def test_every_durable_write_goes_through_the_persistence_handle(self) -> None:
        self.assertEqual({"self._persistence"}, _durable_write_receivers(self.source))

    def test_the_runner_keeps_no_alias_for_the_moved_symbols(self) -> None:
        for name in MOVED_SYMBOLS:
            with self.subTest(name=name):
                self.assertFalse(hasattr(runner, name))
                self.assertTrue(hasattr(live_verification, name))


class _LiveRecordCase(unittest.TestCase):
    """Shared scaffolding: a real persistence handle over a temp directory."""

    def setUp(self) -> None:
        self.tmp_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.event_path = self.tmp_dir / events.EVENTS_FILE_NAME
        self.payload: dict[str, Any] = {
            "state_path": str(self.tmp_dir / events.STATE_FILE_NAME),
            "run_id": "run-1",
            "active_turn": None,
            "active_verification": None,
            "active_finalization": None,
        }
        self.persistence = run_persistence._RunPersistence()

    def _verification(self, **overrides: Any) -> live_verification._LiveVerification:
        kwargs: dict[str, Any] = {
            "event_path": self.event_path,
            "payload": self.payload,
            "progress": run_reporting._ProgressReporter(enabled=False),
            "persistence": self.persistence,
            "run_id": "run-1",
            "turn_index": 1,
            "session_id": "1",
            "session_label": "1/2",
            "total_sessions": 2,
            "check_count": 1,
            "check_names": ("suite",),
            "check_kinds": ("command",),
            "heartbeat_interval": 3600.0,
        }
        kwargs.update(overrides)
        return live_verification._LiveVerification(**kwargs)

    def _finalization(self, **overrides: Any) -> live_verification._LiveFinalization:
        kwargs: dict[str, Any] = {
            "event_path": self.event_path,
            "payload": self.payload,
            "persistence": self.persistence,
            "run_id": "run-1",
            "phase": live_verification.FINALIZATION_PHASE_ASSURANCE_AUDIT,
            "cancellable": False,
            "heartbeat_interval": 3600.0,
        }
        kwargs.update(overrides)
        return live_verification._LiveFinalization(**kwargs)

    def _event_names(self) -> list[str]:
        if not self.event_path.exists():
            return []
        return [record["name"] for record in read_jsonl(self.event_path)]


class AgentMuxSeamTests(_LiveRecordCase):
    """Verification announces through the loop's notifier, resolved per call."""

    def test_verification_announces_through_the_loop_notifier(self) -> None:
        # Looked up on the module per call rather than bound at definition: a
        # default captured at import would silently escape a patched notifier,
        # and this is the run's only fail-open announcement path.
        notifier = _RecordingNotifier()

        live = self._verification()
        with mock.patch.object(run_persistence, "_notify_agent_mux_loop", notifier):
            live.start()
            live.finish(passed=True, passed_count=1, reason="ok")

        self.assertEqual(
            [
                agent_mux_delivery.VERIFICATION_START_EVENT,
                agent_mux_delivery.VERIFICATION_FINISH_EVENT,
            ],
            notifier.names,
        )
        # The run's identity is merged into every announcement, so a listener
        # can attribute one to a session without reading the state file.
        for _, extra in notifier.calls:
            self.assertEqual("run-1", extra["run_id"])
            self.assertEqual("1", extra["session_id"])

    def test_the_holder_takes_no_notifier_of_its_own(self) -> None:
        # Session 29 collapsed the `notify` construction seam: it had one
        # implementation, no caller ever passed it, and this module already
        # imports the module that owns it. Re-adding it would fork the
        # announcement path again.
        self.assertNotIn(
            "notify",
            inspect.signature(live_verification._LiveVerification).parameters,
        )

    def test_finalization_publishes_no_notification(self) -> None:
        # The terminal phase's whole audience is the state record an operator
        # or a supervisor reads, so it takes no notifier and reaches none.
        seen: list[str] = []

        def record(event_name: str, payload: dict[str, Any], **kwargs: Any) -> None:
            seen.append(event_name)

        phase = self._finalization()
        with mock.patch.object(run_persistence, "_notify_agent_mux_loop", record):
            phase.start()
            phase.finish()

        self.assertEqual([], seen)
        self.assertEqual(
            [
                events.FINALIZATION_STARTED_EVENT,
                events.FINALIZATION_FINISHED_EVENT,
            ],
            self._event_names(),
        )


class HeartbeatOrderingTests(_LiveRecordCase):
    """A heartbeat may never contradict the record it is stamping."""

    def test_a_heartbeat_notification_is_dropped_once_the_phase_is_stopping(
        self,
    ) -> None:
        notifier = _RecordingNotifier()
        live = self._verification()
        with mock.patch.object(run_persistence, "_notify_agent_mux_loop", notifier):
            live.start()
            live.check_started(
                check_index=0, name="suite", kind="command", argv=("just",), cwd=None
            )
            notifier.calls.clear()

            # `finish` sets `_stop` before it appends `verification_finished`.
            # A heartbeat that lands in that window still updates the durable
            # record -- it happened -- but must not announce, or a supervisor
            # reads "still running" after the finish notification.
            live._stop.set()
            self.assertIsNotNone(live._record_heartbeat())

        self.assertEqual([], notifier.names)
        self.assertIn(events.VERIFICATION_HEARTBEAT_EVENT, self._event_names())

    def test_the_started_notification_precedes_the_heartbeat_thread(self) -> None:
        # Ordering by construction rather than by luck: the thread that can
        # emit a heartbeat does not exist until `start` has announced itself,
        # so no heartbeat can carry a lower sequence than the start.
        thread_at_notify: list[Any] = []

        def notify(event_name: str, payload: dict[str, Any], **kwargs: Any) -> None:
            thread_at_notify.append(live._thread)

        live = self._verification()
        with mock.patch.object(run_persistence, "_notify_agent_mux_loop", notify):
            live.start()
            live.finish(passed=True, passed_count=1, reason="ok")

        self.assertIsNone(thread_at_notify[0])
        self.assertIsNotNone(thread_at_notify[1])


class FinalizationPhaseTests(_LiveRecordCase):
    """A published phase is closed exactly once, and drains on its own clock."""

    def test_a_published_phase_is_closed_exactly_once(self) -> None:
        # A cancellable terminal phase has two callers that may reach its
        # close -- the audit finishing and the abort path -- and two
        # `finalization_finished` records would describe a lifecycle the run
        # never had.
        phase = self._finalization(cancellable=True)
        phase.start()
        phase.finish()
        phase.finish(cancelled=True)

        names = self._event_names()
        self.assertEqual(1, names.count(events.FINALIZATION_FINISHED_EVENT))
        records = read_jsonl(self.event_path)
        self.assertFalse(records[-1]["payload"]["cancelled"])
        self.assertIsNone(self.payload["active_finalization"])

    def test_the_control_drain_ticks_independently_of_the_heartbeat(self) -> None:
        # The drain is what keeps a second abort observable once the loop has
        # exited, so it must not inherit the heartbeat's cadence: an operator
        # pressing abort during a long audit cannot wait 30s per tick.
        polled = threading.Event()
        phase = self._finalization(
            heartbeat_interval=3600.0,
            poll_interval=0.01,
            poll=polled.set,
        )
        phase.start()
        try:
            wait_until(polled.is_set)
        finally:
            phase.finish()

        self.assertEqual(
            [
                events.FINALIZATION_STARTED_EVENT,
                events.FINALIZATION_FINISHED_EVENT,
            ],
            self._event_names(),
        )


if __name__ == "__main__":  # pragma: no cover - convenience entry point
    unittest.main()
