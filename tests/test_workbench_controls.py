"""PortableControlRulesTests, ControlDispatchTests coverage."""

from __future__ import annotations

import unittest

from agent_session_loop import (
    bridge_protocol,
    control_cli,
    control_queue,
    intervention,
    workbench_controls,
    workbench_server,
)
from asl_workbench import controls
from tests.workbench_controls_support import (
    RUN_ID,
    SESSION_ID,
    UNRESOLVABLE_PLAN_DIR,
    ControlsTestCase,
)

from .support import (
    assert_no_runner_import,
)

# Integration: the run controls and the redirect journal are driven here
# through the shared service and through both surfaces that call it, so a
# divergence shows up as a failure rather than as two passing suites.
ASL_TEST_LANE = "integration"


class PortableControlRulesTests(unittest.TestCase):
    """What a control means, decided without importing ASL."""

    def test_the_rules_module_never_reaches_the_worker_runtime(self) -> None:
        assert_no_runner_import(self, controls)

    def test_every_control_names_exactly_one_queue_command(self) -> None:
        commands = [controls.queue_command_for(kind) for kind in controls.CONTROL_KINDS]

        self.assertEqual(len(controls.CONTROL_KINDS), len(set(commands)))
        for kind, command in zip(controls.CONTROL_KINDS, commands, strict=True):
            with self.subTest(kind=kind):
                self.assertEqual(controls.QUEUE_COMMAND_FOR_CONTROL[kind], command)

    def test_and_every_one_of_them_is_a_command_asls_queue_accepts(self) -> None:
        """The mapping is portable because it has to be readable on a Pi, which
        is exactly why it is checked against the consumer's own closed set: a
        portable name that drifted would otherwise reach the queue as a line
        nothing consumes.
        """
        for command in controls.QUEUE_COMMAND_FOR_CONTROL.values():
            with self.subTest(command=command):
                self.assertIn(command, control_queue.COMMAND_NAMES)

    def test_an_unknown_control_is_refused_before_anything_is_read(self) -> None:
        with self.assertRaises(controls.ControlRequestError) as caught:
            controls.queue_command_for("run.detonate")

        self.assertEqual(controls.REASON_INVALID_CONTROL, caught.exception.reason)

    def test_only_the_controls_a_live_turn_owns_have_an_asl_action(self) -> None:
        """Harness selection is about the *next* run, so it has no turn to be
        fresh about. Giving it one would invent an authorization ASL does not
        define; leaving it out is what makes the strict path refuse it by name.
        """
        self.assertIsNone(controls.asl_action_for(controls.CONTROL_SELECT_HARNESS))
        self.assertEqual(
            intervention.ACTION_STOP_AFTER_CURRENT,
            controls.asl_action_for(controls.CONTROL_STOP_AFTER_CURRENT),
        )
        self.assertEqual(
            intervention.ACTION_ABORT, controls.asl_action_for(controls.CONTROL_ABORT)
        )

    def test_an_abort_note_is_decided_by_the_two_facts_that_decide_it(self) -> None:
        self.assertEqual(
            controls.ABORT_NOTE_NO_RUNNER,
            controls.abort_note_code(runner_present=False, turn_active=False),
        )
        self.assertEqual(
            controls.ABORT_NOTE_WILL_APPLY,
            controls.abort_note_code(runner_present=True, turn_active=True),
        )
        self.assertEqual(
            controls.ABORT_NOTE_DEGRADES,
            controls.abort_note_code(runner_present=True, turn_active=False),
        )

    def test_every_note_code_has_asl_s_own_sentence_behind_it(self) -> None:
        """The codes are portable and the wording is ASL's, so an operator who
        aborts from a browser is told what the terminal would have told them.
        """
        for code in controls.ABORT_NOTE_CODES:
            with self.subTest(code=code):
                self.assertIn(code, workbench_controls.ABORT_NOTES)
        self.assertEqual(
            control_cli.ABORT_WILL_APPLY_NOTE,
            workbench_controls.ABORT_NOTES[controls.ABORT_NOTE_WILL_APPLY],
        )

    def test_an_authorization_policy_outside_the_closed_set_is_refused(self) -> None:
        for policy in controls.AUTHORIZATION_POLICIES:
            with self.subTest(policy=policy):
                controls.require_authorization(policy)
        with self.assertRaises(controls.ControlRequestError):
            controls.require_authorization("lenient")

    def test_whitespace_is_not_a_correction(self) -> None:
        with self.assertRaises(controls.ControlRequestError) as caught:
            controls.require_correction_text("   \n  ")

        self.assertEqual(controls.REASON_EMPTY_TEXT, caught.exception.reason)
        self.assertEqual("stay put", controls.require_correction_text("stay put"))

    def test_a_named_turn_is_checked_for_shape_and_nothing_more(self) -> None:
        fields = controls.expected_target_fields(
            {
                "run_id": RUN_ID,
                "session_id": SESSION_ID,
                "turn_index": 1,
                "harness": "claude",
            },
            label="expected_target",
        )

        self.assertEqual(sorted(controls.EXPECTED_TARGET_FIELDS), sorted(fields))
        self.assertEqual(1, fields["turn_index"])

    def test_a_boolean_turn_index_is_not_an_integer(self) -> None:
        """``True`` is an ``int`` in Python and must not be read as one here."""
        with self.assertRaises(controls.ControlRequestError) as caught:
            controls.expected_target_fields(
                {"run_id": RUN_ID, "session_id": SESSION_ID, "turn_index": True},
                label="params.expected_target",
            )

        self.assertIn("params.expected_target.turn_index", caught.exception.message)

    def test_a_named_turn_that_is_not_an_object_is_refused(self) -> None:
        with self.assertRaises(controls.ControlRequestError) as caught:
            controls.expected_target_fields("session-9", label="expected_target")

        self.assertEqual(controls.REASON_INVALID_REQUEST, caught.exception.reason)

    def test_a_wait_is_bounded_by_the_ceiling_it_is_given(self) -> None:
        self.assertEqual(
            2.5,
            controls.bounded_wait_seconds(
                2.5, default=1.0, maximum=30.0, label="wait_seconds"
            ),
        )
        self.assertEqual(
            1.0,
            controls.bounded_wait_seconds(
                None, default=1.0, maximum=30.0, label="wait_seconds"
            ),
        )
        for raw in (True, "5", -1.0, 31.0, float("nan"), float("inf")):
            with (
                self.subTest(raw=raw),
                self.assertRaises(controls.ControlRequestError),
            ):
                controls.bounded_wait_seconds(
                    raw, default=1.0, maximum=30.0, label="wait_seconds"
                )

    def test_a_projection_window_truncates_the_oldest(self) -> None:
        """An operator asking where a correction got to is asking about a recent
        one, so the window keeps the tail rather than the head.
        """
        window = controls.newest_operations(
            list(range(controls.MAX_REDIRECT_OPERATIONS + 5))
        )

        self.assertEqual(controls.MAX_REDIRECT_OPERATIONS, len(window))
        self.assertEqual(controls.MAX_REDIRECT_OPERATIONS + 4, window[-1])
        self.assertEqual(5, window[0])

    def test_both_surfaces_take_their_ceiling_from_here(self) -> None:
        self.assertEqual(
            controls.MAX_REDIRECT_OPERATIONS, bridge_protocol.MAX_REDIRECT_OPERATIONS
        )
        self.assertEqual(
            controls.MAX_REDIRECT_OPERATIONS, workbench_server.MAX_REDIRECT_OPERATIONS
        )


class ControlDispatchTests(ControlsTestCase):
    """Check, queue, and wake -- once, for whichever surface asked."""

    def test_each_control_queues_the_command_asl_s_own_cli_queues(self) -> None:
        self.live_run()

        for kind in controls.CONTROL_KINDS:
            with self.subTest(kind=kind):
                outcome = workbench_controls.dispatch_control(
                    self.plan_dir,
                    kind,
                    state_dir=self.state_dir,
                    command_id=f"cmd-{kind}",
                    harness="codex",
                )
                self.assertEqual(
                    controls.queue_command_for(kind), outcome.command.command
                )
                drained = self.queued()
                self.assertEqual((f"cmd-{kind}",), drained.consumed_ids)
                self.assertEqual((), drained.rejected)

    def test_a_queued_control_wakes_the_runner_it_was_queued_for(self) -> None:
        self.live_run()

        outcome = workbench_controls.dispatch_control(
            self.plan_dir,
            controls.CONTROL_STOP_AFTER_CURRENT,
            state_dir=self.state_dir,
            command_id="cmd-wake",
        )

        self.assertEqual(
            control_queue.queue_path_for_plan(self.plan_dir, self.state_dir),
            outcome.dispatch.queue_path,
        )

    def test_an_open_caller_may_queue_for_a_runner_that_has_not_started(self) -> None:
        """The CLI has always allowed this, and the runner is still the
        authority: the command waits in the queue for whoever picks the plan up.
        """
        outcome = workbench_controls.dispatch_control(
            self.plan_dir,
            controls.CONTROL_STOP_AFTER_CURRENT,
            state_dir=self.state_dir,
            command_id="cmd-early",
        )

        self.assertFalse(outcome.activity.runner_present)
        self.assertEqual(controls.ABORT_NOTE_NO_RUNNER, outcome.activity.note_code)
        self.assertEqual(("cmd-early",), self.queued().consumed_ids)

    def test_a_strict_caller_may_not(self) -> None:
        with self.assertRaises(workbench_controls.ControlRefused) as caught:
            workbench_controls.dispatch_control(
                self.plan_dir,
                controls.CONTROL_STOP_AFTER_CURRENT,
                state_dir=self.state_dir,
                command_id="cmd-strict",
                authorization=controls.AUTHORIZATION_STRICT,
            )

        self.assertEqual(intervention.REASON_NO_ACTIVE_RUN, caught.exception.reason)
        self.assertEqual((), self.queued().consumed_ids)

    def test_a_strict_caller_carrying_a_stale_proof_queues_nothing(self) -> None:
        self.live_run()
        stale = self.action_token()
        self.write_state(turn_index=None)

        with self.assertRaises(workbench_controls.ControlRefused) as caught:
            workbench_controls.dispatch_control(
                self.plan_dir,
                controls.CONTROL_ABORT,
                state_dir=self.state_dir,
                command_id="cmd-stale",
                authorization=controls.AUTHORIZATION_STRICT,
                action_token=stale,
                grace_seconds=control_queue.DEFAULT_ABORT_GRACE_SECONDS,
            )

        self.assertEqual(
            intervention.REASON_STALE_ACTION_TOKEN, caught.exception.reason
        )
        self.assertEqual(controls.SCOPE_INTERVENTION, caught.exception.scope)
        self.assertEqual((), self.queued().consumed_ids)

    def test_an_abort_reports_what_it_is_about_to_mean(self) -> None:
        """Appending to a durable queue always succeeds, so the honest part of
        the answer is the run it is about to reach.
        """
        self.live_run()

        outcome = workbench_controls.dispatch_control(
            self.plan_dir,
            controls.CONTROL_ABORT,
            state_dir=self.state_dir,
            command_id="cmd-abort",
            grace_seconds=control_queue.DEFAULT_ABORT_GRACE_SECONDS,
        )

        self.assertTrue(outcome.activity.runner_present)
        self.assertTrue(outcome.activity.turn_active)
        self.assertEqual(control_cli.ABORT_WILL_APPLY_NOTE, outcome.activity.note)

    def test_an_abort_against_a_runner_holding_no_turn_says_so(self) -> None:
        self.write_state(turn_index=None)
        self.write_lock()

        activity = workbench_controls.inspect_activity(
            self.plan_dir, state_dir=self.state_dir
        )

        self.assertTrue(activity.runner_present)
        self.assertFalse(activity.turn_active)
        self.assertEqual(control_cli.ABORT_DEGRADES_NOTE, activity.note)

    def test_a_plan_with_no_state_at_all_is_simply_not_running(self) -> None:
        """No lock, no state file: "nothing is running" is the true answer, and
        it is the one a local operator queuing ahead of a runner has always got.
        """
        activity = workbench_controls.inspect_activity(
            self.plans_root / "not-a-plan", state_dir=self.state_dir
        )

        self.assertFalse(activity.runner_present)
        self.assertEqual(control_cli.NO_ACTIVE_RUN_NOTE, activity.note)

    def test_a_plan_path_that_cannot_be_resolved_is_no_runner_to_an_open_caller(
        self,
    ) -> None:
        activity = workbench_controls.inspect_activity(
            UNRESOLVABLE_PLAN_DIR, state_dir=self.state_dir
        )

        self.assertIsNone(activity.facts)
        self.assertEqual(control_cli.NO_ACTIVE_RUN_NOTE, activity.note)

    def test_and_a_refusal_to_a_strict_one(self) -> None:
        with self.assertRaises(workbench_controls.ControlRefused) as caught:
            workbench_controls.inspect_activity(
                UNRESOLVABLE_PLAN_DIR, state_dir=self.state_dir, strict=True
            )

        self.assertEqual(intervention.REASON_INVALID_PLAN, caught.exception.reason)
        self.assertEqual(controls.SCOPE_INTERVENTION, caught.exception.scope)

    def test_an_unsupported_harness_is_refused_before_anything_is_queued(self) -> None:
        self.live_run()

        with self.assertRaises(workbench_controls.ControlRefused) as caught:
            workbench_controls.dispatch_control(
                self.plan_dir,
                controls.CONTROL_SELECT_HARNESS,
                state_dir=self.state_dir,
                command_id="cmd-harness",
                harness="telepathy",
            )

        self.assertEqual(controls.REASON_INVALID_HARNESS, caught.exception.reason)
        self.assertEqual((), self.queued().consumed_ids)

    def test_a_grace_the_queue_rejects_names_the_field(self) -> None:
        """The queue answers with a code; a client needs the field back."""
        self.live_run()

        with self.assertRaises(workbench_controls.ControlQueueRefused) as caught:
            workbench_controls.dispatch_control(
                self.plan_dir,
                controls.CONTROL_ABORT,
                state_dir=self.state_dir,
                command_id="cmd-grace",
                grace_seconds=0,
            )

        self.assertEqual(control_queue.REJECT_INVALID_GRACE, caught.exception.reason)
        self.assertIn("grace_seconds", caught.exception.message)
        self.assertEqual((), self.queued().consumed_ids)


if __name__ == "__main__":
    unittest.main()
