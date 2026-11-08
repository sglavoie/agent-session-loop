"""Writes tests extracted from test_asl_run_persistence."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    agent_mux_delivery,
    agent_mux_projection,
    events,
    run_persistence,
)
from tests.support import (
    agent_mux_notify_enabled,
    read_jsonl,
)

from .asl_run_persistence_support import _identified_payload


class DurableWriteTests(unittest.TestCase):
    """Both durable records go through the handle, and both can be turned off."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.state_path = self.root / "state.json"
        self.event_path = self.root / "events.jsonl"

    def test_a_state_write_attaches_the_projection_it_persists(self) -> None:
        persistence = run_persistence._RunPersistence()
        payload = _identified_payload(state_path=str(self.state_path), turns=[])
        persistence.write_state(payload)

        record = json.loads(self.state_path.read_text(encoding="utf-8"))
        projection = record[agent_mux_projection.PROJECTION_KEY]
        self.assertEqual(1, projection["projection_generation"])
        self.assertEqual(agent_mux_projection.STATE_SCHEMA_VERSION, record["version"])
        # The projection a reader finds was built from the payload it is
        # nested in, so the live payload carries the same generation.
        self.assertEqual(
            projection,
            payload[agent_mux_projection.PROJECTION_KEY],
        )

    def test_a_payload_without_a_state_path_writes_nothing(self) -> None:
        run_persistence._write_state_payload(_identified_payload())
        run_persistence._write_state_payload(_identified_payload(state_path=""))
        self.assertEqual([], list(self.root.iterdir()))

    def test_an_event_append_is_one_record_on_the_log(self) -> None:
        persistence = run_persistence._RunPersistence()
        persistence.append_event(self.event_path, "run_started", {"run_id": "run-1"})
        (record,) = read_jsonl(self.event_path)
        self.assertEqual("run_started", record["name"])
        self.assertEqual({"run_id": "run-1"}, record["payload"])

    def test_a_disabled_handle_writes_neither_record(self) -> None:
        persistence = run_persistence._RunPersistence(
            write_state=False, append_events=False
        )
        persistence.write_state(_identified_payload(state_path=str(self.state_path)))
        persistence.append_event(self.event_path, "run_started", {})
        self.assertFalse(self.state_path.exists())
        self.assertFalse(self.event_path.exists())

    def test_an_absent_handle_resolves_to_a_real_one(self) -> None:
        resolved = run_persistence._run_persistence(None)
        self.assertIsInstance(resolved, run_persistence._RunPersistence)
        existing = run_persistence._RunPersistence()
        self.assertIs(existing, run_persistence._run_persistence(existing))

    def test_a_sweep_that_reaped_nothing_is_not_worth_an_event(self) -> None:
        persistence = run_persistence._RunPersistence()
        for report in (None, {}, {"killed": [], "kill_failed": []}):
            run_persistence._append_process_sweep_event(
                event_path=self.event_path,
                persistence=persistence,
                report=report,
                phase="turn_end",
                identity={"session_id": "3"},
            )
        self.assertFalse(self.event_path.exists())

    def test_a_sweep_that_signalled_a_pid_records_what_it_signalled(self) -> None:
        persistence = run_persistence._RunPersistence()
        run_persistence._append_process_sweep_event(
            event_path=self.event_path,
            persistence=persistence,
            report={"killed": [123], "survivors_after": [456]},
            phase="turn_end",
            identity={"session_id": "3"},
        )
        (record,) = read_jsonl(self.event_path)
        self.assertEqual(events.PROCESS_SWEEP_EVENT, record["name"])
        payload = record["payload"]
        self.assertEqual([123], payload["killed"])
        self.assertEqual([], payload["kill_failed"])
        self.assertEqual([456], payload["survivors_after"])
        self.assertEqual("turn_end", payload["phase"])
        self.assertEqual("3", payload["session_id"])

    def test_a_degradation_event_carries_no_failure_of_its_own(self) -> None:
        persistence = run_persistence._RunPersistence()
        run_persistence._append_harness_degradation_event(
            event_path=self.event_path,
            persistence=persistence,
            degradation=None,
            identity={"session_id": "3"},
        )
        self.assertFalse(self.event_path.exists())

        run_persistence._append_harness_degradation_event(
            event_path=self.event_path,
            persistence=persistence,
            degradation={"capability": "mcp"},
            identity={"session_id": "3"},
        )
        (record,) = read_jsonl(self.event_path)
        self.assertEqual(events.HARNESS_DEGRADED_EVENT, record["name"])
        self.assertEqual({"session_id": "3", "capability": "mcp"}, record["payload"])


class NotifySeamTests(unittest.TestCase):
    """Announcements are bounded, fail-open, and never a run dependency."""

    def test_a_question_preview_is_a_locator_not_the_question(self) -> None:
        self.assertIsNone(run_persistence._question_preview(None))
        self.assertIsNone(run_persistence._question_preview("   \n  "))
        self.assertEqual(
            "one two three",
            run_persistence._question_preview("one\n  two\tthree\n"),
        )
        preview = run_persistence._question_preview("q" * 500)
        self.assertEqual(
            run_persistence.AGENT_MUX_QUESTION_PREVIEW_CHARS, len(preview or "")
        )

    def test_a_delivery_failure_cannot_end_the_run(self) -> None:
        # Containment itself lives in `agent_mux_delivery.notify`. What this
        # module owes is to reach delivery *through* that seam rather than
        # around it, so an exploding `amx` lookup returns instead of raising.
        persistence = run_persistence._RunPersistence(write_state=False)
        payload = _identified_payload()
        with (
            agent_mux_notify_enabled(),
            mock.patch.object(agent_mux_delivery, "shutil") as shutil_stub,
        ):
            shutil_stub.which.side_effect = RuntimeError("amx lookup exploded")
            run_persistence._notify_agent_mux_loop_finished(payload, persistence)
        # The sequence is allocated before the transport is reached, so the
        # failure is visibly downstream of the durable write.
        self.assertEqual(1, payload["agent_mux_sequence"])

    def test_each_lifecycle_entry_point_names_its_own_event(self) -> None:
        payload = _identified_payload()
        seen: list[tuple[str, str]] = []

        def capture(event_name: str, _payload: dict[str, Any], **kwargs: Any) -> None:
            seen.append((event_name, kwargs["reason"]))

        with mock.patch.object(agent_mux_delivery, "notify_loop", side_effect=capture):
            run_persistence._notify_agent_mux_loop_started(payload)
            run_persistence._notify_agent_mux_loop_paused(payload)
            run_persistence._notify_agent_mux_loop_finished(payload)

        self.assertEqual(
            [
                (agent_mux_delivery.START_EVENT, agent_mux_delivery.START_REASON),
                (agent_mux_delivery.PAUSE_EVENT, agent_mux_delivery.PAUSE_REASON),
                (agent_mux_delivery.FINISH_EVENT, agent_mux_delivery.FINISH_REASON),
            ],
            seen,
        )

    def test_the_wait_notifier_binds_a_wait_to_this_runs_projection(self) -> None:
        persistence = run_persistence._RunPersistence(write_state=False)
        payload = _identified_payload()
        notifier = run_persistence._AgentMuxWaitNotifier(payload, persistence)
        seen: list[dict[str, Any]] = []

        def capture(event_name: str, notified: dict[str, Any], **kwargs: Any) -> None:
            seen.append({"event": event_name, "payload": notified, **kwargs})

        with mock.patch.object(agent_mux_delivery, "notify_loop", side_effect=capture):
            notifier.wait_started(question_id="q1", question_text="are  we  ok?")
            notifier.wait_ended(question_id="q1", wait_reason="reply", closure_id="c1")
            notifier.wait_ended(
                question_id="q1", wait_reason="timeout", closure_id=None
            )

        started, consumed, unconsumed = seen
        self.assertIs(payload, started["payload"])
        self.assertIs(persistence, started["persistence"])
        self.assertEqual("are we ok?", started["extra"]["question_preview"])
        self.assertEqual("consumed", consumed["extra"]["delivery_state"])
        self.assertEqual("c1", consumed["extra"]["closure_id"])
        self.assertEqual("unconsumed", unconsumed["extra"]["delivery_state"])
        self.assertNotIn("closure_id", unconsumed["extra"])
