"""Mailbox CLI outbox supervise cursors and terminal records.

Split from tests/test_asl_control.py."""

from __future__ import annotations

import io
import json
import os
import threading
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
    mailbox_waits,
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
        self.assertEqual("DONE", payload["state"])
        self.assertFalse(payload["timed_out"])
        self.assertTrue(payload["supervision_ended"])
        self.assertEqual([own.id], [item["id"] for item in payload["supervision_ends"]])
        self.assertEqual([], payload["questions"])
        self.assertEqual([], payload["notices"])
        self.assertEqual("supervision", payload["include"])
        self.assertEqual(own.seq, payload["since"])

    def test_outbox_supervise_self_heals_an_unusable_cursor(self) -> None:
        self.write_active_turn(run_id="run-live", session_id="5", turn_index=2)
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
            # Deep enough that the decoder raises RecursionError instead.
            "[" * 1_000_000,
            json.dumps({"version": 99, "next_since": 5}),
            json.dumps(
                {
                    "version": True,
                    "supervision_id": launch,
                    "next_since": 5,
                }
            ),
            json.dumps(
                {
                    "version": mailbox_constants.MAILBOX_SUPERVISION_CURSOR_VERSION,
                    "supervision_id": mailbox_validation.new_supervision_id(),
                    "next_since": 5,
                }
            ),
        ):
            with self.subTest(content=content[:80]):
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
        self.write_active_turn(run_id="run-live", session_id="5", turn_index=2)
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

    def test_supervision_cursor_is_private_like_every_mailbox_file(self) -> None:
        launch = mailbox_validation.new_supervision_id()
        old_umask = os.umask(0o022)
        try:
            mailbox_cursors.write_supervision_cursor(
                self.plan_dir,
                launch,
                1,
                state_dir=self.state_dir,
            )
        finally:
            os.umask(old_umask)

        cursor_path = mailbox_cursors.supervision_cursor_path_for_plan(
            self.plan_dir,
            launch,
            self.state_dir,
        )
        self.assertEqual(0o600, cursor_path.stat().st_mode & 0o777)
        lock_path = (
            cursor_path.parent
            / mailbox_cursors.MAILBOX_SUPERVISION_CURSOR_LOCK_FILE_NAME
        )
        self.assertEqual(0o600, lock_path.stat().st_mode & 0o777)

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

    def test_overlapping_cursor_writers_cannot_regress_the_saved_sequence(self) -> None:
        launch = mailbox_validation.new_supervision_id()
        low_ready = threading.Event()
        release_low = threading.Event()
        high_started = threading.Event()
        high_done = threading.Event()
        failures: list[BaseException] = []
        replace_bytes = mailbox_cursors.durability.replace_bytes

        def delayed_replace(path, data, **kwargs):
            if json.loads(data)["next_since"] == 10:
                low_ready.set()
                if not release_low.wait(timeout=5):
                    raise AssertionError("low writer was never released")
            return replace_bytes(path, data, **kwargs)

        def write(sequence: int, done: threading.Event | None = None) -> None:
            try:
                if done is not None:
                    high_started.set()
                mailbox_cursors.write_supervision_cursor(
                    self.plan_dir,
                    launch,
                    sequence,
                    state_dir=self.state_dir,
                )
            except BaseException as error:
                failures.append(error)
            finally:
                if done is not None:
                    done.set()

        with mock.patch.object(
            mailbox_cursors.durability, "replace_bytes", side_effect=delayed_replace
        ):
            low = threading.Thread(target=write, args=(10,), daemon=True)
            high = threading.Thread(target=write, args=(20, high_done), daemon=True)
            try:
                low.start()
                self.assertTrue(low_ready.wait(timeout=5))
                high.start()
                self.assertTrue(high_started.wait(timeout=5))
                high_finished_before_release = high_done.wait(timeout=1)
            finally:
                release_low.set()
                low.join(timeout=5)
                if high.ident is not None:
                    high.join(timeout=5)

        self.assertFalse(low.is_alive())
        self.assertFalse(high.is_alive())
        self.assertEqual([], failures)
        self.assertEqual(20, self.cursor_on_disk(launch))
        self.assertFalse(high_finished_before_release)

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
