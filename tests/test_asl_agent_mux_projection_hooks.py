"""Asl agent mux projection hooks regressions."""

from __future__ import annotations

import unittest
from typing import Any

from agent_session_loop import agent_mux_contract as contract
from agent_session_loop import (
    agent_mux_delivery,
    agent_mux_projection,
    run_persistence,
)

from .support_asl_agent_mux_projection import (
    _REASON_BY_EVENT,
    HOOK_SCHEMA,
    _base_payload,
    _emitted_hook_payloads,
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


if __name__ == "__main__":
    unittest.main()
