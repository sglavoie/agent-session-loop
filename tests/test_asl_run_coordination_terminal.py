"""Test asl run coordination terminal."""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    events,
    harnesses,
    run_control,
    run_coordination_control,
    run_coordination_mailbox_question,
    run_coordination_terminal,
    supervisor_audit,
)
from agent_session_loop import run_coordination_constants as wait_budgets
from tests import support_asl_run_coordination as fixtures


class ControlPayloadTests(unittest.TestCase):
    """The ``control`` slot and its event can never disagree."""

    def test_a_consumed_abort_clears_the_pending_fields(self) -> None:
        control_payload = run_coordination_control.initial_control_payload()
        state = run_control.RunControlState()
        requested = state.request_abort_turn(grace_seconds=30, reason="stop")
        run_coordination_control.apply_run_control_action(control_payload, requested)
        control_payload["abort_deadline"] = "2026-01-01T00:00:00+00:00"

        consumed = state.consume_abort_turn()
        run_coordination_control.apply_run_control_action(control_payload, consumed)

        self.assertFalse(control_payload["abort_turn_requested"])
        self.assertIsNone(control_payload["abort_grace_seconds"])
        self.assertIsNone(control_payload["abort_reason"])
        self.assertIsNone(control_payload["abort_deadline"])
        self.assertEqual(
            control_payload["last_action"],
            events.RUN_CONTROL_ACTION_ABORT_TURN_HONORED,
        )

    def test_a_pending_abort_keeps_the_grace_it_was_requested_with(self) -> None:
        control_payload = run_coordination_control.initial_control_payload()
        state = run_control.RunControlState()

        requested = state.request_abort_turn(grace_seconds=30, reason="stop")
        run_coordination_control.apply_run_control_action(control_payload, requested)

        self.assertTrue(control_payload["abort_turn_requested"])
        self.assertEqual(control_payload["abort_grace_seconds"], 30)
        self.assertEqual(control_payload["abort_reason"], "stop")

    def test_every_action_name_has_an_event_action_and_a_message(self) -> None:
        self.assertEqual(
            set(run_coordination_control.RUN_CONTROL_EVENT_ACTIONS),
            set(run_coordination_control.RUN_CONTROL_EVENT_MESSAGES),
        )


class TerminalAssuranceTests(fixtures.CoordinationTestCase):
    """The finalization phase closes exactly once, whatever the audit does."""

    def setUp(self) -> None:
        super().setUp()
        self.run_dir = Path(self.tmp.name) / "run"
        self.run_dir.mkdir()
        self.payload.update(
            {
                "ok": True,
                "run_dir": str(self.run_dir),
                "assurance": {
                    "schema_version": 1,
                    "run_id": fixtures.RUN_ID,
                    "state": "pending",
                    "eligible": True,
                },
            }
        )
        self.audit_plan = supervisor_audit.AuditPlan(
            config=supervisor_audit.AuditConfig(),
            role=None,
            state="pending",
            reason=None,
        )
        self.context = run_coordination_terminal.TerminalAuditContext(
            plan=self.audit_plan,
            run_dir=self.run_dir,
            state_dir=self.state_dir,
            plan_dir=self.plan_dir,
            workspace_root=Path(self.tmp.name),
            ledger_state_dir=self.state_dir,
        )
        self.phase = fixtures.RecordingPhase()

    def coordinator(self, **overrides: Any):
        kwargs: dict[str, Any] = {
            "payload": self.payload,
            "context": self.context,
            "persistence": self.persistence,
            "phase": self.phase,
            "state_snapshot": dict,
        }
        kwargs.update(overrides)
        return run_coordination_terminal.TerminalAssuranceCoordinator(**kwargs)

    def summary(self, state: str) -> supervisor_audit.AssuranceSummary:
        return supervisor_audit.AssuranceSummary(
            run_id=fixtures.RUN_ID,
            state=state,
            eligible=True,
            reason=None,
            role=None,
        )

    def test_a_completed_audit_publishes_its_verdict_and_closes_the_phase(self) -> None:
        with mock.patch.object(
            run_coordination_terminal.supervisor_audit,
            "audit_terminal_run",
            return_value=self.summary("all-clear"),
        ):
            outcome = self.coordinator().run()

        self.assertFalse(outcome.cancelled)
        self.assertTrue(outcome.audited)
        self.assertTrue(outcome.reported)
        self.assertEqual(outcome.state, "all-clear")
        self.assertFalse(outcome.degraded)
        self.assertEqual(self.phase.calls, ["start", "finish"])
        self.assertEqual(self.payload["assurance"]["state"], "all-clear")
        self.assertIn("assurance_report", self.payload)

    def test_a_cancelled_audit_closes_the_phase_as_cancelled(self) -> None:
        with mock.patch.object(
            run_coordination_terminal.supervisor_audit,
            "audit_terminal_run",
            side_effect=supervisor_audit.AuditCancelled("aborted"),
        ):
            outcome = self.coordinator().run()

        self.assertTrue(outcome.cancelled)
        self.assertEqual(self.phase.calls, ["start", "finish-cancelled"])
        self.assertEqual(self.payload["assurance"]["state"], "unavailable")
        self.assertTrue(outcome.degraded)

    def test_a_keyboard_interrupt_is_a_cancelled_audit_rather_than_a_crash(
        self,
    ) -> None:
        with mock.patch.object(
            run_coordination_terminal.supervisor_audit,
            "audit_terminal_run",
            side_effect=KeyboardInterrupt,
        ):
            outcome = self.coordinator().run()

        self.assertTrue(outcome.cancelled)
        self.assertEqual(self.phase.calls, ["start", "finish-cancelled"])
        self.assertEqual(
            self.payload["assurance"]["reason"], supervisor_audit.REASON_INTERRUPTED
        )

    def test_the_cancelled_run_keeps_the_outcome_it_had_already_earned(self) -> None:
        with mock.patch.object(
            run_coordination_terminal.supervisor_audit,
            "audit_terminal_run",
            side_effect=supervisor_audit.AuditCancelled("aborted"),
        ):
            self.coordinator().run()

        self.assertTrue(self.payload["ok"])

    def test_an_unexpected_failure_still_closes_the_phase(self) -> None:
        with (
            mock.patch.object(
                run_coordination_terminal.supervisor_audit,
                "audit_terminal_run",
                side_effect=RuntimeError("auditor exploded"),
            ),
            self.assertRaises(RuntimeError),
        ):
            self.coordinator().run()

        self.assertEqual(self.phase.calls, ["start", "finish"])

    def test_an_armed_abort_reaches_the_audit(self) -> None:
        abort = harnesses.AbortSignal()
        abort.arm("stop auditing")
        with mock.patch.object(
            run_coordination_terminal.supervisor_audit,
            "audit_terminal_run",
            return_value=self.summary("all-clear"),
        ) as audit:
            self.coordinator(abort=abort).run()

        self.assertIs(audit.call_args.kwargs["abort"], abort)

    def test_the_audit_reads_a_snapshot_taken_under_the_run_lock(self) -> None:
        taken: list[dict[str, Any]] = []

        def snapshot(payload: dict[str, Any]) -> dict[str, Any]:
            self.assertTrue(self.persistence.lock.acquire(blocking=False))
            self.persistence.lock.release()
            copied = dict(payload)
            taken.append(copied)
            return copied

        with mock.patch.object(
            run_coordination_terminal.supervisor_audit,
            "audit_terminal_run",
            return_value=self.summary("all-clear"),
        ) as audit:
            self.coordinator(state_snapshot=snapshot).run()

        self.assertEqual(len(taken), 1)
        self.assertIs(audit.call_args.kwargs["state_payload"], taken[0])

    def test_an_ineligible_run_is_not_audited_but_the_phase_still_closes(self) -> None:
        self.payload["assurance"] = {
            "schema_version": 1,
            "run_id": fixtures.RUN_ID,
            "state": "pending",
            "eligible": False,
        }

        with mock.patch.object(
            run_coordination_terminal.supervisor_audit, "audit_terminal_run"
        ) as audit:
            outcome = self.coordinator().run()

        audit.assert_not_called()
        self.assertFalse(outcome.audited)
        self.assertEqual(self.phase.calls, ["start", "finish"])

    def test_an_already_graded_run_is_not_audited_again(self) -> None:
        self.payload["assurance"] = {
            "schema_version": 1,
            "run_id": fixtures.RUN_ID,
            "state": "all-clear",
            "eligible": True,
        }

        with mock.patch.object(
            run_coordination_terminal.supervisor_audit, "audit_terminal_run"
        ) as audit:
            outcome = self.coordinator().run()

        audit.assert_not_called()
        self.assertFalse(outcome.audited)
        self.assertEqual(outcome.state, "all-clear")


class DegradedAssuranceTests(fixtures.CoordinationTestCase):
    """A run whose audit could not speak says so, without failing the run."""

    def test_an_unavailable_verdict_is_recorded_as_a_degraded_success(self) -> None:
        self.payload.update(
            {
                "ok": True,
                "assurance": {
                    "state": "unavailable",
                    "reason": supervisor_audit.REASON_INTERRUPTED,
                    "artifacts": [{"locator": "run/audit/report.json"}],
                    "issue_ids": ["issue-1"],
                },
            }
        )

        run_coordination_terminal.record_degraded_assurance(
            self.payload, persistence=self.persistence
        )

        degraded = self.payload["assurance_degraded"]
        self.assertEqual(degraded["assurance_state"], "unavailable")
        self.assertFalse(degraded["blocking"])
        self.assertTrue(degraded["run_ok"])
        self.assertEqual(degraded["evidence"], ["run/audit/report.json"])
        self.assertEqual(degraded["issue_ids"], ["issue-1"])

    def test_a_settled_decision_not_to_audit_is_not_a_degradation(self) -> None:
        self.payload["assurance"] = {"state": "skipped"}
        self.payload["assurance_degraded"] = {"stale": True}

        run_coordination_terminal.record_degraded_assurance(
            self.payload, persistence=self.persistence
        )

        self.assertNotIn("assurance_degraded", self.payload)
        self.assertEqual(self.persistence.states, [])


class QuestionTextTests(fixtures.CoordinationTestCase):
    """A question body is bounded and byte-stable across a retried preparation."""

    def test_a_short_message_is_the_question_verbatim(self) -> None:
        context = fixtures.qna_context(
            self.plan_dir, self.state_dir, last_message="short"
        )

        self.assertEqual(
            run_coordination_mailbox_question.mailbox_question_text(context), "short"
        )

    def test_an_oversized_message_is_truncated_deterministically(self) -> None:
        body = "x" * (wait_budgets.MAILBOX_QUESTION_TEXT_MAX_CHARS + 500)
        context = fixtures.qna_context(self.plan_dir, self.state_dir, last_message=body)

        first = run_coordination_mailbox_question.mailbox_question_text(context)
        second = run_coordination_mailbox_question.mailbox_question_text(context)

        self.assertEqual(first, second)
        self.assertTrue(
            first.startswith("x" * wait_budgets.MAILBOX_QUESTION_TEXT_MAX_CHARS)
        )
        self.assertIn("question truncated", first)
        self.assertIn(str(context.last_message_path), first)

    def test_the_question_id_is_stable_across_retries_of_one_turn(self) -> None:
        first = run_coordination_mailbox_question.mailbox_question_id(
            plan_dir=self.plan_dir,
            run_id=fixtures.RUN_ID,
            session_id=fixtures.SESSION_ID,
            turn_index=1,
        )
        second = run_coordination_mailbox_question.mailbox_question_id(
            plan_dir=self.plan_dir,
            run_id=fixtures.RUN_ID,
            session_id=fixtures.SESSION_ID,
            turn_index=1,
        )
        other_turn = run_coordination_mailbox_question.mailbox_question_id(
            plan_dir=self.plan_dir,
            run_id=fixtures.RUN_ID,
            session_id=fixtures.SESSION_ID,
            turn_index=2,
        )

        self.assertEqual(first, second)
        self.assertNotEqual(first, other_turn)


if __name__ == "__main__":
    unittest.main()
