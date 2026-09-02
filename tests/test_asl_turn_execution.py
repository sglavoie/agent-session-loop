"""The typed turn-execution boundary, exercised without the run loop.

``TurnExecutor`` is the single entry and exit for spending a turn, collecting
its exited process, and publishing the accepted result. These tests drive it
through collaborators defined here rather than through ``runner``, which is
the point of the boundary: if it only worked against the run loop's own
persistence and progress objects, it would not be a boundary at all.

The five endings a turn can have -- success, failure, timeout, interrupt, and
a resumed turn following an earlier one -- are covered as properties of the
records the executor returns and the events it appends, not as call
assertions, because those records are what the ledger and the next turn read.
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any
from unittest import mock

from agent_session_loop import (
    clock,
    events,
    harnesses,
    plans,
    run_persistence,
    runner,
    turn_execution,
)

from .support import assert_no_runner_import, write_plan


class RecordingPersistence:
    """A :class:`turn_execution.TurnPersistence` that keeps what it was told.

    Events are kept in order and state writes are kept as snapshots, so a test
    can assert on sequence without reading files back. The lock is real: the
    heartbeat thread takes it concurrently with the calling thread.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.states: list[dict[str, Any]] = []

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def append_event(self, path: Path, name: str, payload: dict[str, Any]) -> None:
        self.events.append((name, json.loads(json.dumps(payload, default=str))))

    def write_state(self, payload: dict[str, Any]) -> None:
        self.states.append(json.loads(json.dumps(payload, default=str)))

    def names(self) -> list[str]:
        return [name for name, _payload in self.events]

    def payloads(self, name: str) -> list[dict[str, Any]]:
        return [payload for event, payload in self.events if event == name]


class RecordingProgress:
    """A :class:`turn_execution.TurnProgress` that reports to a list."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def turn_started(self, active_turn: dict[str, Any]) -> None:
        self.calls.append("turn_started")

    def turn_finished(self, turn_payload: dict[str, Any]) -> None:
        self.calls.append("turn_finished")

    def heartbeat(self, active_turn: dict[str, Any]) -> None:
        self.calls.append("heartbeat")

    def action_required(self, signal: dict[str, Any]) -> None:
        self.calls.append("action_required")


@contextmanager
def silent_delivery() -> Iterator[None]:
    """Suppress AgentMux delivery: it is a separate seam with its own tests."""
    with mock.patch.object(
        turn_execution.agent_mux_delivery, "notify_loop", return_value=None
    ):
        yield


class TurnExecutorHarness:
    """One plan, one run directory, and one executor bound to turn ``index``."""

    def __init__(self, root: Path, *, turn_index: int = 1) -> None:
        self.root = root
        self.plan_dir = root / "plan"
        write_plan(self.plan_dir, ("pending",))
        self.before = plans.inspect_plan(self.plan_dir)
        self.persistence = RecordingPersistence()
        self.progress = RecordingProgress()
        self.plan_cache = plans.PlanSnapshotCache()
        self.event_path = root / "events.jsonl"
        self.turn_index = turn_index
        self.paths = turn_execution.turn_paths(
            root / "run", turn_index=turn_index, session_id="1"
        )
        self.payload: dict[str, Any] = {
            "run_id": "run-1",
            "run_started_at": clock.utc_iso_now(),
            "active_turn": None,
            "turns": [],
        }
        self.executor = turn_execution.TurnExecutor(
            identity=turn_execution.TurnIdentity(
                run_id="run-1", turn_index=turn_index, session_id="1"
            ),
            paths=self.paths,
            event_path=self.event_path,
            persistence=self.persistence,
            progress=self.progress,
            plan_cache=self.plan_cache,
        )

    def names(self) -> list[str]:
        return self.persistence.names()

    def active_turn(self) -> dict[str, Any]:
        started_at = clock.utc_iso_now()
        return {
            "run_id": "run-1",
            "turn_index": self.turn_index,
            **plans.session_identity_payload("1"),
            "session_kind": plans.SESSION_KIND_IMPLEMENTATION,
            "total_sessions": 1,
            "artifact_dir": str(self.paths["artifact_dir"]),
            "prompt": "do the work",
            "harness": "codex",
            "model": None,
            "model_source": None,
            "observed_model": None,
            "observed_model_source": None,
            "effort": None,
            "effort_source": None,
            "spark_route": None,
            "stdout_path": str(self.paths["stdout"]),
            "stderr_path": str(self.paths["stderr"]),
            "last_message_path": str(self.paths["last_message"]),
            "result_path": str(self.paths["result"]),
            "started_at": started_at,
            "finished_at": None,
            "duration_seconds": None,
            "idle_seconds_since_previous_turn": None,
            "last_activity_at": started_at,
            "last_activity_stream": None,
            "last_activity_preview": "",
            "last_heartbeat_at": None,
            "elapsed_seconds": 0.0,
            "idle_seconds": 0.0,
            "heartbeat_count": 0,
            "stdout_chars": 0,
            "stderr_chars": 0,
            "action_required": None,
        }

    def live_turn(self, active_turn: dict[str, Any]) -> turn_execution.LiveTurn:
        return turn_execution.LiveTurn(
            payload=self.payload,
            event_path=self.event_path,
            progress=self.progress,
            active_turn=active_turn,
            persistence=self.persistence,
            # Far enough out that no heartbeat fires inside a test; the
            # heartbeat cadence has its own coverage in the control suite.
            heartbeat_interval=3600.0,
        )

    def launch_spec(self) -> turn_execution.TurnLaunch:
        invocation = harnesses.HarnessInvocation(
            name="codex",
            argv=["codex", "exec", "do the work"],
            cwd=self.root,
        )
        return turn_execution.TurnLaunch(
            harness="codex",
            cwd=self.root,
            plan_dir=self.plan_dir,
            prompt="do the work",
            unsafe_bypass=False,
            timeout=None,
            model=None,
            effort=None,
            abort=harnesses.AbortSignal(),
            live_delivery=None,
            stream_callback=None,
            build_invocation=lambda: invocation,
        )

    def run(
        self, result: harnesses.HarnessResult | BaseException
    ) -> turn_execution.TurnExecution:
        """Launch a turn whose harness produces ``result`` (or raises it)."""
        active_turn = self.active_turn()
        live_turn = self.live_turn(active_turn)
        side_effect: Any = (
            result if isinstance(result, BaseException) else lambda *a, **k: result
        )
        with (
            silent_delivery(),
            mock.patch.object(harnesses, "run_harness", side_effect=side_effect),
        ):
            return self.executor.launch(
                self.launch_spec(),
                live_turn=live_turn,
                active_turn=active_turn,
                payload=self.payload,
            )


def harness_result(**overrides: Any) -> harnesses.HarnessResult:
    fields: dict[str, Any] = {
        "name": "codex",
        "argv": ["codex", "exec", "do the work"],
        "returncode": 0,
        "stdout": "working\n",
        "stderr": "",
        "last_message": "done\n",
    }
    fields.update(overrides)
    return harnesses.HarnessResult(**fields)


class TurnExecutorBoundaryTests(unittest.TestCase):
    """The shape of the boundary itself, independent of any single ending."""

    def test_the_run_loop_collaborators_satisfy_the_protocols(self) -> None:
        # The Protocols exist so turn_execution never imports runner; that is
        # only true if the runner's own objects still fit through them.
        persistence: turn_execution.TurnPersistence = run_persistence._RunPersistence(
            write_state=False
        )
        progress: turn_execution.TurnProgress = runner._ProgressReporter(enabled=False)
        self.assertIsNotNone(persistence.lock)
        self.assertIsNotNone(progress)

    def test_turn_execution_does_not_import_the_runner(self) -> None:
        assert_no_runner_import(self, turn_execution)

    def test_transition_records_are_frozen(self) -> None:
        for record in (
            turn_execution.TurnIdentity,
            turn_execution.TurnLaunch,
            turn_execution.TurnExecution,
            turn_execution.TurnFinalization,
            turn_execution.TurnCommit,
        ):
            with self.subTest(record=record.__name__):
                self.assertTrue(record.__dataclass_params__.frozen)


class TurnExecutorEndingTests(unittest.TestCase):
    """Every way a turn can end, read back off the records it produced."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.harness = TurnExecutorHarness(Path(self._tmp.name))

    def finalize(
        self, execution: turn_execution.TurnExecution
    ) -> turn_execution.TurnFinalization:
        with silent_delivery():
            return self.harness.executor.finalize(execution, before=self.harness.before)

    def test_launch_starts_the_turn_before_the_process(self) -> None:
        execution = self.harness.run(harness_result())

        self.assertEqual("turn_started", self.harness.names()[0])
        self.assertIn("turn_started", self.harness.progress.calls)
        # The live record is finished, so nothing can still be writing to it.
        self.assertIsNotNone(execution.live_turn["finished_at"])
        self.assertIsNone(self.harness.persistence.states[-1]["active_turn"])
        self.assertIsInstance(execution.finished_at, datetime)

    def test_launch_materializes_the_logs_the_started_event_names(self) -> None:
        self.harness.run(harness_result())

        started = self.harness.persistence.payloads("turn_started")[0]
        for key in ("stdout_path", "stderr_path"):
            with self.subTest(key=key):
                self.assertTrue(Path(started[key]).exists(), key)

    def test_successful_turn_records_a_clean_exit(self) -> None:
        finalization = self.finalize(self.harness.run(harness_result()))

        payload = finalization.turn_payload
        self.assertEqual(0, payload["returncode"])
        self.assertFalse(payload["timed_out"])
        self.assertFalse(payload["interrupted"])
        self.assertIsNotNone(finalization.after)
        self.assertIsNone(finalization.plan_error)
        self.assertEqual("working\n", self.harness.paths["stdout"].read_text("utf-8"))
        self.assertEqual(
            "done\n", self.harness.paths["last_message"].read_text("utf-8")
        )

    def test_failed_turn_carries_the_classified_failure(self) -> None:
        finalization = self.finalize(
            self.harness.run(harness_result(returncode=2, stderr="boom\n"))
        )

        payload = finalization.turn_payload
        self.assertEqual(2, payload["returncode"])
        self.assertFalse(payload["timed_out"])
        self.assertNotEqual("complete", payload["status_result"])
        self.assertEqual("boom\n", self.harness.paths["stderr"].read_text("utf-8"))

    def test_timed_out_turn_is_recorded_as_a_timeout(self) -> None:
        finalization = self.finalize(
            self.harness.run(harness_result(returncode=124, timed_out=True))
        )

        self.assertTrue(finalization.turn_payload["timed_out"])
        self.assertEqual("timeout", finalization.turn_payload["status_result"])
        execution_finished = self.harness.persistence.payloads(
            events.TURN_EXECUTION_FINISHED_EVENT
        )
        self.assertEqual([True], [event["timed_out"] for event in execution_finished])

    def test_interrupted_turn_is_a_result_rather_than_a_raise(self) -> None:
        # The run has to record what it spent before it stops, so a
        # KeyboardInterrupt out of the harness becomes a turn record.
        execution = self.harness.run(KeyboardInterrupt())

        self.assertTrue(execution.result.interrupted)
        self.assertEqual(turn_execution.INTERRUPTED_EXIT, execution.result.returncode)
        self.assertEqual(["codex", "exec", "do the work"], execution.result.argv)

        finalization = self.finalize(execution)
        self.assertEqual("interrupted", finalization.turn_payload["status_result"])
        self.assertIsNone(self.harness.persistence.states[-1]["active_turn"])

    def test_unknown_failure_tears_the_live_turn_down_and_re_raises(self) -> None:
        active_turn = self.harness.active_turn()
        live_turn = self.harness.live_turn(active_turn)
        abandoned: list[str] = []

        with (
            silent_delivery(),
            mock.patch.object(harnesses, "run_harness", side_effect=RuntimeError("x")),
            self.assertRaises(RuntimeError),
        ):
            self.harness.executor.launch(
                self.harness.launch_spec(),
                live_turn=live_turn,
                active_turn=active_turn,
                payload=self.harness.payload,
                on_failure=lambda: abandoned.append("stopped"),
            )

        self.assertEqual(["stopped"], abandoned)
        self.assertIsNone(self.harness.persistence.states[-1]["active_turn"])

    def test_after_stop_runs_before_the_live_turn_is_cleared(self) -> None:
        active_turn = self.harness.active_turn()
        live_turn = self.harness.live_turn(active_turn)
        observed: list[Any] = []

        with (
            silent_delivery(),
            mock.patch.object(harnesses, "run_harness", return_value=harness_result()),
        ):
            self.harness.executor.launch(
                self.harness.launch_spec(),
                live_turn=live_turn,
                active_turn=active_turn,
                payload=self.harness.payload,
                after_stop=lambda: observed.append(self.harness.payload["active_turn"]),
            )

        self.assertEqual(1, len(observed))
        self.assertIsNotNone(observed[0])
        self.assertIsNone(self.harness.payload["active_turn"])

    def test_execution_finished_precedes_any_turn_finished(self) -> None:
        finalization = self.finalize(self.harness.run(harness_result()))

        names = self.harness.names()
        self.assertIn(events.TURN_EXECUTION_FINISHED_EVENT, names)
        self.assertNotIn(events.TURN_FINISHED_EVENT, names)
        self.assertIsNotNone(finalization.after)

    def test_unreadable_plan_is_carried_rather_than_raised(self) -> None:
        execution = self.harness.run(harness_result())
        # A harness that removed the manifest leaves a plan the runner cannot
        # re-read; the boundary reports that, it does not decide what it means.
        (self.harness.plan_dir / "manifest.md").unlink()

        finalization = self.finalize(execution)

        self.assertIsNone(finalization.after)
        self.assertIsNotNone(finalization.plan_error)
        self.assertEqual("malformed-plan", finalization.turn_payload["status_result"])

    def test_commit_publishes_an_immutable_record_once(self) -> None:
        finalization = self.finalize(self.harness.run(harness_result()))
        assert finalization.after is not None
        self.harness.payload["turns"] = [finalization.turn_payload]

        with silent_delivery():
            commit = self.harness.executor.commit(
                payload=self.harness.payload,
                turn_payload=finalization.turn_payload,
                live_turn=dict(finalization.turn_payload),
                accepted=finalization.after,
            )

        self.assertIsInstance(commit.record, MappingProxyType)
        self.assertTrue(commit.record["committed"])
        self.assertEqual(
            plans.snapshot_payload(commit.published), commit.record["after"]
        )
        self.assertEqual([events.TURN_FINISHED_EVENT], self.harness.names()[-1:])
        self.assertEqual(1, self.harness.names().count(events.TURN_FINISHED_EVENT))
        self.assertTrue(self.harness.paths["result"].exists())
        # The result file and the event agree, because the file is written
        # inside the same lock, before the event.
        persisted = json.loads(self.harness.paths["result"].read_text("utf-8"))
        self.assertEqual(
            persisted, self.harness.persistence.payloads(events.TURN_FINISHED_EVENT)[0]
        )


class ResumedTurnTests(unittest.TestCase):
    """A second turn of the same run against the same session."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_resumed_turn_keeps_its_own_artifacts_and_totals(self) -> None:
        first = TurnExecutorHarness(self.root, turn_index=1)
        first_execution = first.run(harness_result())
        with silent_delivery():
            first_final = first.executor.finalize(first_execution, before=first.before)

        second = TurnExecutorHarness(self.root, turn_index=2)
        second.plan_dir = first.plan_dir
        second.before = first.before
        second.payload["run_started_at"] = first.payload["run_started_at"]
        second_execution = second.run(harness_result(stdout="resumed\n"))
        with silent_delivery():
            second_final = second.executor.finalize(
                second_execution, before=second.before
            )

        self.assertNotEqual(first.paths["artifact_dir"], second.paths["artifact_dir"])
        self.assertEqual("working\n", first.paths["stdout"].read_text("utf-8"))
        self.assertEqual("resumed\n", second.paths["stdout"].read_text("utf-8"))
        self.assertEqual(1, first_final.turn_payload["turn_index"])
        self.assertEqual(2, second_final.turn_payload["turn_index"])
        # Each turn's own duration is bounded by the run total it reports.
        for execution in (first_execution, second_execution):
            with self.subTest(turn=execution.live_turn["turn_index"]):
                self.assertLessEqual(
                    execution.live_turn["duration_seconds"],
                    execution.live_turn["total_duration_seconds"],
                )

    def test_each_turn_appends_its_own_started_event(self) -> None:
        first = TurnExecutorHarness(self.root, turn_index=1)
        first.run(harness_result())
        second = TurnExecutorHarness(self.root, turn_index=2)
        second.plan_dir = first.plan_dir
        second.run(harness_result())

        self.assertEqual(1, first.names().count("turn_started"))
        self.assertEqual(1, second.names().count("turn_started"))
        self.assertEqual(
            1, second.persistence.payloads("turn_started")[0]["turn_index"] - 1
        )


if __name__ == "__main__":
    unittest.main()
