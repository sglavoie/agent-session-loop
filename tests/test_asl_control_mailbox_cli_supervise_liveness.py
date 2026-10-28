"""Mailbox CLI outbox supervise liveness and supervision-end wakes.

Split from tests/test_asl_control.py."""

from __future__ import annotations

import io
import json
import os
import time
import unittest
import uuid
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from unittest import mock

from agent_session_loop import (
    agent_mux_delivery,
    mailbox,
    mailbox_appenders,
    mailbox_cli_agent_mux,
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
    mailbox_validation,
    mailbox_vocabulary,
    supervision_liveness,
)
from tests.asl_control_mailbox_cli_support import MailboxCliTestsBase


class MailboxCliTestsPart9(MailboxCliTestsBase):
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


if __name__ == "__main__":
    unittest.main()
