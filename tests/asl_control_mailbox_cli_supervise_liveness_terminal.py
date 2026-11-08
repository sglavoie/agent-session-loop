"""MailboxCliTestsPart9: terminal."""

from __future__ import annotations

from .asl_control_mailbox_cli_supervise_liveness_support import (
    MailboxCliTestsPart9Base,
    mailbox,
    mailbox_appenders,
    mailbox_cli_agent_mux,
    mailbox_cli_constants,
    mailbox_cli_outbox,
    mailbox_cli_status,
    mailbox_cli_waits,
    mailbox_constants,
    mailbox_cursors,
    mailbox_models,
    mailbox_snapshot,
    mailbox_validation,
    mailbox_vocabulary,
    mock,
    supervision_liveness,
    time,
    uuid,
)


class MailboxCliTestsPart9(MailboxCliTestsPart9Base):
    def test_terminal_record_survives_interrupted_output_after_cursor_write(
        self,
    ) -> None:
        launch = mailbox_validation.new_supervision_id()
        self.write_liveness_record(launch)
        terminal = mailbox_appenders.append_supervision_end(
            self.plan_dir,
            "helper exited: complete",
            outcome=mailbox_vocabulary.SUPERVISION_OUTCOME_COMPLETE,
            state_dir=self.state_dir,
            supervision_id=launch,
        )
        with (
            mock.patch.object(
                mailbox_cli_agent_mux,
                "notify_agent_mux_mailbox_question",
                side_effect=KeyboardInterrupt,
            ),
            self.assertRaises(KeyboardInterrupt),
        ):
            self.supervise_lines(supervision_id=launch)
        self.assertEqual(terminal.seq, self.cursor_on_disk(launch))

        with mock.patch.object(
            supervision_liveness, "probe", return_value=supervision_liveness.DEAD
        ) as probe:
            code, recovered = self.payload_from(
                mailbox_cli_outbox.outbox_supervise_from_args,
                self.args(timeout=0, supervision_id=launch),
            )

        self.assertEqual(0, code)
        self.assertEqual("DONE", recovered["state"])
        self.assertIsNone(recovered["lost_reason"])
        self.assertEqual(
            [terminal.id], [item["id"] for item in recovered["supervision_ends"]]
        )
        probe.assert_not_called()

    def test_terminal_replay_is_immediate_and_scoped_to_its_launch(self) -> None:
        launch = mailbox_validation.new_supervision_id()
        own = mailbox_appenders.append_supervision_end(
            self.plan_dir,
            "helper exited: complete",
            outcome=mailbox_vocabulary.SUPERVISION_OUTCOME_COMPLETE,
            state_dir=self.state_dir,
            supervision_id=launch,
        )
        foreign = mailbox_appenders.append_supervision_end(
            self.plan_dir,
            "helper exited: stopped",
            outcome=mailbox_vocabulary.SUPERVISION_OUTCOME_STOPPED,
            state_dir=self.state_dir,
            supervision_id=mailbox_validation.new_supervision_id(),
        )
        mailbox_cursors.write_supervision_cursor(
            self.plan_dir, launch, foreign.seq, state_dir=self.state_dir
        )
        with mock.patch.object(time, "sleep", side_effect=AssertionError("slept")):
            code, replay = self.payload_from(
                mailbox_cli_outbox.outbox_supervise_from_args,
                self.args(timeout=550, supervision_id=launch),
            )
        self.assertEqual(0, code)
        self.assertEqual("DONE", replay["state"])
        self.assertEqual([own.id], [item["id"] for item in replay["supervision_ends"]])
        self.assertEqual(foreign.seq, replay["next_since"])

        # The general-purpose outbox reader still honors its physical cursor.
        code, ordinary = self.payload_from(
            mailbox_cli_outbox.outbox_wait_from_args,
            self.args(
                timeout=0,
                since=foreign.seq,
                include=mailbox_cli_constants.OUTBOX_INCLUDE_ALL,
                supervision_id=launch,
            ),
        )
        self.assertEqual(0, code)
        self.assertTrue(ordinary["timed_out"])
        self.assertEqual([], ordinary["supervision_ends"])

    def test_dead_helper_requires_a_successful_final_mailbox_read(self) -> None:
        empty = mailbox_snapshot.MailboxView(
            mailbox_path=mailbox_cursors.mailbox_path_for_plan(
                self.plan_dir, self.state_dir
            )
        )
        error = mailbox_models.MailboxError(
            mailbox_constants.ERROR_MAILBOX_READ,
            "temporarily unavailable",
            path=empty.mailbox_path,
        )
        # Even a successful read before the probe cannot rule out a terminal
        # record published while the helper exits.
        for first_read in (empty, error):
            with (
                self.subTest(initial_read_succeeded=first_read is empty),
                mock.patch.object(
                    mailbox, "read_mailbox", side_effect=[first_read, error]
                ),
                mock.patch.object(mailbox_cli_status, "write_supervisor_lease"),
            ):
                with self.assertRaises(mailbox_models.MailboxError) as raised:
                    mailbox_cli_waits.wait_for_questions(
                        self.plan_dir,
                        state_dir=self.state_dir,
                        since=0,
                        timeout=0,
                        supervisor_id=str(uuid.uuid4()),
                        include=mailbox_cli_constants.OUTBOX_INCLUDE_SUPERVISION,
                        liveness_probe=lambda: (
                            mailbox_cli_constants.SUPERVISE_LOST_HELPER_EXITED
                        ),
                    )
                self.assertIs(error, raised.exception)

    def test_final_mailbox_read_retries_before_declaring_helper_lost(self) -> None:
        empty = mailbox.read_mailbox(self.plan_dir, state_dir=self.state_dir)
        terminal = mailbox_appenders.append_supervision_end(
            self.plan_dir,
            "helper exited: complete",
            outcome=mailbox_vocabulary.SUPERVISION_OUTCOME_COMPLETE,
            state_dir=self.state_dir,
            supervision_id=mailbox_validation.new_supervision_id(),
        )
        finished = mailbox.read_mailbox(self.plan_dir, state_dir=self.state_dir)
        error = mailbox_models.MailboxError(
            mailbox_constants.ERROR_MAILBOX_READ,
            "temporarily unavailable",
            path=empty.mailbox_path,
        )
        probe = mock.Mock(
            return_value=mailbox_cli_constants.SUPERVISE_LOST_HELPER_EXITED
        )
        with (
            mock.patch.object(
                mailbox, "read_mailbox", side_effect=[empty, error, finished]
            ) as read,
            mock.patch.object(mailbox_cli_status, "write_supervisor_lease"),
            mock.patch.object(time, "monotonic", return_value=0.0),
            mock.patch.object(time, "sleep") as sleep,
        ):
            result = mailbox_cli_waits.wait_for_questions(
                self.plan_dir,
                state_dir=self.state_dir,
                since=0,
                timeout=10,
                supervisor_id=str(uuid.uuid4()),
                include=mailbox_cli_constants.OUTBOX_INCLUDE_SUPERVISION,
                liveness_probe=probe,
            )

        self.assertEqual((terminal,), result.supervision_ends)
        self.assertIsNone(result.lost_reason)
        self.assertEqual(3, read.call_count)
        probe.assert_called_once()
        sleep.assert_called_once()

    def test_terminal_record_published_during_dead_probe_wins(self) -> None:
        launch = mailbox_validation.new_supervision_id()
        self.write_liveness_record(launch)

        def finish_during_probe(*args, **kwargs):
            mailbox_appenders.append_supervision_end(
                self.plan_dir,
                "helper exited: complete",
                outcome=mailbox_vocabulary.SUPERVISION_OUTCOME_COMPLETE,
                state_dir=self.state_dir,
                supervision_id=launch,
            )
            return supervision_liveness.DEAD

        with mock.patch.object(
            supervision_liveness, "probe", side_effect=finish_during_probe
        ) as probe:
            code, payload = self.payload_from(
                mailbox_cli_outbox.outbox_supervise_from_args,
                self.args(timeout=0, supervision_id=launch),
            )

        self.assertEqual(0, code)
        self.assertEqual(mailbox_cli_constants.SUPERVISE_TOKEN_DONE, payload["state"])
        self.assertTrue(payload["supervision_ended"])
        self.assertIsNone(payload["lost_reason"])
        probe.assert_called_once()

    def test_carried_open_question_wins_over_dead_helper(self) -> None:
        launch = mailbox_validation.new_supervision_id()
        self.write_active_turn(run_id="run-live", session_id="5", turn_index=2)
        self.write_liveness_record(launch)
        question = mailbox_appenders.append_question(
            self.plan_dir,
            "Which shape?",
            state_dir=self.state_dir,
            run_id="run-live",
        )
        code, first = self.payload_from(
            mailbox_cli_outbox.outbox_supervise_from_args,
            self.args(timeout=0, supervision_id=launch),
        )
        self.assertEqual(0, code)
        self.assertEqual(mailbox_cli_constants.SUPERVISE_TOKEN_ACTION, first["state"])

        with mock.patch.object(
            supervision_liveness, "probe", return_value=supervision_liveness.DEAD
        ):
            code, repeated = self.payload_from(
                mailbox_cli_outbox.outbox_supervise_from_args,
                self.args(timeout=0, supervision_id=launch),
            )

        self.assertEqual(0, code)
        self.assertEqual(
            mailbox_cli_constants.SUPERVISE_TOKEN_ACTION, repeated["state"]
        )
        self.assertEqual([question.id], [item["id"] for item in repeated["questions"]])

        def reply_during_probe(*args, **kwargs):
            mailbox_appenders.append_reply(
                self.plan_dir,
                question.id,
                "Use the first shape",
                state_dir=self.state_dir,
            )
            return supervision_liveness.DEAD

        with mock.patch.object(
            supervision_liveness, "probe", side_effect=reply_during_probe
        ):
            code, closed = self.payload_from(
                mailbox_cli_outbox.outbox_supervise_from_args,
                self.args(timeout=0, supervision_id=launch),
            )

        self.assertEqual(0, code)
        self.assertEqual(mailbox_cli_constants.SUPERVISE_TOKEN_LOST, closed["state"])
        self.assertEqual([], closed["questions"])
