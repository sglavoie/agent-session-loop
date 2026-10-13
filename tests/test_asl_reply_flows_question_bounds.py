"""Bounded question text and mailbox capacity tests."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tests.reply_flow_support as rfs
from agent_session_loop import (
    events,
    harnesses,
    mailbox,
    mailbox_appenders,
    mailbox_constants,
    mailbox_cursors,
    mailbox_snapshot,
    plans_snapshot,
    run_coordination,
    state,
    state_status,
)
from tests.support import (
    _write_lock_file,
    _write_state_payload,
    init_git_repo,
    read_jsonl,
    write_plan,
)


class QuestionBoundsFlowTests(rfs.ReplyResumeFlowCase):
    """Oversized last messages and mailbox capacity degradation."""

    def test_oversized_last_message_yields_bounded_retry_stable_question(
        self,
    ) -> None:
        cap = run_coordination.MAILBOX_QUESTION_TEXT_MAX_CHARS
        oversized = "".join(f"line {index}\n" for index in range(cap // 4))
        self.assertGreater(len(oversized), cap)
        artifact_path = Path("/tmp/artifacts/last-message.txt")

        # Two independent preparations of the same turn read the same
        # artifact; their bodies must be byte-identical or the reuse
        # equality check in _ensure_mailbox_question degrades the retry.
        first_text = run_coordination.mailbox_question_text(
            self._question_context(oversized, artifact_path)
        )
        retried_text = run_coordination.mailbox_question_text(
            self._question_context(oversized, artifact_path)
        )
        self.assertEqual(first_text.encode("utf-8"), retried_text.encode("utf-8"))
        self.assertTrue(first_text.startswith(oversized[:cap]))
        self.assertLess(len(first_text), cap + 200)
        footer = first_text[cap:]
        self.assertIn(str(artifact_path), footer)
        self.assertIn(str(cap), footer)
        self.assertIn(str(len(oversized)), footer)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            write_plan(plan_dir, ("pending",))
            question_id = run_coordination.mailbox_question_id(
                plan_dir=plan_dir,
                run_id="run-bounded",
                session_id="1",
                turn_index=2,
            )
            published = run_coordination.ensure_mailbox_question(
                plan_dir=plan_dir,
                state_dir_arg=str(state_dir),
                question_id=question_id,
                text=first_text,
                run_id="run-bounded",
                session_id="1",
                turn_index=2,
            )
            self.assertEqual(first_text, published.text)

            with mock.patch.object(
                mailbox_appenders,
                "append_question",
                side_effect=AssertionError(
                    "a retried preparation must reuse the bounded question"
                ),
            ) as append_question:
                reused = run_coordination.ensure_mailbox_question(
                    plan_dir=plan_dir,
                    state_dir_arg=str(state_dir),
                    question_id=question_id,
                    text=retried_text,
                    run_id="run-bounded",
                    session_id="1",
                    turn_index=2,
                )

            self.assertEqual(published, reused)
            append_question.assert_not_called()

    def test_last_message_at_exact_boundary_is_not_truncated(self) -> None:
        cap = run_coordination.MAILBOX_QUESTION_TEXT_MAX_CHARS
        artifact_path = Path("/tmp/artifacts/last-message.txt")
        at_boundary = "b" * cap
        self.assertEqual(
            at_boundary,
            run_coordination.mailbox_question_text(
                self._question_context(at_boundary, artifact_path)
            ),
        )

        over_boundary = "b" * (cap + 1)
        bounded = run_coordination.mailbox_question_text(
            self._question_context(over_boundary, artifact_path)
        )
        self.assertNotEqual(over_boundary, bounded)
        self.assertTrue(bounded.startswith(at_boundary))
        self.assertIn(str(artifact_path), bounded[cap:])

    def test_question_cap_degrades_to_mailbox_full_without_appending(
        self,
    ) -> None:
        past_question_cap = mailbox_snapshot.MailboxView(
            mailbox_path=Path("/tmp/mailbox.jsonl"),
            messages=(),
            last_seq=0,
            size_bytes=(
                mailbox_constants.MAILBOX_MAX_BYTES
                - mailbox_constants.MAILBOX_QUESTION_RESERVE_BYTES
            ),
        )
        # The guard must fire on the reduced question cap even though the
        # file cap still has the whole reply reserve to spare.
        self.assertFalse(past_question_cap.full)
        self.assertFalse(past_question_cap.accepts_questions)

        with (
            mock.patch.object(
                mailbox,
                "read_mailbox",
                return_value=past_question_cap,
            ),
            mock.patch.object(
                mailbox_appenders,
                "append_question",
            ) as append_question,
            self.assertRaises(run_coordination.MailboxWaitUnavailable) as raised,
        ):
            run_coordination.ensure_mailbox_question(
                plan_dir=Path("/tmp/plan"),
                state_dir_arg=None,
                question_id="00000000-0000-4000-8000-000000000042",
                text="Which option?",
                run_id="RUN-TEST",
                session_id="1",
                turn_index=1,
            )

        self.assertEqual(mailbox_constants.ERROR_MAILBOX_FULL, raised.exception.reason)
        append_question.assert_not_called()

    def test_abandoned_escalation_stops_counting_once_the_run_pauses(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            write_plan(plan_dir, ("pending",))
            run_id = "run-that-times-out"
            question_id = run_coordination.mailbox_question_id(
                plan_dir=plan_dir,
                run_id=run_id,
                session_id="1",
                turn_index=2,
            )

            question = run_coordination.ensure_mailbox_question(
                plan_dir=plan_dir,
                state_dir_arg=str(state_dir),
                question_id=question_id,
                text="RUNNER_PAUSE: wrangler auth expired",
                run_id=run_id,
                session_id="1",
                turn_index=2,
            )

            self.assertEqual(run_id, question.run_id)
            live_view = mailbox.read_mailbox(plan_dir, state_dir=state_dir)
            self.assertEqual((question,), live_view.open_questions(run_id))

            # The wait times out, the run pauses, and nothing ever answers.
            _write_state_payload(
                plan_dir,
                state_dir,
                {
                    "run_id": run_id,
                    "ok": False,
                    "paused": True,
                    "reason": "blocked",
                    "run_finished_at": "2026-07-24T01:45:51+00:00",
                },
            )
            _write_lock_file(plan_dir, state_dir, pid=os.getpid())

            payload = state_status.status_payload(
                plans_snapshot.inspect_plan(plan_dir), state_dir
            )

            status = payload["mailbox"]
            self.assertEqual(1, status["unanswered_questions"])
            self.assertEqual(0, status["open_questions"])
            # History is preserved: the fix retires the record, never rewrites it.
            self.assertEqual(
                (question,),
                mailbox.read_mailbox(
                    plan_dir, state_dir=state_dir
                ).unanswered_questions,
            )

    def test_full_malformed_mailbox_falls_back_without_a_live_wait(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            init_git_repo(tmp_dir)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            write_plan(plan_dir, ("pending",))
            mailbox_path = mailbox_cursors.mailbox_path_for_plan(plan_dir, state_dir)
            mailbox_path.parent.mkdir(parents=True, exist_ok=True)
            mailbox_path.write_bytes(b"{" * mailbox_constants.MAILBOX_MAX_BYTES)
            pause_message = "blocked - mailbox capacity fallback"

            def fake_run_harness(name, **_kwargs):
                return harnesses.HarnessResult(
                    name=name,
                    argv=[name],
                    returncode=0,
                    stdout="waiting\n",
                    stderr="",
                    last_message=(
                        f"Choose option 1a or 1b.\nRUNNER_PAUSE: {pause_message}\n"
                    ),
                )

            args = self._build_args(
                plan_dir,
                state_dir,
                reply=None,
                cwd=tmp_dir,
            )
            with mock.patch.object(
                harnesses,
                "run_harness",
                side_effect=fake_run_harness,
            ):
                code, out, _err = self._run_capture(args)

            self.assertEqual(1, code, out)
            payload = json.loads(out)
            self.assertTrue(payload["mailbox_enabled"])
            self.assertTrue(payload["paused"])
            self.assertEqual("blocked", payload["pause_reason"])
            self.assertNotIn("waiting_for_reply", payload)

            records = read_jsonl(
                state.plan_state_dir(plan_dir, state_dir) / events.EVENTS_FILE_NAME
            )
            self.assertEqual(
                [],
                [
                    record
                    for record in records
                    if record.get("name") == events.MAILBOX_EVENT
                    and record.get("payload", {}).get("action")
                    == events.MAILBOX_ACTION_WAIT_STARTED
                ],
            )
            self.assertEqual(
                1,
                sum(
                    record.get("name") == events.RUN_PAUSED_EVENT for record in records
                ),
            )
            self.assertEqual(
                mailbox_constants.MAILBOX_MAX_BYTES,
                mailbox_path.stat().st_size,
            )


if __name__ == "__main__":
    unittest.main()
