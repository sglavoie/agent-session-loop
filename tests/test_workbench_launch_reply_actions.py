# mypy: disable-error-code="attr-defined,name-defined"
# ruff: noqa: F403, F405, I001
"""Shared launch/reply action and isolation tests."""

# ruff: noqa: F403, F405
from tests.workbench_launch_reply_support import *


ASL_TEST_LANE = "integration"


class SharedActionTests(_PlanFixture):
    """The one path every surface takes, driven directly."""

    def test_a_launch_is_finished_by_the_process_it_promised(self) -> None:
        with mock.patch.object(
            workbench_launch, "launch_plan", return_value=self.launch_result()
        ) as patched:
            outcome = workbench_actions.launch_run(
                self.canonical, operation_id="op-launch", state_dir=self.state_dir
            )
        patched.assert_called_once()
        self.assertEqual(operations.OPERATION_CONSUMED, outcome.status)
        self.assertEqual(operations.METHOD_LAUNCH, outcome.method)

    def test_a_resume_waits_for_a_run_to_say_it_read_the_reply(self) -> None:
        """Accepted, not consumed: a ``Popen`` is not a reader."""
        self.make_paused()
        with mock.patch.object(
            workbench_launch,
            "resume_plan_with_reply",
            return_value=self.launch_result(reply=True),
        ):
            outcome = workbench_actions.resume_with_reply(
                self.canonical,
                reply_text=REPLY_TEXT,
                operation_id="op-resume",
                state_dir=self.state_dir,
            )
        self.assertEqual(operations.OPERATION_ACCEPTED, outcome.status)
        self.assertEqual(operations.METHOD_REPLY, outcome.method)

    def test_a_mailbox_reply_is_appended_and_left_accepted(self) -> None:
        question_id = self.make_waiting()
        outcome = workbench_actions.reply_to_question(
            self.canonical,
            text=REPLY_TEXT,
            operation_id="op-reply",
            state_dir=self.state_dir,
            question_id=question_id,
        )
        self.assertEqual(operations.OPERATION_ACCEPTED, outcome.status)
        self.assertEqual(question_id, outcome.question_id)
        self.assertIsNotNone(outcome.closure_id)
        view = mailbox.read_mailbox(self.canonical, state_dir=self.state_dir)
        replies = [
            message
            for message in view.messages
            if message.kind == mailbox_vocabulary.KIND_REPLY
        ]
        self.assertEqual(1, len(replies))

    def test_a_reply_witnessed_later_reconciles_to_consumed(self) -> None:
        """The operation's own end, read out of the run's record of reading it."""
        question_id = self.make_waiting()
        workbench_actions.reply_to_question(
            self.canonical,
            text=REPLY_TEXT,
            operation_id="op-reply",
            state_dir=self.state_dir,
            question_id=question_id,
        )
        self.append_reply_provided(
            origin=actions.REPLY_ORIGIN_MAILBOX, question_id=question_id
        )
        workbench_operations.reconcile_plan(self.canonical, state_dir=self.state_dir)
        row = self.row("op-reply")
        self.assertEqual(operations.OPERATION_CONSUMED, row["status"])
        self.assertEqual(operations.DETAIL_REPLY_WITNESSED, row["detail_code"])

    def test_a_mailbox_receipt_reconciles_an_in_turn_reply_to_consumed(self) -> None:
        """An in-turn ask writes a receipt without starting another turn."""
        question_id = self.make_waiting()
        outcome = workbench_actions.reply_to_question(
            self.canonical,
            text=REPLY_TEXT,
            operation_id="op-reply",
            state_dir=self.state_dir,
            question_id=question_id,
        )
        assert outcome.closure_id is not None
        mailbox_appenders.append_receipt(
            self.canonical,
            question_id,
            outcome.closure_id,
            state_dir=self.state_dir,
        )

        workbench_operations.reconcile_plan(self.canonical, state_dir=self.state_dir)

        row = self.row("op-reply")
        self.assertEqual(operations.OPERATION_CONSUMED, row["status"])
        self.assertEqual(operations.DETAIL_REPLY_WITNESSED, row["detail_code"])

    def test_a_reply_to_another_question_leaves_this_one_open(self) -> None:
        question_id = self.make_waiting()
        workbench_actions.reply_to_question(
            self.canonical,
            text=REPLY_TEXT,
            operation_id="op-reply",
            state_dir=self.state_dir,
            question_id=question_id,
        )
        self.append_reply_provided(
            origin=actions.REPLY_ORIGIN_MAILBOX, question_id="q-somebody-else"
        )
        workbench_operations.reconcile_plan(self.canonical, state_dir=self.state_dir)
        self.assertEqual(operations.OPERATION_ACCEPTED, self.row("op-reply")["status"])

    def test_a_resent_id_replays_instead_of_spawning_again(self) -> None:
        self.make_paused()
        with mock.patch.object(
            workbench_launch,
            "resume_plan_with_reply",
            return_value=self.launch_result(reply=True),
        ) as patched:
            first = workbench_actions.resume_with_reply(
                self.canonical,
                reply_text=REPLY_TEXT,
                operation_id="op-resume",
                state_dir=self.state_dir,
            )
            second = workbench_actions.resume_with_reply(
                self.canonical,
                reply_text=REPLY_TEXT,
                operation_id="op-resume",
                state_dir=self.state_dir,
            )
        patched.assert_called_once()
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(1, len(self.rows()))

    def test_a_named_run_that_moved_is_refused_without_any_token(self) -> None:
        """A surface that carries no snapshot still cannot act on a stale target.

        Forcing the browser to acquire an action token would be a new
        requirement on a surface that never had one. Naming a run *is* a claim
        about that run, and the claim is checked either way.
        """
        self.make_paused()
        with (
            mock.patch.object(workbench_launch, "resume_plan_with_reply") as patched,
            self.assertRaises(intervention.InterventionError) as caught,
        ):
            workbench_actions.resume_with_reply(
                self.canonical,
                reply_text=REPLY_TEXT,
                operation_id="op-resume",
                state_dir=self.state_dir,
                run_id="20990101T000000Z-9",
            )
        self.assertEqual(intervention.REASON_RUN_MISMATCH, caught.exception.reason)
        patched.assert_not_called()
        # Refused before admission, so the id it carried is still spendable.
        self.assertEqual([], self.rows())

    def test_a_question_that_is_not_open_is_refused_without_any_token(self) -> None:
        self.make_waiting()
        with self.assertRaises(intervention.InterventionError) as caught:
            workbench_actions.reply_to_question(
                self.canonical,
                text=REPLY_TEXT,
                operation_id="op-reply",
                state_dir=self.state_dir,
                question_id="q-not-open",
            )
        self.assertEqual(intervention.REASON_QUESTION_MISMATCH, caught.exception.reason)
        self.assertEqual([], self.rows())

    def test_a_reply_to_a_plan_with_no_run_is_refused(self) -> None:
        with self.assertRaises(intervention.InterventionError) as caught:
            workbench_actions.reply_to_question(
                self.canonical,
                text=REPLY_TEXT,
                operation_id="op-reply",
                state_dir=self.state_dir,
            )
        self.assertEqual(intervention.REASON_NO_ACTIVE_RUN, caught.exception.reason)

    def test_a_guard_refusal_closes_the_row_rather_than_holding_the_plan(self) -> None:
        """An open row holds the plan's launch scope; a withdrawn one must not.

        The row is ``cancelled`` rather than ``rejected`` because nothing was
        attempted -- and because ``pending -> rejected`` is not a transition the
        vocabulary has, so writing it would fail silently and leave the row
        open forever.
        """
        with (
            mock.patch.object(workbench_launch, "launch_plan") as patched,
            self.assertRaises(workbench_actions.ActionRefused) as caught,
        ):
            workbench_actions.launch_run(
                self.canonical,
                operation_id="op-launch",
                state_dir=self.state_dir,
                guard=lambda facts: ("run-already-active", "somebody has it"),
            )
        self.assertEqual("run-already-active", caught.exception.reason)
        patched.assert_not_called()
        self.assertEqual(
            operations.OPERATION_CANCELLED, self.row("op-launch")["status"]
        )
        self.assertEqual(
            [],
            workbench_operations.open_operations(
                self.canonical, state_dir=self.state_dir
            ),
        )
        # And the plan is free: a new submission is admitted rather than
        # refused by the withdrawn one.
        with mock.patch.object(
            workbench_launch, "launch_plan", return_value=self.launch_result()
        ):
            again = workbench_actions.launch_run(
                self.canonical, operation_id="op-launch-2", state_dir=self.state_dir
            )
        self.assertEqual(operations.OPERATION_CONSUMED, again.status)

    def test_a_launch_refused_before_the_spawn_is_closed_as_rejected(self) -> None:
        error = workbench_launch.LaunchError(
            workbench_launch.LAUNCH_REASON_INVALID_PROFILE, "no such harness"
        )
        with (
            mock.patch.object(workbench_launch, "launch_plan", side_effect=error),
            self.assertRaises(workbench_launch.LaunchError),
        ):
            workbench_actions.launch_run(
                self.canonical,
                operation_id="op-launch",
                state_dir=self.state_dir,
            )
        row = self.row("op-launch")
        self.assertEqual(operations.OPERATION_REJECTED, row["status"])
        self.assertEqual(operations.DETAIL_LAUNCH_REFUSED, row["detail_code"])

    def test_a_child_killed_after_the_spawn_is_left_uncertain(self) -> None:
        """The one failure that must not be recorded as "did not run"."""
        error = workbench_launch.LaunchError(
            workbench_launch.LAUNCH_REASON_PROCESS_FAILED,
            "failed to stream reply text to child stdin",
            spawned=True,
        )
        self.make_paused()
        with (
            mock.patch.object(
                workbench_launch, "resume_plan_with_reply", side_effect=error
            ),
            self.assertRaises(workbench_launch.LaunchError),
        ):
            workbench_actions.resume_with_reply(
                self.canonical,
                reply_text=REPLY_TEXT,
                operation_id="op-resume",
                state_dir=self.state_dir,
            )
        self.assertEqual(
            operations.OPERATION_UNCONFIRMED, self.row("op-resume")["status"]
        )

    def test_the_reply_text_never_reaches_the_journal(self) -> None:
        question_id = self.make_waiting()
        secret = "the-operator-typed-this"
        workbench_actions.reply_to_question(
            self.canonical,
            text=secret,
            operation_id="op-reply",
            state_dir=self.state_dir,
            question_id=question_id,
        )
        raw = workbench_operations.journal_path_for_plan(
            self.canonical, self.state_dir
        ).read_bytes()
        self.assertNotIn(secret.encode("utf-8"), raw)
        self.assertIn(b"op-reply", raw)


class LaunchIsolationTests(_PlanFixture):
    """A run outlives the tooling that started it."""

    def test_a_launched_run_gets_its_own_session(self) -> None:
        """Ctrl-C in the launcher's terminal must not reach the worker.

        A Workbench server stopped at the keyboard, a bridge whose connector
        went away, a closing terminal -- each signals a process *group*. A child
        in the launcher's group would be killed mid-turn by an event that has
        nothing to do with the plan.
        """
        spawned: list[_FakePopen] = []

        def factory(argv: Any, **kwargs: Any) -> _FakePopen:
            process = _FakePopen(argv, **kwargs)
            spawned.append(process)
            return process

        workbench_launch.launch_plan(
            self.canonical, state_dir=self.state_dir, popen_factory=factory
        )
        self.assertEqual(1, len(spawned))
        self.assertTrue(spawned[0].start_new_session)

    def test_a_resume_streaming_a_reply_is_isolated_too(self) -> None:
        self.make_paused()
        spawned: list[_FakePopen] = []

        def factory(argv: Any, **kwargs: Any) -> _FakePopen:
            process = _FakePopen(argv, **kwargs)
            spawned.append(process)
            return process

        workbench_launch.resume_plan_with_reply(
            self.canonical,
            reply_text=REPLY_TEXT,
            state_dir=self.state_dir,
            popen_factory=factory,
        )
        self.assertEqual(1, len(spawned))
        self.assertTrue(spawned[0].start_new_session)
        self.assertEqual(REPLY_TEXT, spawned[0].stdin_text)


class EventRecordShapeTests(_PlanFixture):
    """The witness reads the record ASL actually writes, not a copy of it."""

    def test_the_event_name_is_asls_own(self) -> None:
        self.assertEqual(events.REPLY_PROVIDED_EVENT, actions.REPLY_PROVIDED_EVENT)

    def test_the_log_the_witness_reads_is_the_one_the_runner_appends_to(
        self,
    ) -> None:
        self.assertEqual(
            state.plan_state_dir(self.canonical, self.state_dir)
            / events.EVENTS_FILE_NAME,
            workbench_operations.events_path_for_plan(self.canonical, self.state_dir),
        )
