"""MailboxCliTestsPart9: probes."""

from __future__ import annotations

from .asl_control_mailbox_cli_supervise_liveness_support import (
    UTC,
    MailboxCliTestsPart9Base,
    datetime,
    mailbox_appenders,
    mailbox_cli_constants,
    mailbox_cli_outbox,
    mailbox_cli_output,
    mailbox_cli_status,
    mailbox_cursors,
    mailbox_validation,
    mailbox_vocabulary,
    mock,
    os,
    supervision_liveness,
    timedelta,
)


class MailboxCliTestsPart9(MailboxCliTestsPart9Base):
    def test_outbox_supervise_lost_payload_carries_run_evidence(self) -> None:
        launch = mailbox_validation.new_supervision_id()
        self.write_liveness_record(launch)
        self.write_active_turn(run_id="run-live", session_id="1", turn_index=2)

        with mock.patch.object(
            supervision_liveness,
            "probe",
            return_value=supervision_liveness.DEAD,
        ):
            code, payload = self.payload_from(
                mailbox_cli_outbox.outbox_supervise_from_args,
                self.args(timeout=0, supervision_id=launch),
            )

        self.assertEqual(0, code)
        self.assertEqual(mailbox_cli_constants.SUPERVISE_TOKEN_LOST, payload["state"])
        self.assertEqual(
            mailbox_cli_constants.SUPERVISE_LOST_HELPER_EXITED,
            payload["lost_reason"],
        )
        self.assertFalse(payload["timed_out"])
        self.assertFalse(payload["supervision_ended"])
        evidence = payload["run_evidence"]
        self.assertTrue(evidence["lock_active"])
        self.assertEqual(os.getpid(), evidence["lock_pid"])
        self.assertEqual("run-live", evidence["run_id"])
        self.assertEqual(
            mailbox_cli_constants.RUNNER_STATE_FRESH, evidence["runner_state"]
        )
        self.assertTrue(evidence["runner_state_fresh"])
        self.assertEqual("1", evidence["active_turn"]["session_id"])
        self.assertEqual(0, evidence["sessions_complete"])
        self.assertEqual(1, evidence["sessions_total"])

    def test_lost_evidence_names_why_the_runner_state_is_not_fresh(self) -> None:
        cases = (
            ({}, mailbox_cli_constants.RUNNER_STATE_ABSENT, "runner state absent,"),
            (
                {"present": True, "error": "not valid JSON"},
                mailbox_cli_constants.RUNNER_STATE_UNREADABLE,
                "runner state unreadable,",
            ),
            (
                {"present": True, "run_finished_at": "2026-08-10T12:00:00+00:00"},
                mailbox_cli_constants.RUNNER_STATE_FINISHED,
                "runner state finished,",
            ),
            (
                {"present": True, "stale_unfinished": True},
                mailbox_cli_constants.RUNNER_STATE_STALE,
                "runner state stale (unfinished, its runner is gone),",
            ),
            (
                {"present": True},
                mailbox_cli_constants.RUNNER_STATE_FRESH,
                "runner state fresh,",
            ),
        )
        for runner_state, condition, wording in cases:
            with self.subTest(condition=condition):
                with mock.patch.object(
                    mailbox_cli_status.state_status,
                    "status_payload",
                    return_value={"runner_state": runner_state},
                ):
                    evidence = mailbox_cli_status.supervision_run_evidence(
                        self.plan_dir, self.state_dir
                    )
                assert evidence is not None
                self.assertEqual(condition, evidence["runner_state"])
                lines = mailbox_cli_output.outbox_lost_text_lines(
                    self.plan_dir,
                    mailbox_cli_constants.SUPERVISE_LOST_HELPER_EXITED,
                    evidence,
                )
                self.assertIn(wording, lines[1])

    def test_outbox_supervise_reports_a_helper_that_never_started(self) -> None:
        launch = mailbox_validation.new_supervision_id()
        self.age_grace_anchor(
            launch,
            mailbox_cli_constants.SUPERVISE_LIVENESS_GRACE_SECONDS + 60,
        )

        code, lines = self.supervise_lines(supervision_id=launch)

        self.assertEqual(0, code)
        self.assertEqual(
            f"LOST reason={mailbox_cli_constants.SUPERVISE_LOST_HELPER_NEVER_STARTED}",
            lines[0],
        )
        self.assertIn("Active turn: none.", lines)

    def test_outbox_supervise_stays_idle_inside_the_liveness_grace(self) -> None:
        launch = mailbox_validation.new_supervision_id()

        code, lines = self.supervise_lines(supervision_id=launch)

        self.assertEqual(0, code)
        self.assertEqual(["IDLE"], lines)
        # The first poll anchors the grace window so a later one can expire it.
        self.assertIsNotNone(
            mailbox_cursors.read_supervision_first_seen_at(
                self.plan_dir,
                launch,
                state_dir=self.state_dir,
            )
        )

    def test_future_grace_anchor_is_replaced_after_clock_moves_backward(self) -> None:
        launch = mailbox_validation.new_supervision_id()
        mailbox_cursors.write_supervision_cursor(
            self.plan_dir,
            launch,
            0,
            state_dir=self.state_dir,
            first_seen_at=datetime.now(UTC) + timedelta(days=1),
        )

        code, lines = self.supervise_lines(supervision_id=launch)

        self.assertEqual(0, code)
        self.assertEqual(["IDLE"], lines)
        anchor = mailbox_cursors.read_supervision_first_seen_at(
            self.plan_dir,
            launch,
            state_dir=self.state_dir,
        )
        self.assertIsNotNone(anchor)
        assert anchor is not None
        self.assertLessEqual(anchor, datetime.now(UTC))

        self.age_grace_anchor(
            launch,
            mailbox_cli_constants.SUPERVISE_LIVENESS_GRACE_SECONDS + 60,
        )
        code, lines = self.supervise_lines(supervision_id=launch)
        self.assertEqual(0, code)
        self.assertEqual(
            f"LOST reason={mailbox_cli_constants.SUPERVISE_LOST_HELPER_NEVER_STARTED}",
            lines[0],
        )

    def test_outbox_supervise_stays_idle_when_a_live_run_holds_the_lock(self) -> None:
        launch = mailbox_validation.new_supervision_id()
        self.write_active_turn(run_id="run-live", session_id="1", turn_index=2)
        self.age_grace_anchor(
            launch,
            mailbox_cli_constants.SUPERVISE_LIVENESS_GRACE_SECONDS + 60,
        )

        code, lines = self.supervise_lines(supervision_id=launch)

        self.assertEqual(0, code)
        self.assertEqual(["IDLE"], lines)

    def test_outbox_supervise_stays_idle_when_the_probe_is_unverified(self) -> None:
        launch = mailbox_validation.new_supervision_id()
        self.write_liveness_record(launch)
        self.age_grace_anchor(
            launch,
            mailbox_cli_constants.SUPERVISE_LIVENESS_GRACE_SECONDS + 60,
        )

        with mock.patch.object(
            supervision_liveness,
            "probe",
            return_value=supervision_liveness.UNVERIFIED,
        ):
            code, lines = self.supervise_lines(supervision_id=launch)

        self.assertEqual(0, code)
        self.assertEqual(["IDLE"], lines)

    def test_outbox_supervise_prefers_its_terminal_record_over_a_dead_probe(
        self,
    ) -> None:
        launch = mailbox_validation.new_supervision_id()
        self.write_liveness_record(launch)
        own = mailbox_appenders.append_supervision_end(
            self.plan_dir,
            "helper exited: complete",
            outcome=mailbox_vocabulary.SUPERVISION_OUTCOME_COMPLETE,
            state_dir=self.state_dir,
            supervision_id=launch,
        )

        with mock.patch.object(
            supervision_liveness,
            "probe",
            return_value=supervision_liveness.DEAD,
        ):
            code, lines = self.supervise_lines(supervision_id=launch)

        self.assertEqual(0, code)
        self.assertEqual(
            (
                f"DONE outcome={mailbox_vocabulary.SUPERVISION_OUTCOME_COMPLETE} "
                f"seq={own.seq}"
            ),
            lines[0],
        )

    def test_outbox_supervise_prefers_an_open_question_over_a_dead_probe(self) -> None:
        launch = mailbox_validation.new_supervision_id()
        self.write_active_turn(run_id="run-live", session_id="5", turn_index=2)
        self.write_liveness_record(launch)
        mailbox_appenders.append_question(
            self.plan_dir,
            "Which shape?",
            state_dir=self.state_dir,
            run_id="run-live",
        )

        with mock.patch.object(
            supervision_liveness,
            "probe",
            return_value=supervision_liveness.DEAD,
        ):
            code, lines = self.supervise_lines(supervision_id=launch)

        self.assertEqual(0, code)
        self.assertEqual("ACTION questions=1", lines[0])
