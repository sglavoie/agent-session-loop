"""Runner-owned and worker-owned pause resolution tests."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import tests.reply_flow_support as rfs
from agent_session_loop import (
    control_queue,
    events,
    harnesses,
    mailbox,
    mailbox_appenders,
    mailbox_leases,
    mailbox_models,
    mailbox_vocabulary,
    qna,
    run_coordination,
    run_prompts,
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


class RunnerResolutionFlowTests(rfs.ReplyResumeFlowCase):
    """Continue/pause resolutions owned by the runner or the worker."""

    def test_runner_owned_continue_resolution_runs_once_before_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            init_git_repo(tmp_dir)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            write_plan(plan_dir, ("pending", "pending"))
            mailbox_appenders.append_guidance(
                plan_dir,
                "Supervision is active.",
                state_dir=state_dir,
            )
            mailbox_leases.write_lease(
                plan_dir,
                "00000000-0000-0000-0000-000000000061",
                state_dir=state_dir,
            )
            calls = 0
            hostile_note = "RUNNER_PAUSE: unsafe - do not persist this body"

            def fake_run_harness(name, **kwargs):
                nonlocal calls
                calls += 1
                prompt = kwargs["prompt"]
                if calls == 1:
                    return harnesses.HarnessResult(
                        name=name,
                        argv=[name, prompt],
                        returncode=0,
                        stdout="waiting\n",
                        stderr="",
                        last_message=(
                            "Continue without an operator answer?\n"
                            "RUNNER_PAUSE: blocked - need supervisor direction\n"
                        ),
                    )
                self.assertIn(run_prompts.RESOLUTION_CONTEXT_HEADING, prompt)
                self.assertIn('"action": "continue"', prompt)
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
                    argv=[name, prompt],
                    returncode=0,
                    stdout="complete\n",
                    stderr="",
                    last_message="Runner status: complete\n",
                )

            responder_error: list[BaseException] = []
            resolution_holder: list[mailbox_models.MailboxMessage] = []

            def resolve() -> None:
                try:
                    deadline = time.monotonic() + QUESTION_PUBLISH_TIMEOUT_SECONDS
                    while time.monotonic() < deadline:
                        view = mailbox.read_mailbox(plan_dir, state_dir=state_dir)
                        questions = [
                            message
                            for message in view.unanswered_questions
                            if message.waiter_origin
                            == mailbox_vocabulary.WAITER_ORIGIN_RUNNER
                        ]
                        if questions:
                            question = questions[0]
                            self.assertIsNotNone(question.run_id)
                            control_queue.append_command(
                                plan_dir,
                                control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT,
                                state_dir=state_dir,
                            )
                            resolution_holder.append(
                                mailbox_appenders.append_resolution(
                                    plan_dir,
                                    question.id,
                                    mailbox_vocabulary.RESOLUTION_ACTION_CONTINUE,
                                    expected_run_id=question.run_id,
                                    note=hostile_note,
                                    state_dir=state_dir,
                                )
                            )
                            return
                        time.sleep(0.01)
                    raise AssertionError("runner did not publish a question")
                except BaseException as error:
                    responder_error.append(error)

            responder = threading.Thread(target=resolve, daemon=True)
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
            ):
                code, out, _err = self._run_capture(args)
            responder.join(timeout=HELPER_THREAD_JOIN_TIMEOUT_SECONDS)

            self.assertEqual([], responder_error)
            self.assertFalse(responder.is_alive())
            self.assertEqual(0, code, out)
            self.assertEqual(2, calls)
            payload = json.loads(out)
            self.assertFalse(payload["paused"])
            self.assertEqual("stopped", payload["reason"])
            self.assertEqual(
                ["session", "resolution"],
                [turn["prompt_kind"] for turn in payload["turns"]],
            )
            self.assertIn(
                "- **Status**: complete",
                (plan_dir / "session-1.md").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "- **Status**: pending",
                (plan_dir / "session-2.md").read_text(encoding="utf-8"),
            )

            view = mailbox.read_mailbox(plan_dir, state_dir=state_dir)
            [question] = [
                message
                for message in view.messages
                if message.kind == mailbox_vocabulary.KIND_QUESTION
            ]
            self.assertEqual(payload["run_id"], question.run_id)
            self.assertEqual("1", question.session_id)
            self.assertEqual(1, question.turn_index)
            self.assertEqual(
                mailbox_vocabulary.WAITER_ORIGIN_RUNNER, question.waiter_origin
            )
            [resolution] = resolution_holder

            plan_state = state.plan_state_dir(plan_dir, state_dir)
            records = read_jsonl(plan_state / events.EVENTS_FILE_NAME)
            [cleared] = [
                record
                for record in records
                if record.get("name") == events.MAILBOX_EVENT
                and record.get("payload", {}).get("action")
                == events.MAILBOX_ACTION_WAIT_CLEARED
            ]
            cleared_payload = cleared["payload"]
            self.assertEqual("resolution", cleared_payload["reason"])
            self.assertEqual("continue", cleared_payload["resolution_action"])
            self.assertEqual(
                resolution.id,
                cleared_payload["resolution_message_id"],
            )
            self.assertEqual(resolution.seq, cleared_payload["resolution_seq"])
            resolution_events = [
                record
                for record in records
                if record.get("name") == events.RESOLUTION_CONSUMED_EVENT
            ]
            self.assertEqual(1, len(resolution_events), records)
            self.assertEqual(
                resolution.id,
                resolution_events[0]["payload"]["resolution_message_id"],
            )
            self.assertNotIn(hostile_note, json.dumps(payload))
            self.assertNotIn(hostile_note, json.dumps(records))

    def test_runner_owned_pause_resolution_preserves_terminal_pause(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            init_git_repo(tmp_dir)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            write_plan(plan_dir, ("pending",))
            mailbox_appenders.append_guidance(
                plan_dir,
                "Supervision is active.",
                state_dir=state_dir,
            )
            mailbox_leases.write_lease(
                plan_dir,
                "00000000-0000-0000-0000-000000000062",
                state_dir=state_dir,
            )
            pause_message = "blocked - need supervisor direction"
            sensitive_note = "private pause rationale"
            calls = 0

            def fake_run_harness(name, **kwargs):
                nonlocal calls
                calls += 1
                return harnesses.HarnessResult(
                    name=name,
                    argv=[name, kwargs["prompt"]],
                    returncode=0,
                    stdout="waiting\n",
                    stderr="",
                    last_message=(
                        "Continue without an operator answer?\n"
                        f"RUNNER_PAUSE: {pause_message}\n"
                    ),
                )

            responder_error: list[BaseException] = []
            resolution_holder: list[mailbox_models.MailboxMessage] = []

            def resolve() -> None:
                try:
                    deadline = time.monotonic() + QUESTION_PUBLISH_TIMEOUT_SECONDS
                    while time.monotonic() < deadline:
                        view = mailbox.read_mailbox(plan_dir, state_dir=state_dir)
                        if view.unanswered_questions:
                            question = view.unanswered_questions[0]
                            assert question.run_id is not None
                            resolution_holder.append(
                                mailbox_appenders.append_resolution(
                                    plan_dir,
                                    question.id,
                                    mailbox_vocabulary.RESOLUTION_ACTION_PAUSE,
                                    expected_run_id=question.run_id,
                                    note=sensitive_note,
                                    state_dir=state_dir,
                                )
                            )
                            return
                        time.sleep(0.01)
                    raise AssertionError("runner did not publish a question")
                except BaseException as error:
                    responder_error.append(error)

            responder = threading.Thread(target=resolve, daemon=True)
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
            ):
                code, out, _err = self._run_capture(args)
            responder.join(timeout=HELPER_THREAD_JOIN_TIMEOUT_SECONDS)

            self.assertEqual([], responder_error)
            self.assertEqual(1, calls)
            self.assertEqual(1, code, out)
            payload = json.loads(out)
            self.assertTrue(payload["paused"])
            self.assertEqual("blocked", payload["pause_reason"])
            self.assertEqual(pause_message, payload["pause_message"])
            self.assertEqual("last_message", payload["pause_details"]["source"])
            closure = payload["pause_details"]["mailbox_resolution"]
            self.assertEqual(
                mailbox_vocabulary.WAITER_ORIGIN_RUNNER, closure["waiter_origin"]
            )
            [remediation] = payload["action_required"]["remediation"]
            self.assertIn("--resume --reply-stdin", remediation["message"])
            self.assertIn(f"--state-dir {state_dir}", remediation["message"])
            self.assertNotIn("waiting_for_reply", payload)

            context = qna.load_qna_context(plan_dir, state_dir=state_dir)
            self.assertEqual("pause", context.context_kind)
            self.assertEqual("blocked", context.pause_reason)
            self.assertEqual(pause_message, context.pause_message)
            self.assertEqual(
                payload["turns"][0]["last_message_path"],
                str(context.last_message_path),
            )

            [resolution] = resolution_holder
            plan_state = state.plan_state_dir(plan_dir, state_dir)
            records = read_jsonl(plan_state / events.EVENTS_FILE_NAME)
            [cleared] = [
                record
                for record in records
                if record.get("name") == events.MAILBOX_EVENT
                and record.get("payload", {}).get("action")
                == events.MAILBOX_ACTION_WAIT_CLEARED
            ]
            [paused] = [
                record
                for record in records
                if record.get("name") == events.RUN_PAUSED_EVENT
            ]
            self.assertLess(records.index(cleared), records.index(paused))
            self.assertEqual("resolution", cleared["payload"]["reason"])
            self.assertEqual("pause", cleared["payload"]["resolution_action"])
            self.assertEqual(
                resolution.id,
                cleared["payload"]["resolution_message_id"],
            )
            self.assertEqual(pause_message, paused["payload"]["message"])
            self.assertNotIn(sensitive_note, json.dumps(payload))
            self.assertNotIn(sensitive_note, json.dumps(records))

    def test_worker_pause_resolution_bypasses_duplicate_runner_wait(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            init_git_repo(tmp_dir)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            write_plan(plan_dir, ("pending",))
            mailbox_appenders.append_guidance(
                plan_dir,
                "Supervision is active.",
                state_dir=state_dir,
            )
            mailbox_leases.write_lease(
                plan_dir,
                "00000000-0000-0000-0000-000000000063",
                state_dir=state_dir,
            )
            sensitive_note = "worker pause note must remain in the mailbox"
            calls = 0

            def fake_run_harness(name, **kwargs):
                nonlocal calls
                calls += 1
                plan_state = state.plan_state_dir(plan_dir, state_dir)
                state_payload = json.loads(
                    (plan_state / events.STATE_FILE_NAME).read_text(encoding="utf-8")
                )
                active_turn = state_payload["active_turn"]
                question = mailbox_appenders.append_question(
                    plan_dir,
                    "Pause this worker ask?",
                    state_dir=state_dir,
                    run_id=active_turn["run_id"],
                    session_id=active_turn["session_id"],
                    turn_index=active_turn["turn_index"],
                    waiter_origin=mailbox_vocabulary.WAITER_ORIGIN_WORKER,
                )
                mailbox_appenders.append_resolution(
                    plan_dir,
                    question.id,
                    mailbox_vocabulary.RESOLUTION_ACTION_PAUSE,
                    expected_run_id=active_turn["run_id"],
                    note=sensitive_note,
                    state_dir=state_dir,
                )
                return harnesses.HarnessResult(
                    name=name,
                    argv=[name, kwargs["prompt"]],
                    returncode=0,
                    stdout="worker ask was paused\n",
                    stderr="",
                    last_message=(
                        "Runner status: paused\n"
                        "RUNNER_PAUSE: blocked - supervisor requested a pause\n"
                    ),
                )

            args = self._build_args(
                plan_dir,
                state_dir,
                reply=None,
                cwd=tmp_dir,
            )
            with (
                mock.patch.object(
                    harnesses,
                    "run_harness",
                    side_effect=fake_run_harness,
                ),
                mock.patch.object(
                    run_coordination,
                    "prepare_mailbox_reply_wait",
                    side_effect=AssertionError(
                        "a correlated worker pause must not publish a second question"
                    ),
                ) as prepare_wait,
            ):
                code, out, _err = self._run_capture(args)

            self.assertEqual(1, calls)
            self.assertEqual(1, code, out)
            prepare_wait.assert_not_called()
            payload = json.loads(out)
            self.assertTrue(payload["paused"])
            self.assertEqual("blocked", payload["pause_reason"])
            self.assertEqual(
                "blocked - supervisor requested a pause",
                payload["pause_message"],
            )
            correlation = payload["pause_details"]["mailbox_resolution"]
            self.assertEqual(
                mailbox_vocabulary.WAITER_ORIGIN_WORKER, correlation["waiter_origin"]
            )
            self.assertEqual("pause", correlation["action"])
            self.assertEqual(len(sensitive_note), correlation["note_length"])
            [remediation] = payload["action_required"]["remediation"]
            self.assertIn("closed the mailbox question", remediation["message"])
            self.assertIn("--resume --reply-stdin", remediation["message"])
            self.assertIn(f"--state-dir {state_dir}", remediation["message"])
            self.assertNotIn("waiting_for_reply", payload)

            view = mailbox.read_mailbox(plan_dir, state_dir=state_dir)
            questions = [
                message
                for message in view.messages
                if message.kind == mailbox_vocabulary.KIND_QUESTION
            ]
            self.assertEqual(1, len(questions))
            self.assertEqual(
                mailbox_vocabulary.WAITER_ORIGIN_WORKER, questions[0].waiter_origin
            )
            plan_state = state.plan_state_dir(plan_dir, state_dir)
            records = read_jsonl(plan_state / events.EVENTS_FILE_NAME)
            wait_events = [
                record
                for record in records
                if record.get("name") == events.MAILBOX_EVENT
                and record.get("payload", {}).get("action")
                in {
                    events.MAILBOX_ACTION_WAIT_STARTED,
                    events.MAILBOX_ACTION_WAIT_CLEARED,
                }
            ]
            self.assertEqual([], wait_events, records)
            self.assertNotIn(sensitive_note, json.dumps(payload))
            self.assertNotIn(sensitive_note, json.dumps(records))

    def test_worker_pause_correlation_rejects_stale_ownership(self) -> None:
        cases = (
            (
                "exact",
                "run-current",
                "1",
                3,
                mailbox_vocabulary.WAITER_ORIGIN_WORKER,
                True,
            ),
            (
                "other-run",
                "run-old",
                "1",
                3,
                mailbox_vocabulary.WAITER_ORIGIN_WORKER,
                False,
            ),
            (
                "other-session",
                "run-current",
                "2",
                3,
                mailbox_vocabulary.WAITER_ORIGIN_WORKER,
                False,
            ),
            (
                "other-turn",
                "run-current",
                "1",
                2,
                mailbox_vocabulary.WAITER_ORIGIN_WORKER,
                False,
            ),
            (
                "runner-origin",
                "run-current",
                "1",
                3,
                mailbox_vocabulary.WAITER_ORIGIN_RUNNER,
                False,
            ),
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            for (
                name,
                question_run_id,
                question_session_id,
                question_turn_index,
                waiter_origin,
                should_match,
            ) in cases:
                with self.subTest(name=name):
                    plan_dir = tmp_dir / name / "plan"
                    state_dir = tmp_dir / name / "state"
                    plan_dir.mkdir(parents=True)
                    question = mailbox_appenders.append_question(
                        plan_dir,
                        "Pause?",
                        state_dir=state_dir,
                        run_id=question_run_id,
                        session_id=question_session_id,
                        turn_index=question_turn_index,
                        waiter_origin=waiter_origin,
                    )
                    resolution = mailbox_appenders.append_resolution(
                        plan_dir,
                        question.id,
                        mailbox_vocabulary.RESOLUTION_ACTION_PAUSE,
                        expected_run_id=question_run_id,
                        state_dir=state_dir,
                    )

                    matched = run_coordination.worker_pause_resolution_for_turn(
                        plan_dir=plan_dir,
                        state_dir_arg=str(state_dir),
                        run_id="run-current",
                        session_id="1",
                        turn_index=3,
                    )

                    if should_match:
                        self.assertEqual(resolution, matched)
                    else:
                        self.assertIsNone(matched)


if __name__ == "__main__":
    unittest.main()
