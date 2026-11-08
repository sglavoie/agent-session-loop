"""MailboxCliTestsPart8: questions."""

from __future__ import annotations

from .asl_control_mailbox_cli_supervise_support import (
    MailboxCliTestsPart8Base,
    io,
    mailbox_appenders,
    mailbox_cli_constants,
    mailbox_cli_outbox,
    mailbox_cli_waits,
    mailbox_validation,
    mailbox_vocabulary,
    mailbox_waits,
    mock,
    redirect_stdout,
)


class MailboxCliTestsPart8(MailboxCliTestsPart8Base):
    def test_unidentified_supervision_end_never_ends_a_correlated_wait(
        self,
    ) -> None:
        # A record from a helper predating launch correlation carries no id.
        record = mailbox_appenders.append_supervision_end(
            self.plan_dir,
            "helper exited: complete",
            outcome=mailbox_vocabulary.SUPERVISION_OUTCOME_COMPLETE,
            state_dir=self.state_dir,
        )

        code, payload = self.payload_from(
            mailbox_cli_outbox.outbox_wait_from_args,
            self.args(
                timeout=0,
                include=mailbox_cli_constants.OUTBOX_INCLUDE_ALL,
                supervision_id=mailbox_validation.new_supervision_id(),
            ),
        )

        self.assertEqual(0, code)
        self.assertFalse(payload["supervision_ended"])
        self.assertNotIn("supervision_id", payload["supervision_ends"][0])
        self.assertEqual(record.seq, payload["next_since"])

    def test_uncorrelated_wait_keeps_treating_any_record_as_terminal(
        self,
    ) -> None:
        record = mailbox_appenders.append_supervision_end(
            self.plan_dir,
            "helper exited: complete",
            outcome=mailbox_vocabulary.SUPERVISION_OUTCOME_COMPLETE,
            state_dir=self.state_dir,
            supervision_id=mailbox_validation.new_supervision_id(),
        )

        code, payload = self.payload_from(
            mailbox_cli_outbox.outbox_wait_from_args,
            self.args(timeout=0, include=mailbox_cli_constants.OUTBOX_INCLUDE_ALL),
        )

        self.assertEqual(0, code)
        self.assertTrue(payload["supervision_ended"])
        self.assertIsNone(payload["supervision_id"])
        self.assertEqual(record.seq, payload["next_since"])

    def test_outbox_wait_rejects_a_malformed_supervision_id(self) -> None:
        code, payload = self.payload_from(
            mailbox_cli_outbox.outbox_wait_from_args,
            self.args(
                timeout=0,
                include=mailbox_cli_constants.OUTBOX_INCLUDE_ALL,
                supervision_id="not-a-uuid",
            ),
        )

        self.assertEqual(1, code)
        self.assertFalse(payload["ok"])
        self.assertEqual("invalid-supervision-id", payload["reason"])

    def test_outbox_wait_text_reports_an_uncorrelated_supervision_end(
        self,
    ) -> None:
        record = mailbox_appenders.append_supervision_end(
            self.plan_dir,
            "helper exited: complete",
            outcome=mailbox_vocabulary.SUPERVISION_OUTCOME_COMPLETE,
            state_dir=self.state_dir,
            supervision_id=mailbox_validation.new_supervision_id(),
        )
        launch = mailbox_validation.new_supervision_id()

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = mailbox_cli_outbox.outbox_wait_from_args(
                self.args(
                    json=False,
                    timeout=0,
                    include=mailbox_cli_constants.OUTBOX_INCLUDE_ALL,
                    supervision_id=launch,
                )
            )

        self.assertEqual(0, code)
        lines = stdout.getvalue().splitlines()
        self.assertIn(
            f"Supervision end {record.id} (seq {record.seq}):",
            lines,
        )
        self.assertIn(
            f"No supervision end record matches supervision id {launch}; "
            "supervision continues.",
            lines,
        )
        self.assertEqual(f"Next cursor: {record.seq}", lines[-1])

    def test_outbox_supervise_prints_only_idle_when_nothing_is_actionable(
        self,
    ) -> None:
        self.write_active_turn(run_id="run-live", session_id="5", turn_index=2)
        notice = mailbox_appenders.append_notice(
            self.plan_dir,
            "checkpoint reached",
            state_dir=self.state_dir,
            run_id="run-live",
            session_id="5",
            turn_index=2,
        )
        launch = mailbox_validation.new_supervision_id()

        code, lines = self.supervise_lines(supervision_id=launch)

        self.assertEqual(0, code)
        self.assertEqual(["IDLE"], lines)
        self.assertGreaterEqual(self.cursor_on_disk(launch), notice.seq)

    def test_outbox_supervise_resumes_its_persisted_cursor(self) -> None:
        self.write_active_turn(run_id="run-live", session_id="5", turn_index=2)
        question = mailbox_appenders.append_question(
            self.plan_dir,
            "Which shape?",
            state_dir=self.state_dir,
            run_id="run-live",
        )
        launch = mailbox_validation.new_supervision_id()

        code, first = self.supervise_lines(supervision_id=launch)
        self.assertEqual(0, code)
        self.assertEqual("ACTION questions=1", first[0])
        self.assertIn(
            f"Question {question.id} (seq {question.seq}):",
            first,
        )
        self.assertEqual(question.seq, self.cursor_on_disk(launch))

        code, second = self.payload_from(
            mailbox_cli_outbox.outbox_supervise_from_args,
            self.args(timeout=0, supervision_id=launch),
        )
        self.assertEqual(0, code)
        self.assertEqual(question.seq, second["since"])

        # A different launch keeps its own cursor and still sees the question.
        code, other = self.payload_from(
            mailbox_cli_outbox.outbox_supervise_from_args,
            self.args(
                timeout=0, supervision_id=mailbox_validation.new_supervision_id()
            ),
        )
        self.assertEqual(0, code)
        self.assertEqual(0, other["since"])
        self.assertEqual("ACTION", other["state"])

    def test_outbox_supervise_reports_again_a_question_left_unanswered(
        self,
    ) -> None:
        # The first call moved the cursor past the question before any reply
        # landed; a supervisor whose reply failed must not be told IDLE while
        # the run waits on it.
        self.write_active_turn(run_id="run-live", session_id="5", turn_index=2)
        question = mailbox_appenders.append_question(
            self.plan_dir,
            "Which shape?",
            state_dir=self.state_dir,
            run_id="run-live",
        )
        launch = mailbox_validation.new_supervision_id()

        code, first = self.supervise_lines(supervision_id=launch)
        self.assertEqual(0, code)
        self.assertEqual("ACTION questions=1", first[0])
        self.assertEqual(question.seq, self.cursor_on_disk(launch))

        code, second = self.supervise_lines(supervision_id=launch)
        self.assertEqual(0, code)
        self.assertEqual("ACTION questions=1", second[0])
        self.assertIn(f"Question {question.id} (seq {question.seq}):", second)

        mailbox_appenders.append_reply(
            self.plan_dir, question.id, "answered", state_dir=self.state_dir
        )
        code, third = self.supervise_lines(supervision_id=launch)
        self.assertEqual(0, code)
        self.assertEqual(["IDLE"], third)

    def test_outbox_supervise_waits_before_reporting_a_carried_question(
        self,
    ) -> None:
        self.write_active_turn(run_id="run-live", session_id="5", turn_index=2)
        mailbox_appenders.append_question(
            self.plan_dir,
            "Which shape?",
            state_dir=self.state_dir,
            run_id="run-live",
        )
        launch = mailbox_validation.new_supervision_id()
        self.supervise_lines(supervision_id=launch)

        # The repeat waits out the carried-question delay, not the whole
        # timeout, and never returns at once: a supervisor reissuing while a
        # human considers the question must not spin.
        timeouts: list[float] = []
        real_wait = mailbox_cli_waits.wait_for_questions

        def recording_wait(*args, **kwargs):
            timeouts.append(kwargs["timeout"])
            return real_wait(*args, **{**kwargs, "timeout": 0})

        with mock.patch.object(mailbox_cli_waits, "wait_for_questions", recording_wait):
            code, lines = self.supervise_lines(supervision_id=launch, timeout=550)
        self.assertEqual(0, code)
        self.assertEqual("ACTION questions=1", lines[0])
        self.assertLessEqual(
            timeouts[0],
            mailbox_cli_constants.SUPERVISE_CARRIED_QUESTION_DELAY_SECONDS,
        )
        self.assertGreater(timeouts[0], 0)

    def test_outbox_supervise_drops_a_carried_question_whose_wait_ended(
        self,
    ) -> None:
        self.write_active_turn(run_id="run-live", session_id="5", turn_index=2)
        question, wait = mailbox_waits.append_wait_question(
            self.plan_dir,
            "Still blocked?",
            state_dir=self.state_dir,
            run_id="run-live",
            session_id="5",
            turn_index=2,
            waiter_origin=mailbox_vocabulary.WAITER_ORIGIN_WORKER,
        )
        launch = mailbox_validation.new_supervision_id()
        code, first = self.supervise_lines(supervision_id=launch)
        self.assertEqual("ACTION questions=1", first[0])

        # The worker's wait times out while the repeat waits out its delay.
        real_wait = mailbox_cli_waits.wait_for_questions

        def timing_out_wait(*args, **kwargs):
            mailbox_waits.finish_wait(
                self.plan_dir,
                question.id,
                wait.id,
                reason=mailbox_vocabulary.WAIT_END_REASON_TIMEOUT,
                state_dir=self.state_dir,
            )
            return real_wait(*args, **{**kwargs, "timeout": 0})

        with mock.patch.object(
            mailbox_cli_waits, "wait_for_questions", timing_out_wait
        ):
            code, lines = self.supervise_lines(supervision_id=launch)
        self.assertEqual(0, code)
        self.assertEqual(["IDLE"], lines)

    def test_outbox_supervise_never_carries_another_runs_question(self) -> None:
        self.write_active_turn(run_id="run-live", session_id="5", turn_index=2)
        mailbox_appenders.append_question(
            self.plan_dir,
            "Left open by a crashed run",
            state_dir=self.state_dir,
            run_id="run-crashed",
        )
        launch = mailbox_validation.new_supervision_id()
        self.supervise_lines(supervision_id=launch)

        code, lines = self.supervise_lines(supervision_id=launch)
        self.assertEqual(0, code)
        self.assertEqual(["IDLE"], lines)

    def test_outbox_supervise_absorbs_another_runs_new_question(self) -> None:
        # Never behind the cursor, so not carried: still not this run's, and
        # reply refuses it, so it must not read as ACTION.
        self.write_active_turn(run_id="run-live", session_id="5", turn_index=2)
        mailbox_appenders.append_question(
            self.plan_dir,
            "Left open by an earlier run",
            state_dir=self.state_dir,
            run_id="run-earlier",
        )

        code, lines = self.supervise_lines(
            supervision_id=mailbox_validation.new_supervision_id()
        )

        self.assertEqual(0, code)
        self.assertEqual(["IDLE"], lines)

    def test_outbox_supervise_absorbs_a_question_whose_wait_timed_out(
        self,
    ) -> None:
        self.write_active_turn(run_id="run-live", session_id="5", turn_index=2)
        question, wait = mailbox_waits.append_wait_question(
            self.plan_dir,
            "Still blocked?",
            state_dir=self.state_dir,
            run_id="run-live",
            session_id="5",
            turn_index=2,
            waiter_origin=mailbox_vocabulary.WAITER_ORIGIN_WORKER,
        )
        mailbox_waits.finish_wait(
            self.plan_dir,
            question.id,
            wait.id,
            reason=mailbox_vocabulary.WAIT_END_REASON_TIMEOUT,
            state_dir=self.state_dir,
        )
        launch = mailbox_validation.new_supervision_id()

        code, lines = self.supervise_lines(supervision_id=launch)
        self.assertEqual(0, code)
        self.assertEqual(["IDLE"], lines)

        # Nor is it carried back once the cursor has moved past it.
        code, lines = self.supervise_lines(supervision_id=launch)
        self.assertEqual(0, code)
        self.assertEqual(["IDLE"], lines)

    def test_outbox_supervise_absorbs_a_foreign_supervision_end(self) -> None:
        foreign = mailbox_appenders.append_supervision_end(
            self.plan_dir,
            "helper exited: complete",
            outcome=mailbox_vocabulary.SUPERVISION_OUTCOME_COMPLETE,
            state_dir=self.state_dir,
            supervision_id=mailbox_validation.new_supervision_id(),
        )
        launch = mailbox_validation.new_supervision_id()

        code, lines = self.supervise_lines(supervision_id=launch)

        self.assertEqual(0, code)
        self.assertEqual(["IDLE"], lines)
        self.assertEqual(foreign.seq, self.cursor_on_disk(launch))

    def test_outbox_supervise_absorbs_an_already_closed_question(self) -> None:
        question = mailbox_appenders.append_question(
            self.plan_dir,
            "Which shape?",
            state_dir=self.state_dir,
            run_id="run-live",
        )
        mailbox_appenders.append_reply(
            self.plan_dir,
            question.id,
            "answered",
            state_dir=self.state_dir,
        )
        launch = mailbox_validation.new_supervision_id()

        code, lines = self.supervise_lines(supervision_id=launch)

        self.assertEqual(0, code)
        self.assertEqual(["IDLE"], lines)
