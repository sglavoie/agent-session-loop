"""MailboxCliTestsPart8.

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
    mailbox_appenders,
    mailbox_cli_constants,
    mailbox_cli_models,
    mailbox_cli_outbox,
    mailbox_cli_waits,
    mailbox_constants,
    mailbox_cursors,
    mailbox_models,
    mailbox_snapshot,
    mailbox_validation,
    mailbox_vocabulary,
    state,
    supervision_liveness,
)
from tests.asl_control_mailbox_cli_support import MailboxCliTestsBase


class MailboxCliTestsPart8(MailboxCliTestsBase):
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

        code, second = self.supervise_lines(supervision_id=launch)
        self.assertEqual(0, code)
        self.assertEqual(["IDLE"], second)

        # A different launch keeps its own cursor and still sees the question.
        code, other = self.supervise_lines(
            supervision_id=mailbox_validation.new_supervision_id()
        )
        self.assertEqual(0, code)
        self.assertEqual("ACTION questions=1", other[0])

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

    def test_outbox_supervise_reports_its_own_terminal_record(self) -> None:
        launch = mailbox_validation.new_supervision_id()
        mailbox_appenders.append_supervision_end(
            self.plan_dir,
            "helper exited: stopped",
            outcome=mailbox_vocabulary.SUPERVISION_OUTCOME_STOPPED,
            state_dir=self.state_dir,
            supervision_id=mailbox_validation.new_supervision_id(),
        )
        own = mailbox_appenders.append_supervision_end(
            self.plan_dir,
            "helper exited: complete",
            outcome=mailbox_vocabulary.SUPERVISION_OUTCOME_COMPLETE,
            state_dir=self.state_dir,
            supervision_id=launch,
        )

        code, lines = self.supervise_lines(supervision_id=launch)

        self.assertEqual(0, code)
        self.assertEqual(
            (
                f"DONE outcome={mailbox_vocabulary.SUPERVISION_OUTCOME_COMPLETE} "
                f"seq={own.seq}"
            ),
            lines[0],
        )
        self.assertIn(f"Supervision end {own.id} (seq {own.seq}):", lines)

        code, payload = self.payload_from(
            mailbox_cli_outbox.outbox_supervise_from_args,
            self.args(timeout=0, supervision_id=launch),
        )
        self.assertEqual(0, code)
        self.assertEqual("outbox-supervise", payload["command"])
        self.assertEqual("IDLE", payload["state"])
        self.assertTrue(payload["timed_out"])
        self.assertFalse(payload["supervision_ended"])
        self.assertEqual([], payload["questions"])
        self.assertEqual([], payload["notices"])
        self.assertEqual("supervision", payload["include"])
        self.assertEqual(own.seq, payload["since"])

    def test_outbox_supervise_self_heals_an_unusable_cursor(self) -> None:
        question = mailbox_appenders.append_question(
            self.plan_dir,
            "Which shape?",
            state_dir=self.state_dir,
            run_id="run-live",
        )
        launch = mailbox_validation.new_supervision_id()
        cursor_path = mailbox_cursors.supervision_cursor_path_for_plan(
            self.plan_dir,
            launch,
            self.state_dir,
        )
        cursor_path.parent.mkdir(parents=True, exist_ok=True)

        for content in (
            "{not json",
            json.dumps({"version": 99, "next_since": 5}),
            json.dumps(
                {
                    "version": mailbox_constants.MAILBOX_SUPERVISION_CURSOR_VERSION,
                    "supervision_id": mailbox_validation.new_supervision_id(),
                    "next_since": 5,
                }
            ),
        ):
            with self.subTest(content=content):
                cursor_path.write_text(content, encoding="utf-8")
                self.assertEqual(0, self.cursor_on_disk(launch))
                code, lines = self.supervise_lines(supervision_id=launch)
                self.assertEqual(0, code)
                self.assertEqual("ACTION questions=1", lines[0])
                self.assertEqual(question.seq, self.cursor_on_disk(launch))

    def test_outbox_supervise_keeps_one_cursor_file_per_launch(self) -> None:
        first = mailbox_validation.new_supervision_id()
        second = mailbox_validation.new_supervision_id()

        for launch in (first, second):
            code, _lines = self.supervise_lines(supervision_id=launch)
            self.assertEqual(0, code)

        state_dir = state.plan_state_dir(self.plan_dir, self.state_dir)
        self.assertEqual(
            sorted(
                mailbox_cursors.supervision_cursor_path_for_plan(
                    self.plan_dir,
                    launch,
                    self.state_dir,
                ).name
                for launch in (first, second)
            ),
            sorted(
                path.name
                for path in state_dir.glob(
                    mailbox_constants.MAILBOX_SUPERVISION_CURSOR_GLOB
                )
            ),
        )

    def test_outbox_supervise_prunes_cursors_abandoned_long_ago(self) -> None:
        abandoned = mailbox_validation.new_supervision_id()
        mailbox_cursors.write_supervision_cursor(
            self.plan_dir,
            abandoned,
            3,
            state_dir=self.state_dir,
        )
        abandoned_path = mailbox_cursors.supervision_cursor_path_for_plan(
            self.plan_dir,
            abandoned,
            self.state_dir,
        )
        stale = time.time() - (
            mailbox_constants.MAILBOX_SUPERVISION_CURSOR_MAX_AGE_SECONDS + 60
        )
        os.utime(abandoned_path, (stale, stale))

        code, _lines = self.supervise_lines(
            supervision_id=mailbox_validation.new_supervision_id()
        )

        self.assertEqual(0, code)
        self.assertFalse(abandoned_path.exists())

    def test_outbox_supervise_reenters_the_wait_on_the_remaining_budget(
        self,
    ) -> None:
        mailbox_path = mailbox_cursors.mailbox_path_for_plan(
            self.plan_dir, self.state_dir
        )
        foreign_end = mailbox_models.MailboxMessage(
            seq=1,
            id=str(uuid.uuid4()),
            kind=mailbox_vocabulary.KIND_SUPERVISION_END,
            created_at="2026-08-10T12:00:00+00:00",
            text="helper exited: complete",
            supervision_outcome=mailbox_vocabulary.SUPERVISION_OUTCOME_COMPLETE,
            supervision_id=mailbox_validation.new_supervision_id(),
        )
        question = mailbox_models.MailboxMessage(
            seq=2,
            id=str(uuid.uuid4()),
            kind=mailbox_vocabulary.KIND_QUESTION,
            created_at="2026-08-10T12:00:05+00:00",
            text="Which shape?",
            run_id="run-live",
        )

        def woke(
            messages: tuple[mailbox_models.MailboxMessage, ...],
        ) -> mailbox_cli_models.OutboxWaitResult:
            return mailbox_cli_models.OutboxWaitResult(
                questions=tuple(
                    message
                    for message in messages
                    if message.kind == mailbox_vocabulary.KIND_QUESTION
                ),
                notices=(),
                supervision_ends=tuple(
                    message
                    for message in messages
                    if message.kind == mailbox_vocabulary.KIND_SUPERVISION_END
                ),
                include=mailbox_cli_constants.OUTBOX_INCLUDE_SUPERVISION,
                supervisor_id=str(uuid.uuid4()),
                next_since=messages[-1].seq,
                timed_out=False,
                lease_supervision=mailbox_cli_models.LeaseSupervision(),
                mailbox_view=mailbox_snapshot.MailboxView(
                    mailbox_path=mailbox_path,
                    messages=messages,
                    last_seq=messages[-1].seq,
                ),
            )

        with mock.patch.object(
            mailbox_cli_waits,
            "wait_for_questions",
            side_effect=[
                woke((foreign_end,)),
                woke((foreign_end, question)),
            ],
        ) as wait:
            code, lines = self.supervise_lines(
                timeout=60,
                supervision_id=mailbox_validation.new_supervision_id(),
            )

        self.assertEqual(0, code)
        self.assertEqual("ACTION questions=1", lines[0])
        self.assertEqual(
            [0, foreign_end.seq],
            [call.kwargs["since"] for call in wait.call_args_list],
        )
        # The second wait spends what is left of the first one's budget.
        remaining = wait.call_args_list[1].kwargs["timeout"]
        self.assertGreater(remaining, 0)
        self.assertLess(remaining, 60)

    def test_outbox_supervise_rejects_a_malformed_supervision_id(self) -> None:
        code, payload = self.payload_from(
            mailbox_cli_outbox.outbox_supervise_from_args,
            self.args(timeout=0, supervision_id="not-a-uuid"),
        )

        self.assertEqual(1, code)
        self.assertFalse(payload["ok"])
        self.assertEqual("invalid-supervision-id", payload["reason"])

    def test_supervision_cursor_preserves_its_first_seen_anchor(self) -> None:
        launch = mailbox_validation.new_supervision_id()
        anchor = datetime.now(UTC) - timedelta(seconds=300)
        mailbox_cursors.write_supervision_cursor(
            self.plan_dir,
            launch,
            1,
            state_dir=self.state_dir,
            first_seen_at=anchor,
        )

        mailbox_cursors.write_supervision_cursor(
            self.plan_dir,
            launch,
            2,
            state_dir=self.state_dir,
        )

        self.assertEqual(
            anchor,
            mailbox_cursors.read_supervision_first_seen_at(
                self.plan_dir,
                launch,
                state_dir=self.state_dir,
            ),
        )
        self.assertEqual(2, self.cursor_on_disk(launch))

    def test_outbox_supervise_reports_a_dead_helper_as_lost(self) -> None:
        launch = mailbox_validation.new_supervision_id()
        self.write_liveness_record(launch)

        with mock.patch.object(
            supervision_liveness,
            "probe",
            return_value=supervision_liveness.DEAD,
        ):
            code, lines = self.supervise_lines(supervision_id=launch)

        self.assertEqual(0, code)
        self.assertEqual(
            f"LOST reason={mailbox_cli_constants.SUPERVISE_LOST_HELPER_EXITED}",
            lines[0],
        )
        self.assertIn("that process is gone", lines[1])
        self.assertIn("Run evidence: lock not active, run_id none, ", lines[2])
        self.assertIn("sessions 0/1 complete.", lines[2])
        self.assertEqual(
            f"Next step: read `asl status {self.plan_dir.resolve()} --json`.",
            lines[-1],
        )


if __name__ == "__main__":
    unittest.main()
