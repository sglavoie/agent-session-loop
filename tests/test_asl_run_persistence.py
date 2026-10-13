"""The durable write core: one lock, one state record, one event log.

The behavioural coverage of what gets written lives where it always has —
``tests/test_asl_compaction.py`` reads back compacted state, and
``tests/test_asl_agent_mux_projection.py`` drives the projection through whole
runs. What is asserted here is the contract those modules cannot state, because
they exercise the run rather than the seam:

* the module does not import the runner, and needs nothing from it;
* the lock contract — ``_RunPersistence.lock`` is the outermost reentrant mutex
  every durable write takes, and it is never held across a mailbox read, a
  lease read, a sleep, or a subprocess; and
* the two bounds a compact record has to keep: it never embeds raw verification
  output, and it never claims a schema version for a block it does not carry;
  and
* the converse of those bounds — a block the compaction was never told about
  survives it verbatim, which is what lets a free-form record like
  ``paused_work_commit`` reach its readers without a schema of its own.

The lock group is the reason this file exists. A lock bug here does not fail a
gate: it tears a ``state.json`` between two transitions, or it lets a slow
``amx`` spawn stall the heartbeat thread that only wanted to stamp liveness,
and both look exactly like a healthy run from the outside.
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    agent_mux_delivery,
    agent_mux_projection,
    compact_state,
    events,
    run_persistence,
    state_payloads,
    workbench_recovery_state,
)
from tests.support import (
    agent_mux_notify_enabled,
    assert_no_runner_import,
    read_jsonl,
)


def _identified_payload(**overrides: Any) -> dict[str, Any]:
    """A payload carrying the identity the projection contract requires."""
    payload: dict[str, Any] = {
        "run_id": "run-1",
        "plan_dir": "/tmp/plan",
        "run_started_at": "2026-01-01T00:00:00+00:00",
    }
    payload.update(overrides)
    return payload


class RunPersistenceBoundaryTests(unittest.TestCase):
    """``run_persistence`` is runner-free and reaches for nothing slow."""

    def setUp(self) -> None:
        self.source = Path(run_persistence.__file__ or "").read_text(encoding="utf-8")

    def test_run_persistence_does_not_import_the_runner(self) -> None:
        assert_no_runner_import(self, run_persistence)

    def test_nothing_slow_is_reachable_from_inside_the_lock(self) -> None:
        # The lock contract holds structurally as well as behaviourally: the
        # module cannot hold its lock across a mailbox read, a lease read, a
        # sleep, or a subprocess, because it never performs one anywhere. The
        # words survive only in the prose that states the contract.
        for reach in ("subprocess.", "time.sleep(", "mailbox.", "leases.", "locks."):
            self.assertNotIn(reach, self.source, msg=reach)

    def test_the_lock_contract_is_restated_in_the_module_docstring(self) -> None:
        docstring = run_persistence.__doc__ or ""
        self.assertIn("outermost lock every durable write below takes", docstring)
        self.assertIn(
            "never held\n  across a mailbox read, a lease read, a sleep, or a "
            "subprocess",
            docstring,
        )

    def test_the_handle_satisfies_the_transport_persistence_protocol(self) -> None:
        # `agent_mux_delivery` types this handle structurally so neither side
        # imports the other; if that ever stopped holding, the notify seam
        # would only fail at runtime.
        handle: agent_mux_delivery.StatePersistence = run_persistence._RunPersistence()
        self.assertIsNotNone(handle.lock)


class RunPersistenceLockTests(unittest.TestCase):
    """The lock is reentrant, serializing, and released before any transport."""

    def test_the_lock_is_reentrant_because_a_write_may_reach_another(self) -> None:
        persistence = run_persistence._RunPersistence()
        self.assertIsInstance(persistence.lock, type(threading.RLock()))
        with persistence.lock:
            # Sequence allocation bumps the counter and writes state within a
            # single hold; a plain mutex would self-deadlock right here.
            self.assertTrue(persistence.lock.acquire(blocking=False))
            persistence.lock.release()

    def test_reentrancy_does_not_leak_the_hold_to_another_thread(self) -> None:
        persistence = run_persistence._RunPersistence()
        with persistence.lock:
            persistence.lock.acquire()
            try:
                self.assertTrue(_lock_is_held_elsewhere(persistence.lock))
            finally:
                persistence.lock.release()
            self.assertTrue(_lock_is_held_elsewhere(persistence.lock))
        self.assertFalse(_lock_is_held_elsewhere(persistence.lock))

    def test_concurrent_state_writes_are_serialized(self) -> None:
        persistence = run_persistence._RunPersistence()
        overlapping: list[bool] = []
        inside = threading.Event()
        depth = 0

        def slow_write(payload: dict[str, Any]) -> None:
            nonlocal depth
            depth += 1
            overlapping.append(depth > 1)
            inside.set()
            # Long enough that a second writer would overlap if the lock were
            # not taken around the whole write.
            for _ in range(2000):
                pass
            depth -= 1

        with mock.patch.object(
            run_persistence, "_write_state_payload", side_effect=slow_write
        ):
            threads = [
                threading.Thread(target=persistence.write_state, args=({},))
                for _ in range(4)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertTrue(inside.is_set())
        self.assertEqual([False] * 4, overlapping)

    def test_sequence_allocation_holds_the_lock_across_the_state_write(self) -> None:
        # The allocation and the write it implies are one critical section, so
        # a second producer can never persist a lower sequence behind a higher
        # one already on disk.
        persistence = run_persistence._RunPersistence()
        payload = _identified_payload()
        held: list[bool] = []

        def record_hold(_payload: dict[str, Any]) -> None:
            held.append(_lock_is_held_elsewhere(persistence.lock))

        with mock.patch.object(
            run_persistence, "_write_state_payload", side_effect=record_hold
        ):
            first = run_persistence._allocate_agent_mux_sequence(payload, persistence)
            second = run_persistence._allocate_agent_mux_sequence(payload, persistence)

        self.assertEqual([1, 2], [first, second])
        self.assertEqual(2, payload["agent_mux_sequence"])
        self.assertEqual([True, True], held)

    def test_the_notification_transport_runs_outside_the_lock(self) -> None:
        # `amx` is spawned by the transport. If the lock were still held there,
        # a slow or hung daemon would stall every thread that only wanted to
        # record what already happened.
        persistence = run_persistence._RunPersistence(write_state=False)
        payload = _identified_payload()
        held: list[bool] = []

        def fake_notify(*_args: Any, **kwargs: Any) -> None:
            kwargs["allocate_sequence"]()
            held.append(_lock_is_held_elsewhere(persistence.lock))

        with mock.patch.object(
            agent_mux_delivery, "notify", side_effect=fake_notify
        ) as notify:
            run_persistence._notify_agent_mux_loop_started(payload, persistence)

        self.assertEqual(1, notify.call_count)
        self.assertEqual([False], held)
        self.assertEqual(1, payload["agent_mux_sequence"])


def _lock_is_held_elsewhere(lock: Any) -> bool:
    """Whether ``lock`` is held, judged from a thread that does not own it.

    An ``RLock`` re-acquires for its own owner, so the question can only be
    answered from another thread.
    """
    result: list[bool] = []

    def probe() -> None:
        if lock.acquire(blocking=False):
            lock.release()
            result.append(False)
        else:
            result.append(True)

    thread = threading.Thread(target=probe)
    thread.start()
    thread.join()
    return result[0]


class CompactStateTests(unittest.TestCase):
    """A compact record bounds what it embeds and claims only what it carries."""

    def test_raw_verification_output_never_reaches_a_compact_check(self) -> None:
        check = {
            "name": "suite",
            "stdout": "x" * 4096,
            "stderr": "boom",
            "stdout_artifact_path": "/runs/1/suite.stdout",
            "stdout_byte_count": 4096,
            "stdout_sha256": "abc",
        }
        stripped = run_persistence._strip_check_output(check)
        self.assertNotIn("stdout", stripped)
        self.assertNotIn("stderr", stripped)
        # The metadata that makes the full output recoverable survives.
        self.assertEqual("/runs/1/suite.stdout", stripped["stdout_artifact_path"])
        self.assertEqual(4096, stripped["stdout_byte_count"])
        self.assertEqual("abc", stripped["stdout_sha256"])

    def test_bounded_details_strip_checks_and_failures_alike(self) -> None:
        details = {
            "checks": [{"name": "a", "stdout": "raw"}],
            "failures": [{"name": "b", "stderr": "raw"}],
            "reason": "verification_failed",
        }
        bounded = run_persistence._bounded_verification_details(details)
        self.assertEqual([{"name": "a"}], bounded["checks"])
        self.assertEqual([{"name": "b"}], bounded["failures"])
        self.assertEqual("verification_failed", bounded["reason"])
        # The caller's dict is not mutated in place.
        self.assertEqual("raw", details["checks"][0]["stdout"])

    def test_a_non_mapping_passes_through_untouched(self) -> None:
        self.assertEqual("nope", run_persistence._bounded_verification_details("nope"))
        self.assertEqual("nope", run_persistence._strip_check_output("nope"))
        self.assertEqual("nope", run_persistence._compact_snapshot("nope"))
        self.assertEqual("nope", run_persistence._compact_turn_summary("nope"))

    def test_a_turn_summary_drops_snapshots_but_keeps_their_counts(self) -> None:
        turn = {
            "session_id": "3",
            "before": {"counts": {"pending": 2}, "sessions": [{"id": "3"}]},
            "after": {"counts": {"pending": 1}, "sessions": [{"id": "3"}]},
            "verification": {"checks": [{"name": "lint", "stdout": "raw"}]},
        }
        summary = run_persistence._compact_turn_summary(turn)
        self.assertEqual({"counts": {"pending": 2}}, summary["before"])
        self.assertEqual({"counts": {"pending": 1}}, summary["after"])
        self.assertEqual([{"name": "lint"}], summary["verification"]["checks"])
        self.assertEqual("3", summary["session_id"])
        # The detailed payload keeps its sessions for `result.json`.
        self.assertIn("sessions", turn["before"])

    def test_a_projection_free_record_claims_only_v2(self) -> None:
        compact = run_persistence._compact_state_payload({"turns": []})
        self.assertEqual(run_persistence.COMPACT_STATE_SCHEMA_VERSION, 2)
        self.assertEqual(2, compact["version"])

    def test_a_record_carrying_a_projection_claims_v3(self) -> None:
        payload = {agent_mux_projection.PROJECTION_KEY: {"generation": 1}}
        compact = run_persistence._compact_state_payload(payload)
        self.assertEqual(agent_mux_projection.STATE_SCHEMA_VERSION, compact["version"])

    def test_compaction_returns_a_record_that_aliases_no_live_turn(self) -> None:
        turn = {"session_id": "3", "before": {"sessions": [{"id": "3"}]}}
        payload = {"turns": [turn], "pause_details": {"checks": [{"stdout": "raw"}]}}
        compact = run_persistence._compact_state_payload(payload)
        self.assertIsNot(payload, compact)
        self.assertIsNot(turn, compact["turns"][0])
        self.assertEqual([{}], compact["pause_details"]["checks"])
        self.assertEqual("raw", payload["pause_details"]["checks"][0]["stdout"])


class PausedWorkCommitProjectionTests(unittest.TestCase):
    """The settlement's record reaches its readers without a schema of its own.

    Session 8's observability work names ``paused_work_commit`` in three
    places on purpose — a progress line, a metric, an audit key. This class is
    the other half of that decision: everywhere else the record travels as an
    ordinary member of a free-form block, and that has to be measured rather
    than inspected. A compaction step that dropped unknown keys would leave the
    record present in memory, absent from ``state.json``, and invisible until
    an operator went looking for evidence that had never been written.
    """

    RECORD = {
        "guard": "paused-work-commit",
        "pause_reason": "verify-failed",
        "commit_mode": "per-session",
        "result": "committed",
        "verification_repair_attempt": 1,
        "verification_repair_max_attempts": 2,
        "verification_repair_exhausted": False,
        "commit_group": {"group_id": "g1", "scope": "session-paused"},
        "commit": None,
        "error": None,
        "residue": {"repositories": []},
    }

    def test_a_compact_turn_summary_keeps_the_settlement_record(self) -> None:
        turn = {
            "session_id": "3",
            "before": {"counts": {"pending": 1}, "sessions": [{"id": "3"}]},
            "paused_work_commit": self.RECORD,
        }
        summary = run_persistence._compact_turn_summary(turn)
        # Verbatim: the compaction bounds snapshots and check output by name,
        # and passes everything it was not told about through untouched.
        self.assertEqual(self.RECORD, summary["paused_work_commit"])
        self.assertEqual({"counts": {"pending": 1}}, summary["before"])

    def test_bounded_pause_details_keep_the_settlement_record(self) -> None:
        details = {
            "session": "3",
            "checks": [{"name": "suite", "stdout": "raw"}],
            "paused_work_commit": self.RECORD,
        }
        bounded = run_persistence._bounded_verification_details(details)
        self.assertEqual([{"name": "suite"}], bounded["checks"])
        self.assertEqual(self.RECORD, bounded["paused_work_commit"])

    def test_the_compact_state_record_carries_every_place_it_was_written(
        self,
    ) -> None:
        # The three mirrors `run_loop` writes, through the one seam that turns
        # a live payload into the bytes of `state.json`.
        payload = {
            "paused": True,
            "pause_reason": "verify-failed",
            "paused_work_commit": self.RECORD,
            "pause_details": {"paused_work_commit": self.RECORD},
            "turns": [{"session_id": "3", "paused_work_commit": self.RECORD}],
        }
        compact = run_persistence._compact_state_payload(payload)
        self.assertEqual(self.RECORD, compact["paused_work_commit"])
        self.assertEqual(self.RECORD, compact["pause_details"]["paused_work_commit"])
        self.assertEqual(self.RECORD, compact["turns"][0]["paused_work_commit"])

    def test_the_settlement_event_survives_event_log_compaction(self) -> None:
        record = {
            "created_at": "2026-01-01T00:00:00+00:00",
            "name": events.PAUSED_WORK_COMMITTED_EVENT,
            "payload": {"run_id": "run-1", "session_id": "3", **self.RECORD},
            "version": 2,
        }
        compact = compact_state._compact_event_record(record)
        self.assertEqual(record["payload"], compact["payload"])

    def test_the_recovery_centers_pause_details_reader_names_no_key(self) -> None:
        # `/api/recovery` and the paused-Q&A projection both reach the pause
        # details through this one reader, which copies the block wholesale.
        details = workbench_recovery_state._pause_details(
            {"pause_details": {"paused_work_commit": self.RECORD}}
        )
        self.assertEqual(self.RECORD, details["paused_work_commit"])

    def test_the_status_projection_carries_no_pause_details_to_extend(self) -> None:
        # Measured, not assumed: `asl status`'s runner-state block is a fixed
        # key list that has never carried pause details, so the record is not
        # missing from a schema — there is no schema there for it to join.
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "run_id": "run-1",
                        "paused": True,
                        "reason": "verify-failed",
                        "paused_work_commit": self.RECORD,
                        "pause_details": {"paused_work_commit": self.RECORD},
                    }
                ),
                encoding="utf-8",
            )
            projection = state_payloads._runner_state_payload(
                state_path, lock_info=None
            )
        self.assertNotIn("pause_details", projection)
        self.assertNotIn("details", projection)
        self.assertNotIn("paused_work_commit", projection)
        # What it does carry about the pause is unchanged by the settlement.
        self.assertTrue(projection["paused"])
        self.assertEqual("verify-failed", projection["reason"])


class RunFinishedEventPayloadTests(unittest.TestCase):
    """The terminal event references the run rather than duplicating it."""

    def test_the_payload_counts_turns_instead_of_embedding_them(self) -> None:
        payload = run_persistence._run_finished_event_payload(
            {
                "run_id": "run-1",
                "ok": True,
                "turns": [{"stdout": "raw"}, {"stdout": "raw"}],
                "final_snapshot": {"counts": {"complete": 2}, "sessions": ["big"]},
                "state_path": "/runs/1/state.json",
            }
        )
        self.assertEqual(2, payload["turn_count"])
        self.assertNotIn("turns", payload)
        self.assertNotIn("final_snapshot", payload)
        self.assertEqual({"complete": 2}, payload["counts"])
        self.assertEqual("/runs/1/state.json", payload["state_path"])

    def test_a_run_without_turns_or_counts_still_projects(self) -> None:
        payload = run_persistence._run_finished_event_payload({"run_id": "run-1"})
        self.assertEqual(0, payload["turn_count"])
        self.assertIsNone(payload["counts"])

    def test_workspace_identity_appears_only_for_a_workspace_run(self) -> None:
        legacy = run_persistence._run_finished_event_payload({"cwd": "/repo"})
        self.assertNotIn("cwd", legacy)
        self.assertNotIn("workspace_root", legacy)

        workspace = run_persistence._run_finished_event_payload(
            {"cwd": "/repo", "workspace_root": "/ws", "registry_digest": "d"}
        )
        self.assertEqual("/repo", workspace["cwd"])
        self.assertEqual("/ws", workspace["workspace_root"])
        self.assertEqual("d", workspace["registry_digest"])


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


if __name__ == "__main__":  # pragma: no cover - manual invocation
    unittest.main()
