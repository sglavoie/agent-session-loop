"""MailboxCliTestsPart9: waits."""

from __future__ import annotations

from .asl_control_mailbox_cli_supervise_liveness_support import (
    MailboxCliTestsPart9Base,
    agent_mux_delivery,
    io,
    json,
    mailbox,
    mailbox_appenders,
    mailbox_cli_constants,
    mailbox_cli_outbox,
    mailbox_cli_output,
    mailbox_cli_status,
    mailbox_cli_waits,
    mailbox_constants,
    mailbox_cursors,
    mailbox_models,
    mailbox_rendering,
    mailbox_snapshot,
    mailbox_vocabulary,
    mock,
    redirect_stdout,
    time,
    uuid,
)


class MailboxCliTestsPart9(MailboxCliTestsPart9Base):
    def test_outbox_wait_all_wakes_on_a_supervision_end_mid_wait(self) -> None:
        mailbox_path = mailbox_cursors.mailbox_path_for_plan(
            self.plan_dir, self.state_dir
        )
        empty = mailbox_snapshot.MailboxView(mailbox_path=mailbox_path)
        record = mailbox_models.MailboxMessage(
            seq=1,
            id=str(uuid.uuid4()),
            kind=mailbox_vocabulary.KIND_SUPERVISION_END,
            created_at="2026-08-10T12:00:00+00:00",
            text="helper exited: complete",
            supervision_outcome=mailbox_vocabulary.SUPERVISION_OUTCOME_COMPLETE,
        )
        landed = mailbox_snapshot.MailboxView(
            mailbox_path=mailbox_path,
            messages=(record,),
            last_seq=1,
        )
        with (
            mock.patch.object(
                mailbox,
                "read_mailbox",
                side_effect=[empty, landed],
            ),
            mock.patch.object(
                mailbox,
                "mailbox_change_token",
                side_effect=[(1, 2, 0, 3), (1, 2, 1, 4)],
            ),
            mock.patch.object(
                mailbox_cli_status,
                "write_supervisor_lease",
            ),
            mock.patch.object(
                time,
                "monotonic",
                side_effect=[0.0, 5.0],
            ),
            mock.patch.object(time, "sleep") as sleep,
        ):
            result = mailbox_cli_waits.wait_for_questions(
                self.plan_dir,
                state_dir=self.state_dir,
                since=0,
                timeout=60,
                supervisor_id=str(uuid.uuid4()),
                include=mailbox_cli_constants.OUTBOX_INCLUDE_ALL,
            )

        self.assertEqual((record,), result.supervision_ends)
        self.assertEqual((), result.questions)
        self.assertFalse(result.timed_out)
        self.assertEqual(1, result.next_since)
        self.assertEqual(
            [mock.call(mailbox_cli_constants.OUTBOX_WAIT_POLL_INTERVAL_SECONDS)],
            sleep.call_args_list,
        )

    def test_supervision_end_leaves_question_and_notice_waits_unchanged(
        self,
    ) -> None:
        baselines = {}
        for include in (
            mailbox_cli_constants.OUTBOX_INCLUDE_QUESTIONS,
            mailbox_cli_constants.OUTBOX_INCLUDE_NOTICES,
        ):
            code, baselines[include] = self.payload_from(
                mailbox_cli_outbox.outbox_wait_from_args,
                self.args(timeout=0, include=include),
            )
            self.assertEqual(0, code)

        record = mailbox_appenders.append_supervision_end(
            self.plan_dir,
            "helper exited: complete",
            outcome=mailbox_vocabulary.SUPERVISION_OUTCOME_COMPLETE,
            state_dir=self.state_dir,
        )

        # The cursor advances through every physical record and the supervisor
        # id is minted per wait; nothing else may move.
        volatile = {"supervisor_id", "next_since"}
        for include, baseline in baselines.items():
            with self.subTest(include=include):
                code, payload = self.payload_from(
                    mailbox_cli_outbox.outbox_wait_from_args,
                    self.args(timeout=0, include=include),
                )
                self.assertEqual(0, code)
                self.assertTrue(payload["timed_out"])
                self.assertEqual([], payload["supervision_ends"])
                self.assertFalse(payload["supervision_ended"])
                self.assertEqual(record.seq, payload["next_since"])
                self.assertEqual(
                    {
                        key: value
                        for key, value in baseline.items()
                        if key not in volatile
                    },
                    {
                        key: value
                        for key, value in payload.items()
                        if key not in volatile
                    },
                )

    def test_supervision_end_and_question_in_one_batch_both_return(
        self,
    ) -> None:
        self.write_active_turn(run_id="run-live")
        question = mailbox_appenders.append_question(
            self.plan_dir,
            "Proceed?",
            state_dir=self.state_dir,
            run_id="run-live",
        )
        record = mailbox_appenders.append_supervision_end(
            self.plan_dir,
            "helper exited: paused",
            outcome=mailbox_vocabulary.SUPERVISION_OUTCOME_PAUSED,
            state_dir=self.state_dir,
            run_id="run-live",
        )

        with mock.patch.object(
            agent_mux_delivery,
            "notify",
            return_value={"final_status": "accepted"},
        ) as notify:
            code, payload = self.payload_from(
                mailbox_cli_outbox.outbox_wait_from_args,
                self.args(timeout=0, include=mailbox_cli_constants.OUTBOX_INCLUDE_ALL),
            )

        self.assertEqual(0, code)
        self.assertEqual(
            [question.id],
            [item["id"] for item in payload["questions"]],
        )
        self.assertEqual(
            [record.id],
            [item["id"] for item in payload["supervision_ends"]],
        )
        self.assertTrue(payload["supervision_ended"])
        self.assertEqual(record.seq, payload["next_since"])
        notify.assert_called_once()
        self.assertEqual(
            question.id,
            notify.call_args.kwargs["extra"]["question_id"],
        )
        lease = self._lease_on_disk()
        self.assertEqual("run-live", lease.run_id)
        self.assertEqual(1, lease.unanswered_questions)

    def test_terminal_only_wake_projects_nothing_and_notifies_nobody(
        self,
    ) -> None:
        self.write_active_turn(run_id="run-live")
        record = mailbox_appenders.append_supervision_end(
            self.plan_dir,
            "helper exited: complete",
            outcome=mailbox_vocabulary.SUPERVISION_OUTCOME_COMPLETE,
            state_dir=self.state_dir,
            run_id="run-live",
        )

        with mock.patch.object(agent_mux_delivery, "notify") as notify:
            code, payload = self.payload_from(
                mailbox_cli_outbox.outbox_wait_from_args,
                self.args(timeout=0, include=mailbox_cli_constants.OUTBOX_INCLUDE_ALL),
            )

        self.assertEqual(0, code)
        self.assertTrue(payload["supervision_ended"])
        self.assertEqual([], payload["questions"])
        self.assertEqual(record.seq, payload["next_since"])
        notify.assert_not_called()
        lease = self._lease_on_disk()
        self.assertIsNone(lease.run_id)
        self.assertIsNone(lease.projection_seq)
        self.assertIsNone(lease.unanswered_questions)

    def test_outbox_wait_text_renders_the_supervision_end(self) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = mailbox_cli_outbox.outbox_wait_from_args(
                self.args(
                    json=False,
                    timeout=0,
                    include=mailbox_cli_constants.OUTBOX_INCLUDE_ALL,
                )
            )
        self.assertEqual(0, code)
        self.assertIn(
            "No questions, notices, or supervision ends after seq 0",
            stdout.getvalue(),
        )

        record = mailbox_appenders.append_supervision_end(
            self.plan_dir,
            "helper exited: complete",
            outcome=mailbox_vocabulary.SUPERVISION_OUTCOME_COMPLETE,
            state_dir=self.state_dir,
        )
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = mailbox_cli_outbox.outbox_wait_from_args(
                self.args(
                    json=False,
                    timeout=0,
                    include=mailbox_cli_constants.OUTBOX_INCLUDE_ALL,
                )
            )

        self.assertEqual(0, code)
        lines = stdout.getvalue().splitlines()
        self.assertIn(
            f"Supervision end {record.id} (seq {record.seq}):",
            lines,
        )
        rendered = json.loads(
            lines[lines.index(mailbox_cli_constants.MAILBOX_MESSAGE_BEGIN) + 1]
        )
        self.assertEqual(
            {
                "id": record.id,
                "seq": record.seq,
                "text": "helper exited: complete",
                "outcome": mailbox_vocabulary.SUPERVISION_OUTCOME_COMPLETE,
            },
            rendered,
        )
        self.assertEqual(f"Next cursor: {record.seq}", lines[-1])

    def test_outbox_supervision_end_text_is_bounded(self) -> None:
        appended = [
            mailbox_appenders.append_supervision_end(
                self.plan_dir,
                f"{index:02d}:" + ("S" * 40000),
                outcome=mailbox_vocabulary.SUPERVISION_OUTCOME_COMPLETE,
                state_dir=self.state_dir,
            )
            for index in range(30)
        ]

        bounded_lines = mailbox_cli_output.outbox_supervision_end_text_lines(
            tuple(appended)
        )
        bounded = "\n".join(bounded_lines)
        self.assertLessEqual(
            len(bounded.encode("utf-8")),
            mailbox_rendering.MAX_RENDERED_NOTICE_BYTES,
        )
        bounded_records = [
            json.loads(bounded_lines[index + 1])
            for index, line in enumerate(bounded_lines)
            if line == mailbox_cli_constants.MAILBOX_MESSAGE_BEGIN
        ]
        self.assertEqual(
            [appended[-1].id],
            [item["id"] for item in bounded_records],
        )
        self.assertIn(
            "10 older selected supervision end records were omitted by the "
            f"{mailbox_rendering.MAX_RENDERED_NOTICES}-record cap",
            bounded,
        )
        self.assertIn(
            f"seq {appended[-2].seq} (id {appended[-2].id})",
            bounded,
        )

    def test_outbox_retries_transient_reads(self) -> None:
        question = mailbox_appenders.append_question(
            self.plan_dir,
            "eventual",
            state_dir=self.state_dir,
        )
        view = mailbox.read_mailbox(
            self.plan_dir,
            state_dir=self.state_dir,
        )
        transient = mailbox_models.MailboxError(
            mailbox_constants.ERROR_MAILBOX_READ,
            "temporarily unavailable",
            path=view.mailbox_path,
        )
        with (
            mock.patch.object(
                mailbox,
                "read_mailbox",
                side_effect=[transient, view],
            ),
            mock.patch.object(time, "sleep"),
        ):
            code, payload = self.payload_from(
                mailbox_cli_outbox.outbox_wait_from_args,
                self.args(timeout=1),
            )
        self.assertEqual(0, code)
        self.assertEqual([question.id], [item["id"] for item in payload["questions"]])

    def test_outbox_reports_stable_error_when_no_read_succeeds(self) -> None:
        error = mailbox_models.MailboxError(
            mailbox_constants.ERROR_MAILBOX_READ,
            "unavailable",
            path=mailbox_cursors.mailbox_path_for_plan(self.plan_dir, self.state_dir),
        )
        with mock.patch.object(
            mailbox,
            "read_mailbox",
            side_effect=error,
        ):
            code, payload = self.payload_from(
                mailbox_cli_outbox.outbox_wait_from_args,
                self.args(timeout=0),
            )
        self.assertEqual(1, code)
        self.assertEqual("mailbox-read-error", payload["reason"])
        self.assertIn("path", payload["details"])

    def test_outbox_reports_read_error_after_an_earlier_success(self) -> None:
        empty = mailbox.read_mailbox(self.plan_dir, state_dir=self.state_dir)
        error = mailbox_models.MailboxError(
            mailbox_constants.ERROR_MAILBOX_READ,
            "unavailable after mailbox changed",
            path=empty.mailbox_path,
        )
        with (
            mock.patch.object(mailbox, "mailbox_change_token", side_effect=[1, 2]),
            mock.patch.object(mailbox, "read_mailbox", side_effect=[empty, error]),
            mock.patch.object(mailbox_cli_status, "write_supervisor_lease"),
            mock.patch.object(time, "monotonic", side_effect=[0.0, 0.0, 1.0]),
            mock.patch.object(time, "sleep"),
            self.assertRaises(mailbox_models.MailboxError) as raised,
        ):
            mailbox_cli_waits.wait_for_questions(
                self.plan_dir,
                state_dir=self.state_dir,
                since=0,
                timeout=1,
                supervisor_id=str(uuid.uuid4()),
            )
        self.assertIs(error, raised.exception)
