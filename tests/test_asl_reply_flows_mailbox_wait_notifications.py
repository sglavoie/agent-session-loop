"""Mailbox wait idle accounting and lifecycle notifications."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest import mock

import tests.reply_flow_support as rfs
from agent_session_loop import (
    agent_mux_delivery,
    clock,
    events,
    harnesses,
    mailbox,
    run_coordination,
    run_persistence,
    state,
)
from tests.reply_flow_support import (
    HELPER_THREAD_JOIN_TIMEOUT_SECONDS,
    QUESTION_PUBLISH_TIMEOUT_SECONDS,
)
from tests.support import (
    init_git_repo,
    read_jsonl,
    write_plan,
)


class MailboxWaitLifecycleTests(rfs.ReplyResumeFlowCase):
    """Answered mailbox waits: notifications and idle accounting."""

    def test_answered_mailbox_wait_never_notifies_agent_mux_pause(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            init_git_repo(tmp_dir)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            write_plan(plan_dir, ("pending",))
            mailbox.append_guidance(
                plan_dir,
                "Keep working after the answer.",
                state_dir=state_dir,
            )
            mailbox.write_lease(
                plan_dir,
                "00000000-0000-0000-0000-000000000001",
                state_dir=state_dir,
            )
            calls = 0

            def fake_run_harness(name, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    last_message = (
                        "Choose option 1a or 1b.\n"
                        "RUNNER_PAUSE: blocked - need operator answer\n"
                    )
                    return harnesses.HarnessResult(
                        name=name,
                        argv=[name],
                        returncode=0,
                        stdout="waiting\n",
                        stderr="",
                        last_message=last_message,
                    )
                session_path = plan_dir / "session-1.md"
                session_path.write_text(
                    session_path.read_text(encoding="utf-8").replace(
                        "- **Status**: pending",
                        "- **Status**: complete",
                        1,
                    ),
                    encoding="utf-8",
                )
                return harnesses.HarnessResult(
                    name=name,
                    argv=[name],
                    returncode=0,
                    stdout="complete\n",
                    stderr="",
                    last_message="Runner status: complete\n",
                )

            responder_error: list[BaseException] = []

            def respond() -> None:
                try:
                    deadline = time.monotonic() + QUESTION_PUBLISH_TIMEOUT_SECONDS
                    while time.monotonic() < deadline:
                        view = mailbox.read_mailbox(plan_dir, state_dir=state_dir)
                        if view.unanswered_questions:
                            mailbox.append_reply(
                                plan_dir,
                                view.unanswered_questions[0].id,
                                "Use 1a.",
                                state_dir=state_dir,
                            )
                            return
                        time.sleep(0.01)
                    raise AssertionError("runner did not publish a question")
                except BaseException as error:
                    responder_error.append(error)

            responder = threading.Thread(target=respond, daemon=True)
            responder.start()
            args = self._build_args(
                plan_dir,
                state_dir,
                reply=None,
                cwd=tmp_dir,
            )
            env = {
                run_coordination.MAILBOX_REPLY_WAIT_BASE_ENV_VAR: "1",
                run_coordination.MAILBOX_REPLY_WAIT_UNSUPERVISED_CEILING_ENV_VAR: "2",
                run_coordination.MAILBOX_REPLY_WAIT_POLL_ENV_VAR: "0.01",
                run_coordination.MAILBOX_REPLY_WAIT_IO_GRACE_ENV_VAR: "0.05",
            }
            with (
                mock.patch.dict(os.environ, env),
                mock.patch.object(
                    harnesses,
                    "run_harness",
                    side_effect=fake_run_harness,
                ),
                mock.patch.object(
                    run_persistence, "_notify_agent_mux_loop_paused"
                ) as notify_paused,
            ):
                code, out, _err = self._run_capture(args)
            responder.join(timeout=HELPER_THREAD_JOIN_TIMEOUT_SECONDS)

            self.assertEqual([], responder_error)
            self.assertFalse(responder.is_alive())
            self.assertEqual(0, code, out)
            notify_paused.assert_not_called()
            payload = json.loads(out)
            self.assertFalse(payload["paused"])
            self.assertEqual(2, len(payload["turns"]))
            plan_state = state.plan_state_dir(plan_dir, state_dir)
            records = read_jsonl(plan_state / events.EVENTS_FILE_NAME)
            self.assertNotIn(
                events.RUN_PAUSED_EVENT,
                [record.get("name") for record in records],
            )

    def test_mailbox_wait_counts_the_human_answer_as_idle(self) -> None:
        human_answer_seconds = 0.5
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            init_git_repo(tmp_dir)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            # A second session is what proves the pin is one-shot: the turn
            # after the continuation must be back on the loop-top baseline.
            write_plan(plan_dir, ("pending", "pending"))
            mailbox.append_guidance(
                plan_dir,
                "Keep working after the answer.",
                state_dir=state_dir,
            )
            mailbox.write_lease(
                plan_dir,
                "00000000-0000-0000-0000-000000000001",
                state_dir=state_dir,
            )
            calls = 0

            def fake_run_harness(name, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    last_message = (
                        "Choose option 1a or 1b.\n"
                        "RUNNER_PAUSE: blocked - need operator answer\n"
                    )
                    return harnesses.HarnessResult(
                        name=name,
                        argv=[name],
                        returncode=0,
                        stdout="waiting\n",
                        stderr="",
                        last_message=last_message,
                    )
                session_path = plan_dir / f"session-{calls - 1}.md"
                session_path.write_text(
                    session_path.read_text(encoding="utf-8").replace(
                        "- **Status**: pending",
                        "- **Status**: complete",
                        1,
                    ),
                    encoding="utf-8",
                )
                return harnesses.HarnessResult(
                    name=name,
                    argv=[name],
                    returncode=0,
                    stdout="complete\n",
                    stderr="",
                    last_message="Runner status: complete\n",
                )

            responder_error: list[BaseException] = []

            def respond() -> None:
                try:
                    deadline = time.monotonic() + QUESTION_PUBLISH_TIMEOUT_SECONDS
                    while time.monotonic() < deadline:
                        view = mailbox.read_mailbox(plan_dir, state_dir=state_dir)
                        if view.unanswered_questions:
                            time.sleep(human_answer_seconds)
                            mailbox.append_reply(
                                plan_dir,
                                view.unanswered_questions[0].id,
                                "Use 1a.",
                                state_dir=state_dir,
                            )
                            return
                        time.sleep(0.01)
                    raise AssertionError("runner did not publish a question")
                except BaseException as error:
                    responder_error.append(error)

            responder = threading.Thread(target=respond, daemon=True)
            responder.start()
            args = self._build_args(
                plan_dir,
                state_dir,
                reply=None,
                cwd=tmp_dir,
            )
            env = {
                run_coordination.MAILBOX_REPLY_WAIT_POLL_ENV_VAR: "0.01",
                run_coordination.MAILBOX_REPLY_WAIT_IO_GRACE_ENV_VAR: "0.05",
            }
            with (
                mock.patch.dict(os.environ, env),
                mock.patch.object(
                    harnesses,
                    "run_harness",
                    side_effect=fake_run_harness,
                ),
            ):
                code, out, _err = self._run_capture(args)
            responder.join(timeout=HELPER_THREAD_JOIN_TIMEOUT_SECONDS)

            self.assertEqual([], responder_error)
            self.assertFalse(responder.is_alive())
            self.assertEqual(0, code, out)
            payload = json.loads(out)
            turns = payload["turns"]
            self.assertEqual(3, len(turns))
            # The runner has no work of its own while it waits for an answer,
            # so the continuation turn's idle clock has to start before the
            # wait and cover the whole of it.
            self.assertGreaterEqual(
                turns[1]["idle_seconds_since_previous_turn"],
                human_answer_seconds,
            )
            # The pin the wait set is spent on that one continuation. The next
            # turn is back on the loop-top refresh, so its idle clock starts
            # after the continuation turn instead of before the human answer.
            continuation_finished_at = clock.parse_datetime(turns[1]["finished_at"])
            later_started_at = clock.parse_datetime(turns[2]["started_at"])
            assert continuation_finished_at is not None
            assert later_started_at is not None
            later_idle_started_at = later_started_at - timedelta(
                seconds=turns[2]["idle_seconds_since_previous_turn"]
            )
            self.assertGreaterEqual(later_idle_started_at, continuation_finished_at)

    def test_answered_mailbox_wait_emits_wait_lifecycle_notifications(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            init_git_repo(tmp_dir)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            write_plan(plan_dir, ("pending",))
            mailbox.append_guidance(
                plan_dir,
                "Keep working after the answer.",
                state_dir=state_dir,
            )
            mailbox.write_lease(
                plan_dir,
                "00000000-0000-0000-0000-000000000003",
                state_dir=state_dir,
            )
            calls = 0

            def fake_run_harness(name, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    last_message = (
                        "Choose option 1a or 1b.\n"
                        "RUNNER_PAUSE: blocked - need operator answer\n"
                    )
                    return harnesses.HarnessResult(
                        name=name,
                        argv=[name],
                        returncode=0,
                        stdout="waiting\n",
                        stderr="",
                        last_message=last_message,
                    )
                session_path = plan_dir / "session-1.md"
                session_path.write_text(
                    session_path.read_text(encoding="utf-8").replace(
                        "- **Status**: pending",
                        "- **Status**: complete",
                        1,
                    ),
                    encoding="utf-8",
                )
                return harnesses.HarnessResult(
                    name=name,
                    argv=[name],
                    returncode=0,
                    stdout="complete\n",
                    stderr="",
                    last_message="Runner status: complete\n",
                )

            responder_error: list[BaseException] = []

            def respond() -> None:
                try:
                    deadline = time.monotonic() + QUESTION_PUBLISH_TIMEOUT_SECONDS
                    while time.monotonic() < deadline:
                        view = mailbox.read_mailbox(plan_dir, state_dir=state_dir)
                        if view.unanswered_questions:
                            mailbox.append_reply(
                                plan_dir,
                                view.unanswered_questions[0].id,
                                "Use 1a.",
                                state_dir=state_dir,
                            )
                            return
                        time.sleep(0.01)
                    raise AssertionError("runner did not publish a question")
                except BaseException as error:
                    responder_error.append(error)

            responder = threading.Thread(target=respond, daemon=True)
            responder.start()
            args = self._build_args(
                plan_dir,
                state_dir,
                reply=None,
                cwd=tmp_dir,
            )
            env = {
                run_coordination.MAILBOX_REPLY_WAIT_BASE_ENV_VAR: "1",
                run_coordination.MAILBOX_REPLY_WAIT_UNSUPERVISED_CEILING_ENV_VAR: "2",
                run_coordination.MAILBOX_REPLY_WAIT_POLL_ENV_VAR: "0.01",
                run_coordination.MAILBOX_REPLY_WAIT_IO_GRACE_ENV_VAR: "0.05",
            }
            notified: list[tuple[str, dict[str, Any] | None]] = []

            def record_notification(event_name, _payload, **kwargs):
                notified.append((event_name, kwargs.get("extra")))

            with (
                mock.patch.dict(os.environ, env),
                mock.patch.object(
                    harnesses,
                    "run_harness",
                    side_effect=fake_run_harness,
                ),
                # The wait and turn notifications now reach AgentMux from two
                # modules, so the shared delivery entry point is the only place
                # that sees the whole lifecycle in order.
                mock.patch.object(
                    agent_mux_delivery,
                    "notify_loop",
                    side_effect=record_notification,
                ),
            ):
                code, out, _err = self._run_capture(args)
            responder.join(timeout=HELPER_THREAD_JOIN_TIMEOUT_SECONDS)

            self.assertEqual([], responder_error)
            self.assertEqual(0, code, out)
            emitted = [event_name for event_name, _extra in notified]
            self.assertEqual(
                1, emitted.count(agent_mux_delivery.WAIT_START_EVENT), emitted
            )
            self.assertEqual(
                1, emitted.count(agent_mux_delivery.WAIT_END_EVENT), emitted
            )
            start_index = emitted.index(agent_mux_delivery.WAIT_START_EVENT)
            end_index = emitted.index(agent_mux_delivery.WAIT_END_EVENT)
            self.assertLess(start_index, end_index, emitted)
            # The asking turn is never committed, so its wait is bracketed by
            # the turn start alone; the only turn finish belongs to the turn
            # that resumes after the answer.
            self.assertLess(
                emitted.index(agent_mux_delivery.TURN_START_EVENT),
                start_index,
                emitted,
            )
            self.assertEqual(
                1, emitted.count(agent_mux_delivery.TURN_FINISH_EVENT), emitted
            )
            self.assertLess(
                end_index,
                emitted.index(agent_mux_delivery.TURN_FINISH_EVENT),
                emitted,
            )
            self.assertNotIn(agent_mux_delivery.PAUSE_EVENT, emitted)

            view = mailbox.read_mailbox(plan_dir, state_dir=state_dir)
            questions = [
                message
                for message in view.messages
                if message.kind == mailbox.KIND_QUESTION
            ]
            self.assertEqual(1, len(questions))
            question = questions[0]
            self.assertEqual(
                {
                    "question_id": question.id,
                    "question_preview": run_persistence._question_preview(
                        question.text
                    ),
                },
                notified[start_index][1],
            )
            self.assertEqual(question.id, notified[end_index][1]["question_id"])
            self.assertEqual("reply", notified[end_index][1]["wait_reason"])
            self.assertEqual("consumed", notified[end_index][1]["delivery_state"])
            self.assertTrue(notified[end_index][1]["closure_consumed"])
            reply = view.reply_for(question.id)
            assert reply is not None
            self.assertEqual(reply.id, notified[end_index][1]["closure_id"])


if __name__ == "__main__":
    unittest.main()
