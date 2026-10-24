"""Asl agent mux projection state regressions."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import agent_mux_contract as contract
from agent_session_loop import (
    agent_mux_projection,
    durability,
    process_tree,
    run_persistence,
)

from .support_asl_agent_mux_projection import (
    PROJECTION_SCHEMA,
    UPDATED_AT,
    _base_payload,
    _turn,
)

ASL_TEST_PORTABLE = True


class ProcessIdentityTests(unittest.TestCase):
    """Birth identity is probed once per pid and never half-reported."""

    def setUp(self) -> None:
        agent_mux_projection.reset_process_identity_cache()
        self.addCleanup(agent_mux_projection.reset_process_identity_cache)

    def _stub_birth(self, value: str | None) -> list[int]:
        calls: list[int] = []

        def fake_birth(pid: int) -> str | None:
            calls.append(pid)
            return value

        original = process_tree.process_birth
        process_tree.process_birth = fake_birth
        self.addCleanup(setattr, process_tree, "process_birth", original)
        return calls

    def test_birth_probe_runs_once_per_pid(self) -> None:
        calls = self._stub_birth("Thu Jun 19 14:00:00 2026")
        for _ in range(5):
            identity = agent_mux_projection.process_identity(4321)
        self.assertEqual([4321], calls)
        self.assertEqual(4321, identity.pid)
        self.assertEqual("Thu Jun 19 14:00:00 2026", identity.start_token)

    def test_unreadable_birth_yields_no_identity(self) -> None:
        self._stub_birth(None)
        self.assertIsNone(agent_mux_projection.process_identity(4321))

    def test_a_negative_birth_result_is_also_memoized(self) -> None:
        calls = self._stub_birth(None)
        agent_mux_projection.process_identity(4321)
        agent_mux_projection.process_identity(4321)
        self.assertEqual([4321], calls)

    def test_implausible_pids_are_refused_without_probing(self) -> None:
        calls = self._stub_birth("Thu Jun 19 14:00:00 2026")
        self.assertIsNone(agent_mux_projection.process_identity(0))
        self.assertIsNone(
            agent_mux_projection.process_identity(agent_mux_projection.MAX_PID + 1)
        )
        self.assertEqual([], calls)


class GenerationTests(unittest.TestCase):
    """The two counters advance independently and never go backwards."""

    def test_generation_advances_on_every_attach(self) -> None:
        payload = _base_payload()
        seen = [
            agent_mux_projection.attach_projection(payload, updated_at=UPDATED_AT)[
                "projection_generation"
            ]
            for _ in range(4)
        ]
        self.assertEqual([1, 2, 3, 4], seen)

    def test_generation_starts_at_one_for_an_unseeded_payload(self) -> None:
        payload = _base_payload()
        self.assertEqual(1, agent_mux_projection.next_generation(payload))

    def test_a_corrupt_generation_restarts_rather_than_crashes(self) -> None:
        payload = _base_payload()
        payload[agent_mux_projection.GENERATION_KEY] = "not a number"
        self.assertEqual(1, agent_mux_projection.next_generation(payload))

    def test_the_two_counters_are_persisted_separately(self) -> None:
        # A run whose notifications all failed still advances its projection,
        # and several notifications inside one transition still advance the
        # projection exactly once.
        payload = _base_payload()
        payload[agent_mux_projection.LIFECYCLE_SEQUENCE_KEY] = 41
        attached = agent_mux_projection.attach_projection(
            payload, updated_at=UPDATED_AT
        )
        self.assertEqual(41, attached["lifecycle_source_sequence"])
        self.assertEqual(1, attached["projection_generation"])
        self.assertEqual(41, payload[agent_mux_projection.LIFECYCLE_SEQUENCE_KEY])
        self.assertEqual(1, payload[agent_mux_projection.GENERATION_KEY])


class DurableStateWriteTests(unittest.TestCase):
    """Every state transition writes a schema-v3 record carrying a projection."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_path = Path(self._tmp.name) / "state.json"

    def _payload(self, **overrides: Any) -> dict[str, Any]:
        return _base_payload(state_path=str(self.state_path), **overrides)

    def _read(self) -> dict[str, Any]:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def test_write_state_payload_emits_a_valid_v3_record(self) -> None:
        run_persistence._write_state_payload(self._payload(active_turn=_turn()))
        record = self._read()
        self.assertEqual(agent_mux_projection.STATE_SCHEMA_VERSION, record["version"])
        projection = record[agent_mux_projection.PROJECTION_KEY]
        violations = contract.validate_document(
            PROJECTION_SCHEMA, projection, root="projection"
        )
        self.assertEqual([violation.to_dict() for violation in violations], [])
        self.assertEqual(
            agent_mux_projection.CONTRACT_SET_ID, projection["contract_set_id"]
        )

    def test_the_projection_describes_the_payload_it_is_nested_in(self) -> None:
        payload = self._payload(
            waiting_for_reply={"question_id": "q-7", "session_id": "5"}
        )
        run_persistence._write_state_payload(payload)
        record = self._read()
        projection = record[agent_mux_projection.PROJECTION_KEY]
        self.assertEqual(agent_mux_projection.PHASE_WAITING, projection["phase"])
        self.assertEqual("q-7", projection["question_id"])
        self.assertEqual("5", projection["session_id"])
        self.assertEqual(
            record[agent_mux_projection.GENERATION_KEY],
            projection["projection_generation"],
        )

    def test_a_payload_with_no_state_path_writes_nothing(self) -> None:
        payload = _base_payload()
        run_persistence._write_state_payload(payload)
        self.assertFalse(self.state_path.exists())
        self.assertNotIn(agent_mux_projection.GENERATION_KEY, payload)

    def test_concurrent_writers_keep_the_generation_monotonic(self) -> None:
        # A heartbeat thread and the main loop both write through the same
        # ``_RunPersistence``. Allocation happens inside the durable write's
        # critical section, so the order generations reach disk must be the
        # order they were allocated in — no gaps, no repeats, no inversions.
        payload = self._payload(active_turn=_turn())
        persistence = run_persistence._RunPersistence(append_events=False)
        written: list[int] = []
        original = durability.write_json_file

        def recording_write(path: Path, record: dict[str, Any], **kwargs: Any) -> None:
            written.append(
                record[agent_mux_projection.PROJECTION_KEY]["projection_generation"]
            )
            original(path, record, **kwargs)

        durability.write_json_file = recording_write
        self.addCleanup(setattr, durability, "write_json_file", original)

        writers = 8
        per_writer = 25
        barrier = threading.Barrier(writers)

        def write_many() -> None:
            barrier.wait()
            for _ in range(per_writer):
                persistence.write_state(payload)

        threads = [threading.Thread(target=write_many) for _ in range(writers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        total = writers * per_writer
        self.assertEqual(list(range(1, total + 1)), written)
        self.assertEqual(total, self._read()[agent_mux_projection.GENERATION_KEY])
        self.assertEqual(
            total,
            self._read()[agent_mux_projection.PROJECTION_KEY]["projection_generation"],
        )

    def test_the_lifecycle_sequence_advances_independently(self) -> None:
        payload = self._payload(active_turn=_turn())
        persistence = run_persistence._RunPersistence(append_events=False)
        first = run_persistence._allocate_agent_mux_sequence(payload, persistence)
        second = run_persistence._allocate_agent_mux_sequence(payload, persistence)
        persistence.write_state(payload)
        record = self._read()
        self.assertEqual([1, 2], [first, second])
        self.assertEqual(2, record[agent_mux_projection.LIFECYCLE_SEQUENCE_KEY])
        # Three writes, two of which allocated a sequence: the counters agree
        # about nothing except that both only ever went up.
        self.assertEqual(3, record[agent_mux_projection.GENERATION_KEY])
        self.assertEqual(
            2,
            record[agent_mux_projection.PROJECTION_KEY]["lifecycle_source_sequence"],
        )


if __name__ == "__main__":
    unittest.main()
