"""The durable AgentMux projection and the schema-2 lifecycle hook identity.

``agent_mux_projection`` is the one place the two AgentMux surfaces are built
from, so these tests check it from both ends. Against the contract corpus: the
pinned contract-set ID, the phase vocabularies, and the field bounds all have to
still agree with the schemas AgentMux implements against, and every record this
module produces — projection and hook payload alike — has to validate against
them. Against the runner: every state transition must carry a projection, the
generation must stay monotonic when a heartbeat thread and the main loop write
concurrently, and the projection must stay body-free no matter what the run
payload it was built from contains.
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import agent_mux_contract as contract
from agent_session_loop import (
    agent_mux_delivery,
    agent_mux_projection,
    durability,
    process_tree,
    run_persistence,
    verification_transitions,
)

PROJECTION_SCHEMA = contract.load_schema(contract.COMPONENT_PROJECTION)
HOOK_SCHEMA = contract.load_schema(contract.COMPONENT_LIFECYCLE_HOOK)

RUN_STARTED_AT = "2026-06-19T14:00:00+00:00"
UPDATED_AT = "2026-06-19T14:31:02.481900+00:00"

#: Read at its owning module rather than spelled inline: the reason is 62
#: characters and the fixture payloads that carry it nest four levels deep.
DEFERRED_REASON = verification_transitions.REVIEW_BROAD_FAILURES_DEFERRED_REASON


def _base_payload(**overrides: Any) -> dict[str, Any]:
    """A minimally projectable run payload, before any phase-specific state."""
    payload: dict[str, Any] = {
        "run_id": "20260619T140000Z-1",
        "plan_dir": "/fixtures/plans/plan-alpha",
        "workspace_root": "/fixtures/workspaces/alpha",
        "registry_digest": "3398ee413bd2a03b",
        "kitty_window_id": 17,
        "run_started_at": RUN_STARTED_AT,
        "harness": "claude",
        "turns": [],
    }
    payload.update(overrides)
    return payload


def _turn(**overrides: Any) -> dict[str, Any]:
    turn: dict[str, Any] = {
        "turn_index": 4,
        "session_id": "3",
        "harness": "claude",
    }
    turn.update(overrides)
    return turn


def _strings(document: Any) -> list[str]:
    """Every string anywhere in ``document``, keys included."""
    found: list[str] = []
    if isinstance(document, dict):
        for key, value in document.items():
            found.append(key)
            found.extend(_strings(value))
    elif isinstance(document, list):
        for item in document:
            found.extend(_strings(item))
    elif isinstance(document, str):
        found.append(document)
    return found


class ContractIdentityTests(unittest.TestCase):
    """The pinned constants still describe the corpus AgentMux will implement."""

    def test_pinned_contract_set_id_matches_the_corpus(self) -> None:
        # The constant is pinned so the durable write path never re-digests 44
        # files. This is the check that keeps the pin honest: a corpus edit that
        # moves the identity fails here instead of shipping a stale ID.
        self.assertEqual(
            contract.contract_set_id(), agent_mux_projection.CONTRACT_SET_ID
        )

    def test_schema_versions_match_the_schemas(self) -> None:
        self.assertEqual(
            PROJECTION_SCHEMA["version"],
            agent_mux_projection.PROJECTION_SCHEMA_VERSION,
        )
        self.assertEqual(
            HOOK_SCHEMA["version"], agent_mux_projection.HOOK_SCHEMA_VERSION
        )
        self.assertEqual(
            agent_mux_projection.STATE_SCHEMA_VERSION,
            agent_mux_projection.STATE_SCHEMA_VERSION,
        )

    def test_phase_vocabularies_match_the_schemas(self) -> None:
        # Compared as sets: an enum's declared order carries no meaning, and
        # the two schemas happen to list the shared phases differently.
        self.assertEqual(
            set(agent_mux_projection.PROJECTION_PHASES),
            set(PROJECTION_SCHEMA["definitions"]["Phase"]["enum"]),
        )
        self.assertEqual(
            set(agent_mux_projection.HOOK_PHASES),
            set(HOOK_SCHEMA["definitions"]["Phase"]["enum"]),
        )

    def test_live_phases_are_exactly_the_non_resting_phases(self) -> None:
        self.assertEqual(
            agent_mux_projection.LIVE_PHASES,
            frozenset(agent_mux_projection.PROJECTION_PHASES)
            - {agent_mux_projection.PHASE_PAUSED, agent_mux_projection.PHASE_FINISHED},
        )

    def test_field_bounds_match_the_schema(self) -> None:
        properties = PROJECTION_SCHEMA["definitions"]["Projection"]["properties"]
        process = PROJECTION_SCHEMA["definitions"]["Process"]["properties"]
        self.assertEqual(
            properties["run_id"]["maxLength"], agent_mux_projection.MAX_RUN_ID_CHARS
        )
        self.assertEqual(
            properties["session_id"]["maxLength"],
            agent_mux_projection.MAX_SESSION_ID_CHARS,
        )
        self.assertEqual(
            properties["question_id"]["maxLength"],
            agent_mux_projection.MAX_QUESTION_ID_CHARS,
        )
        self.assertEqual(
            properties["harness"]["maxLength"], agent_mux_projection.MAX_HARNESS_CHARS
        )
        self.assertEqual(
            properties["registry_digest"]["maxLength"],
            agent_mux_projection.MAX_REGISTRY_DIGEST_CHARS,
        )
        self.assertEqual(
            process["start_token"]["maxLength"],
            agent_mux_projection.MAX_START_TOKEN_CHARS,
        )
        self.assertEqual(process["pid"]["maximum"], agent_mux_projection.MAX_PID)

    def test_projection_declares_every_field_the_record_emits(self) -> None:
        declared = set(PROJECTION_SCHEMA["definitions"]["Projection"]["properties"])
        emitted = set(
            agent_mux_projection.build_projection(
                _base_payload(), updated_at=UPDATED_AT, generation=1
            ).to_dict()
        )
        self.assertEqual(declared, emitted)
        self.assertEqual(
            sorted(PROJECTION_SCHEMA["definitions"]["Projection"]["required"]),
            sorted(emitted),
        )


class PhaseDerivationTests(unittest.TestCase):
    """The phase reported is the one an operator has to act on."""

    def _phase(self, **overrides: Any) -> str:
        return agent_mux_projection.phase_for_payload(_base_payload(**overrides))

    def test_no_turns_yet_is_starting(self) -> None:
        self.assertEqual(agent_mux_projection.PHASE_STARTING, self._phase())

    def test_between_turns(self) -> None:
        self.assertEqual(
            agent_mux_projection.PHASE_BETWEEN, self._phase(turns=[_turn()])
        )

    def test_active_turn(self) -> None:
        self.assertEqual(
            agent_mux_projection.PHASE_TURN, self._phase(active_turn=_turn())
        )

    def test_verification_outranks_the_turn_that_owns_it(self) -> None:
        # The harness has already returned by the time verification runs, so
        # reporting `turn` would point an operator at the wrong thing.
        self.assertEqual(
            agent_mux_projection.PHASE_VERIFICATION,
            self._phase(active_turn=_turn(), active_verification={"check_count": 2}),
        )

    def test_waiting_outranks_verification(self) -> None:
        self.assertEqual(
            agent_mux_projection.PHASE_WAITING,
            self._phase(
                active_turn=_turn(),
                active_verification={"check_count": 2},
                waiting_for_reply={"question_id": "q-7"},
            ),
        )

    def test_finished_run(self) -> None:
        self.assertEqual(
            agent_mux_projection.PHASE_FINISHED,
            self._phase(run_finished_at="2026-06-19T16:40:00+00:00"),
        )

    def test_paused_outranks_finished(self) -> None:
        # A run that terminalized into a pause is still waiting on someone.
        self.assertEqual(
            agent_mux_projection.PHASE_PAUSED,
            self._phase(paused=True, run_finished_at="2026-06-19T16:12:04+00:00"),
        )

    def test_every_derived_phase_is_in_the_declared_vocabulary(self) -> None:
        payloads = (
            {},
            {"turns": [_turn()]},
            {"active_turn": _turn()},
            {"active_verification": {"check_count": 1}},
            {"waiting_for_reply": {"question_id": "q-7"}},
            {"run_finished_at": "2026-06-19T16:40:00+00:00"},
            {"paused": True},
        )
        for overrides in payloads:
            with self.subTest(payload=sorted(overrides)):
                self.assertIn(
                    self._phase(**overrides), agent_mux_projection.PROJECTION_PHASES
                )


class ProjectionBuildTests(unittest.TestCase):
    """What the built record carries, and what it refuses to carry."""

    def _validate(self, projection: dict[str, Any]) -> None:
        violations = contract.validate_document(
            PROJECTION_SCHEMA, projection, root="projection"
        )
        self.assertEqual([violation.to_dict() for violation in violations], [])

    def test_live_turn_projection_validates(self) -> None:
        projection = agent_mux_projection.build_projection(
            _base_payload(active_turn=_turn()),
            updated_at=UPDATED_AT,
            generation=7,
            process=agent_mux_projection.ProcessIdentity(58971, "1750339200.42"),
        ).to_dict()
        self._validate(projection)
        self.assertEqual(agent_mux_projection.PHASE_TURN, projection["phase"])
        self.assertEqual("3", projection["session_id"])
        self.assertEqual(4, projection["turn_index"])
        self.assertEqual(7, projection["projection_generation"])
        self.assertEqual(
            {"pid": 58971, "start_token": "1750339200.42"}, projection["process"]
        )

    def test_paused_projection_clears_the_process(self) -> None:
        # A paused run owns no loop process, so naming one would invite a
        # consumer to act on a pid that is either gone or somebody else's.
        projection = agent_mux_projection.build_projection(
            _base_payload(paused=True, pause_details={"question_id": "q-7"}),
            updated_at=UPDATED_AT,
            generation=12,
            process=agent_mux_projection.ProcessIdentity(58971, "1750339200.42"),
        ).to_dict()
        self._validate(projection)
        self.assertIsNone(projection["process"])
        self.assertEqual("q-7", projection["question_id"])

    def test_finished_projection_clears_the_process(self) -> None:
        projection = agent_mux_projection.build_projection(
            _base_payload(run_finished_at="2026-06-19T16:40:00+00:00"),
            updated_at=UPDATED_AT,
            generation=14,
            process=agent_mux_projection.ProcessIdentity(58971, "1750339200.42"),
        ).to_dict()
        self._validate(projection)
        self.assertIsNone(projection["process"])
        self.assertEqual("2026-06-19T16:40:00+00:00", projection["finished_at"])

    def test_waiting_projection_names_the_question_not_its_body(self) -> None:
        projection = agent_mux_projection.build_projection(
            _base_payload(
                active_turn=_turn(),
                waiting_for_reply={
                    "question_id": "q-7",
                    "question_text": "which registry alias owns this path?",
                },
            ),
            updated_at=UPDATED_AT,
            generation=9,
        ).to_dict()
        self._validate(projection)
        self.assertEqual("q-7", projection["question_id"])
        self.assertNotIn("which registry alias owns this path?", _strings(projection))

    def test_projection_carries_no_body_even_when_the_payload_is_full_of_them(
        self,
    ) -> None:
        payload = _base_payload(
            active_turn=_turn(
                prompt="the whole session prompt",
                last_message="the harness's final message",
            ),
            initial_prompt="the run's opening prompt",
            waiting_for_reply={"question_id": "q-7", "body": "the question body"},
            active_verification={
                "check_count": 1,
                "stdout": "ruff failed on 400 files",
            },
        )
        projection = agent_mux_projection.build_projection(
            payload, updated_at=UPDATED_AT, generation=3
        ).to_dict()
        self._validate(projection)
        forbidden = set(PROJECTION_SCHEMA["forbidden_property_names"])
        self.assertEqual(forbidden & set(projection), set())
        leaked = {
            "the whole session prompt",
            "the harness's final message",
            "the run's opening prompt",
            "the question body",
            "ruff failed on 400 files",
        }
        self.assertEqual(leaked & set(_strings(projection)), set())

    def test_oversized_identity_is_bounded_rather_than_refused(self) -> None:
        projection = agent_mux_projection.build_projection(
            _base_payload(
                run_id="r" * 400,
                harness="c" * 400,
                waiting_for_reply={"question_id": "q" * 400},
            ),
            updated_at=UPDATED_AT,
            generation=1,
        ).to_dict()
        self._validate(projection)
        self.assertEqual(
            agent_mux_projection.MAX_RUN_ID_CHARS, len(projection["run_id"])
        )
        self.assertEqual(
            agent_mux_projection.MAX_QUESTION_ID_CHARS, len(projection["question_id"])
        )

    def test_a_payload_without_run_identity_does_not_project(self) -> None:
        for missing in ("run_id", "plan_dir", "run_started_at"):
            with self.subTest(missing=missing):
                payload = _base_payload()
                del payload[missing]
                self.assertIsNone(
                    agent_mux_projection.build_projection(
                        payload, updated_at=UPDATED_AT, generation=1
                    )
                )

    def test_attach_leaves_the_generation_alone_when_nothing_projects(self) -> None:
        payload = _base_payload()
        del payload["run_id"]
        payload[agent_mux_projection.GENERATION_KEY] = 4
        self.assertIsNone(
            agent_mux_projection.attach_projection(payload, updated_at=UPDATED_AT)
        )
        self.assertEqual(4, payload[agent_mux_projection.GENERATION_KEY])
        self.assertNotIn(agent_mux_projection.PROJECTION_KEY, payload)

    def test_attach_without_process_detection_names_no_process(self) -> None:
        # The migrator is not the run's owner: substituting its own pid would
        # authenticate the wrong process.
        payload = _base_payload(active_turn=_turn())
        attached = agent_mux_projection.attach_projection(
            payload, updated_at=UPDATED_AT, detect_process=False
        )
        self.assertIsNone(attached["process"])


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


class HookIdentityTests(unittest.TestCase):
    """Schema-2 hook payloads come from the same canonical types."""

    def _hook(
        self,
        event_name: str,
        *,
        payload: dict[str, Any] | None = None,
        active_turn: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        from datetime import UTC, datetime

        return agent_mux_delivery.build_loop_payload(
            _base_payload(
                cwd="/fixtures/workspaces/alpha",
                state_path="/fixtures/state/8f14e45fceea167a/state.json",
            )
            | (payload or {}),
            reason=_REASON_BY_EVENT[event_name],
            active_turn=active_turn,
            event_name=event_name,
            sequence=41,
            emitted_at=datetime(2026, 6, 19, 14, 31, 2, tzinfo=UTC),
            extra=extra,
        )

    def _validate(self, hook_payload: dict[str, Any]) -> None:
        violations = contract.validate_document(
            HOOK_SCHEMA, hook_payload, root="hook_payload"
        )
        self.assertEqual([violation.to_dict() for violation in violations], [])

    def test_identity_block_declares_version_event_and_contract_set(self) -> None:
        identity = agent_mux_projection.hook_identity(
            agent_mux_delivery.TURN_START_EVENT
        )
        self.assertEqual(
            {
                "schema_version": agent_mux_projection.HOOK_SCHEMA_VERSION,
                "contract_set_id": agent_mux_projection.CONTRACT_SET_ID,
                "event": agent_mux_delivery.TURN_START_EVENT,
            },
            identity,
        )

    def test_every_declared_event_has_exactly_one_announced_phase(self) -> None:
        self.assertEqual(
            sorted(HOOK_SCHEMA["definitions"]["EventName"]["enum"]),
            sorted(agent_mux_delivery.PHASE_BY_EVENT),
        )
        for event, phase in agent_mux_delivery.PHASE_BY_EVENT.items():
            with self.subTest(event=event):
                self.assertIn(phase, agent_mux_projection.HOOK_PHASES)

    def test_every_declared_reason_is_emitted_by_some_event(self) -> None:
        self.assertEqual(
            sorted(HOOK_SCHEMA["definitions"]["Reason"]["enum"]),
            sorted(_REASON_BY_EVENT.values()),
        )

    def test_a_payload_without_an_event_carries_no_schema_identity(self) -> None:
        # The schema-1 shape is still what a legacy binary receives; it must not
        # be labelled with a contract set it does not satisfy.
        hook_payload = agent_mux_delivery.build_loop_payload(
            _base_payload(), reason=agent_mux_delivery.START_REASON
        )
        self.assertNotIn("schema_version", hook_payload)
        self.assertNotIn("contract_set_id", hook_payload)
        self.assertNotIn("event", hook_payload)

    def test_every_real_emitter_shape_validates(self) -> None:
        for case, hook_payload in _emitted_hook_payloads(self._hook).items():
            with self.subTest(case=case):
                self._validate(hook_payload)
                self.assertEqual(
                    agent_mux_projection.HOOK_SCHEMA_VERSION,
                    hook_payload["schema_version"],
                )

    def test_a_wait_carries_the_preview_the_schema_bounds(self) -> None:
        hook_payload = self._hook(
            agent_mux_delivery.WAIT_START_EVENT,
            extra={
                "question_id": "q-7",
                "question_preview": run_persistence._question_preview("x" * 500),
            },
        )
        self._validate(hook_payload)
        self.assertEqual(
            run_persistence.AGENT_MUX_QUESTION_PREVIEW_CHARS,
            len(hook_payload["question_preview"]),
        )


_REASON_BY_EVENT = {
    agent_mux_delivery.START_EVENT: agent_mux_delivery.START_REASON,
    agent_mux_delivery.TURN_START_EVENT: agent_mux_delivery.TURN_START_REASON,
    agent_mux_delivery.TURN_HEARTBEAT_EVENT: agent_mux_delivery.TURN_HEARTBEAT_REASON,
    agent_mux_delivery.TURN_FINISH_EVENT: agent_mux_delivery.TURN_FINISH_REASON,
    agent_mux_delivery.TURN_HANG_SUSPECTED_EVENT: (
        agent_mux_delivery.TURN_HANG_SUSPECTED_REASON
    ),
    agent_mux_delivery.TURN_HANG_CLEARED_EVENT: (
        agent_mux_delivery.TURN_HANG_CLEARED_REASON
    ),
    agent_mux_delivery.VERIFICATION_START_EVENT: (
        agent_mux_delivery.VERIFICATION_START_REASON
    ),
    agent_mux_delivery.VERIFICATION_HEARTBEAT_EVENT: (
        agent_mux_delivery.VERIFICATION_HEARTBEAT_REASON
    ),
    agent_mux_delivery.VERIFICATION_FINISH_EVENT: (
        agent_mux_delivery.VERIFICATION_FINISH_REASON
    ),
    agent_mux_delivery.WAIT_START_EVENT: agent_mux_delivery.WAIT_START_REASON,
    agent_mux_delivery.WAIT_END_EVENT: agent_mux_delivery.WAIT_END_REASON,
    agent_mux_delivery.MAILBOX_QUESTION_EVENT: (
        agent_mux_delivery.MAILBOX_QUESTION_REASON
    ),
    agent_mux_delivery.MAILBOX_REPLY_EVENT: agent_mux_delivery.MAILBOX_REPLY_REASON,
    agent_mux_delivery.MAILBOX_RESOLUTION_EVENT: (
        agent_mux_delivery.MAILBOX_RESOLUTION_REASON
    ),
    agent_mux_delivery.PAUSE_EVENT: agent_mux_delivery.PAUSE_REASON,
    agent_mux_delivery.FINISH_EVENT: agent_mux_delivery.FINISH_REASON,
}

#: The turn record ASL threads into a hook payload, at its widest.
_ACTIVE_TURN = {
    "turn_index": 4,
    "session": "3",
    "session_id": "3",
    "session_number": 3,
    "total_sessions": 14,
    "harness": "claude",
    "artifact_dir": "/fixtures/state/8f14e45fceea167a/turns/4",
    "stdout_path": "/fixtures/state/8f14e45fceea167a/turns/4/stdout.log",
    "stderr_path": "/fixtures/state/8f14e45fceea167a/turns/4/stderr.log",
    "last_message_path": "/fixtures/state/8f14e45fceea167a/turns/4/last.txt",
    "result_path": "/fixtures/state/8f14e45fceea167a/turns/4/result.json",
    "started_at": "2026-06-19T14:29:00+00:00",
    "last_activity_at": "2026-06-19T14:30:41+00:00",
    "heartbeat_count": 3,
}

#: The verification identity every verification boundary carries.
_VERIFICATION_IDENTITY = {
    "run_id": "20260619T140000Z-1",
    "turn_index": 4,
    "session": "3",
    "session_id": "3",
    "total_sessions": 14,
}


def _emitted_hook_payloads(hook: Any) -> dict[str, dict[str, Any]]:
    """One payload per shape ASL actually pipes to ``amx hook-indicator``.

    Restated here rather than captured from a live run so the schema is checked
    against every emitter, including the CLI-side mailbox boundaries that no
    single run exercises.
    """
    return {
        "started": hook(agent_mux_delivery.START_EVENT),
        "turn-started": hook(
            agent_mux_delivery.TURN_START_EVENT, active_turn=_ACTIVE_TURN
        ),
        "turn-heartbeat": hook(
            agent_mux_delivery.TURN_HEARTBEAT_EVENT, active_turn=_ACTIVE_TURN
        ),
        "turn-finished": hook(
            agent_mux_delivery.TURN_FINISH_EVENT, active_turn=_ACTIVE_TURN
        ),
        "turn-hang-suspected": hook(
            agent_mux_delivery.TURN_HANG_SUSPECTED_EVENT,
            active_turn=_ACTIVE_TURN,
            extra={
                "hang_reason": "no_output",
                "hang_idle_seconds": 903.5,
                "harness_pid": 58971,
            },
        ),
        "turn-hang-cleared": hook(
            agent_mux_delivery.TURN_HANG_CLEARED_EVENT,
            active_turn=_ACTIVE_TURN,
            extra={
                "hang_reason": "no_output",
                "hang_idle_seconds": None,
                "harness_pid": 58971,
            },
        ),
        "verification-started": hook(
            agent_mux_delivery.VERIFICATION_START_EVENT,
            extra=_VERIFICATION_IDENTITY
            | {"check_count": 5, "started_at": "2026-06-19T14:31:02+00:00"},
        ),
        "verification-heartbeat": hook(
            agent_mux_delivery.VERIFICATION_HEARTBEAT_EVENT,
            extra=_VERIFICATION_IDENTITY
            | {
                "check_index": 1,
                "check_count": 5,
                "check_name": "asl-tests",
                "check_kind": "command",
                "heartbeat_count": 4,
                "last_heartbeat_at": "2026-06-19T14:35:02+00:00",
            },
        ),
        "verification-heartbeat-between-checks": hook(
            # ``_record_heartbeat`` substitutes an empty name and kind when no
            # check is current, so the schema has to accept that spelling.
            agent_mux_delivery.VERIFICATION_HEARTBEAT_EVENT,
            extra=_VERIFICATION_IDENTITY
            | {
                "check_index": 0,
                "check_count": 5,
                "check_name": "",
                "check_kind": "",
                "heartbeat_count": 1,
                "last_heartbeat_at": "2026-06-19T14:32:02+00:00",
            },
        ),
        "verification-finished": hook(
            agent_mux_delivery.VERIFICATION_FINISH_EVENT,
            extra=_VERIFICATION_IDENTITY
            | {
                "check_count": 5,
                "executed_count": 5,
                "passed_count": 4,
                "skipped_count": 0,
                "gate_count": 4,
                "diagnostic_count": 1,
                "passed": False,
                "heartbeat_count": 6,
                "acceptance": {
                    "accepted": True,
                    "reason": DEFERRED_REASON,
                    "review_outcome": "findings",
                    "deferred_failure_count": 1,
                    "deferred_failure_names": ["review-broad:correctness"],
                },
            },
        ),
        "wait-started": hook(
            agent_mux_delivery.WAIT_START_EVENT,
            extra={"question_id": "q-7", "question_preview": "which alias owns this?"},
        ),
        "wait-ended-consumed": hook(
            agent_mux_delivery.WAIT_END_EVENT,
            extra={
                "question_id": "q-7",
                "wait_reason": "reply",
                "delivery_state": "consumed",
                "closure_consumed": True,
                "closure_id": "closure-3",
            },
        ),
        "wait-ended-unconsumed": hook(
            agent_mux_delivery.WAIT_END_EVENT,
            extra={
                "question_id": "q-7",
                "wait_reason": "timeout",
                "delivery_state": "unconsumed",
                "closure_consumed": False,
            },
        ),
        "mailbox-question": hook(
            agent_mux_delivery.MAILBOX_QUESTION_EVENT,
            extra={
                "question_id": "q-7",
                "question_created_at": "2026-06-19T15:11:40+00:00",
                "unanswered_questions": 1,
                "delivery_state": "open",
                "mailbox_projection_sequence": "31",
                "source_event_id": "20260619T140000Z-1:mailbox-question:31",
            },
        ),
        "mailbox-reply": hook(
            agent_mux_delivery.MAILBOX_REPLY_EVENT,
            extra={
                "question_id": "q-7",
                "delivery_state": "accepted",
                "consumed": False,
                "mailbox_projection_sequence": "32",
                "source_event_id": "20260619T140000Z-1:mailbox-reply:32",
            },
        ),
        "mailbox-resolution": hook(
            agent_mux_delivery.MAILBOX_RESOLUTION_EVENT,
            extra={
                "question_id": "q-7",
                "resolution_action": "withdraw",
                "delivery_state": "accepted",
                "consumed": False,
                "mailbox_projection_sequence": "33",
                "source_event_id": "20260619T140000Z-1:mailbox-resolution:33",
            },
        ),
        "paused": hook(
            agent_mux_delivery.PAUSE_EVENT,
            payload={
                "pause_reason": "blocked",
                "pause_message": "blocked - a mailbox question is unanswered",
            },
            extra={
                "pause_reason": "blocked",
                "pause_message": "blocked - a mailbox question is unanswered",
            },
        ),
        "finished": hook(
            agent_mux_delivery.FINISH_EVENT,
            payload={"run_finished_at": "2026-06-19T16:40:00+00:00"},
        ),
    }


if __name__ == "__main__":
    unittest.main()
