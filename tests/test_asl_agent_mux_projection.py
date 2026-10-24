"""The durable AgentMux projection and the schema-2 lifecycle hook identity.

``agent_mux_projection`` is the one place the two AgentMux surfaces are built
from, so these tests check it from both ends. Against the contract corpus: the
pinned contract-set ID, the phase vocabularies, and the field bounds all have to
still agree with the schemas AgentMux implements against, and every record this
module produces — projection and hook payload alike — has to validate against
them. Against the runner: every state transition must carry a projection, the
generation must stay monotonic when a heartbeat thread and the main loop write
concurrently, and the projection must stay body-free no matter what the run
payload it was built from contains."""

from __future__ import annotations

import unittest
from typing import Any

from agent_session_loop import agent_mux_contract as contract
from agent_session_loop import (
    agent_mux_projection,
)

from .support_asl_agent_mux_projection import (
    HOOK_SCHEMA,
    PROJECTION_SCHEMA,
    UPDATED_AT,
    _base_payload,
    _strings,
    _turn,
)

ASL_TEST_PORTABLE = True


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


if __name__ == "__main__":
    unittest.main()
