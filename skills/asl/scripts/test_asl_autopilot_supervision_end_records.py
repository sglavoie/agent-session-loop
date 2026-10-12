#!/usr/bin/env python3
"""Tests for supervision-end record emission per terminal reason."""

from __future__ import annotations

import io
import json
import os
import signal
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout

from asl_autopilot_test_bases import SupervisionEndBase
from asl_autopilot_test_support import (  # noqa: F401
    _OrderRecordingStdout,
    autopilot,
    setUpModule,
    tearDownModule,
)


class SupervisionEndRecordTest(SupervisionEndBase):
    def test_each_terminal_reason_appends_one_mapped_record(self) -> None:
        outcomes = autopilot._mailbox
        cases = (
            ("complete", True, outcomes.SUPERVISION_OUTCOME_COMPLETE),
            ("review_findings", False, outcomes.SUPERVISION_OUTCOME_COMPLETE),
            ("paused", False, outcomes.SUPERVISION_OUTCOME_PAUSED),
            ("needs_agent_reply", False, outcomes.SUPERVISION_OUTCOME_PAUSED),
            ("max-sessions", True, outcomes.SUPERVISION_OUTCOME_STOPPED),
            ("stopped", True, outcomes.SUPERVISION_OUTCOME_STOPPED),
            ("blocked", False, outcomes.SUPERVISION_OUTCOME_BLOCKED),
            ("user_required", False, outcomes.SUPERVISION_OUTCOME_BLOCKED),
            ("worker_hung", False, outcomes.SUPERVISION_OUTCOME_BLOCKED),
            # Unmapped terminal reasons stay `blocked`: `error` is reserved for
            # an exception or a signal.
            ("status-error", False, outcomes.SUPERVISION_OUTCOME_BLOCKED),
            ("run-error", False, outcomes.SUPERVISION_OUTCOME_BLOCKED),
            ("empty-reply", False, outcomes.SUPERVISION_OUTCOME_BLOCKED),
        )
        for index, (reason, ok, outcome) in enumerate(cases):
            with self.subTest(reason=reason):
                plan_dir, state_dir, command = self._plan(f"plan-{index}")
                result = {
                    "ok": ok,
                    "stage": autopilot.RUN_STAGE,
                    "reason": reason,
                    "command": autopilot.parse_command(command),
                }
                exit_code, _ = self._run_main(
                    command, lambda args, result=result: result
                )

                self.assertEqual(0 if ok else 1, exit_code)
                records = self._supervision_ends(plan_dir, state_dir)
                self.assertEqual(1, len(records))
                self.assertEqual(outcome, records[0].supervision_outcome)
                self.assertEqual(f"supervision ended: {reason}", records[0].text)
                self.assertIsNone(records[0].run_id)
                self.assertNotIn("supervision_end", result)

    def test_question_context_post_processing_still_emits_once(self) -> None:
        real_run_autopilot = autopilot.run_autopilot

        def supervision(args):
            return real_run_autopilot(
                args,
                supervisor=lambda parsed, **kwargs: {
                    "ok": False,
                    "stage": autopilot.RUN_STAGE,
                    "reason": "paused",
                },
                questioner=lambda parsed: {"ok": True, "prompt": "answer me"},
            )

        exit_code, stdout = self._run_main(self.command, supervision)

        self.assertEqual(1, exit_code)
        self.assertEqual("needs_agent_reply", json.loads(stdout)["reason"])
        records = self._supervision_ends(self.plan_dir, self.state_dir)
        self.assertEqual(1, len(records))
        self.assertEqual(
            autopilot._mailbox.SUPERVISION_OUTCOME_PAUSED,
            records[0].supervision_outcome,
        )
        self.assertEqual("supervision ended: needs_agent_reply", records[0].text)

    def test_review_context_post_processing_still_emits_once(self) -> None:
        real_run_autopilot = autopilot.run_autopilot
        command = f"{self.command} --review-harness codex"

        def supervision(args):
            return real_run_autopilot(
                args,
                supervisor=lambda parsed, **kwargs: {
                    "ok": True,
                    "stage": autopilot.RUN_STAGE,
                    "reason": "complete",
                    "status": {},
                },
                review_reader=lambda parsed: {
                    "ok": True,
                    "outcome": "findings",
                    "status_line": "2 findings remain",
                },
            )

        exit_code, stdout = self._run_main(command, supervision)

        self.assertEqual(1, exit_code)
        self.assertEqual("review_findings", json.loads(stdout)["reason"])
        records = self._supervision_ends(self.plan_dir, self.state_dir)
        self.assertEqual(1, len(records))
        self.assertEqual(
            autopilot._mailbox.SUPERVISION_OUTCOME_COMPLETE,
            records[0].supervision_outcome,
        )
        self.assertEqual("supervision ended: review_findings", records[0].text)

    def test_supplied_supervision_id_lands_on_the_terminal_record(self) -> None:
        launch = autopilot._mailbox.new_supervision_id()
        result = {
            "ok": True,
            "stage": autopilot.RUN_STAGE,
            "reason": "complete",
            "command": autopilot.parse_command(self.command),
        }

        exit_code, stdout = self._run_main(
            self.command,
            lambda args: result,
            "--supervision-id",
            launch,
        )

        self.assertEqual(0, exit_code)
        self.assertEqual(launch, json.loads(stdout)["supervision_id"])
        records = self._supervision_ends(self.plan_dir, self.state_dir)
        self.assertEqual([launch], [record.supervision_id for record in records])

    def test_launch_without_a_supervision_id_still_mints_one(self) -> None:
        result = {
            "ok": True,
            "stage": autopilot.RUN_STAGE,
            "reason": "complete",
            "command": autopilot.parse_command(self.command),
        }

        exit_code, stdout = self._run_main(self.command, lambda args: result)

        self.assertEqual(0, exit_code)
        minted = json.loads(stdout)["supervision_id"]
        self.assertTrue(autopilot._mailbox.is_supervision_id(minted))
        records = self._supervision_ends(self.plan_dir, self.state_dir)
        self.assertEqual([minted], [record.supervision_id for record in records])

    def test_overlapping_launches_leave_distinguishable_records(self) -> None:
        # Two helpers on one plan: sequence cannot tell their records apart,
        # the launch identity can.
        launches = [autopilot._mailbox.new_supervision_id() for _ in range(2)]
        for launch in launches:
            result = {
                "ok": True,
                "stage": autopilot.RUN_STAGE,
                "reason": "complete",
                "command": autopilot.parse_command(self.command),
            }
            exit_code, _ = self._run_main(
                self.command,
                lambda args, result=result: result,
                "--supervision-id",
                launch,
            )
            self.assertEqual(0, exit_code)

        records = self._supervision_ends(self.plan_dir, self.state_dir)
        self.assertEqual(launches, [record.supervision_id for record in records])

    def test_error_path_record_carries_the_launch_identity(self) -> None:
        launch = autopilot._mailbox.new_supervision_id()

        def supervision(args):
            raise RuntimeError("supervisor exploded")

        self._patch(autopilot, "run_autopilot", supervision)
        with self.assertRaises(RuntimeError), redirect_stdout(io.StringIO()):
            autopilot.main(
                [
                    "--command",
                    self.command,
                    "--json",
                    "--supervision-id",
                    launch,
                ]
            )

        records = self._supervision_ends(self.plan_dir, self.state_dir)
        self.assertEqual([launch], [record.supervision_id for record in records])
        self.assertEqual(
            autopilot._mailbox.SUPERVISION_OUTCOME_ERROR,
            records[0].supervision_outcome,
        )

    def test_malformed_supervision_id_is_refused_before_supervision(self) -> None:
        with (
            self.assertRaises(SystemExit) as raised,
            redirect_stderr(io.StringIO()),
        ):
            autopilot.main(
                [
                    "--command",
                    self.command,
                    "--json",
                    "--supervision-id",
                    "not-a-uuid",
                ]
            )

        self.assertEqual(2, raised.exception.code)
        self.assertFalse(
            autopilot._mailbox.mailbox_path_for_plan(
                self.plan_dir, self.state_dir
            ).exists()
        )

    def test_emitter_refuses_a_second_record_for_the_same_process(self) -> None:
        appended = []
        emitter = autopilot._SupervisionEndEmitter(
            supervision_id=autopilot._mailbox.new_supervision_id(),
            append=lambda *args, **kwargs: appended.append((args, kwargs)),
        )
        parsed = autopilot.parse_command(self.command)

        self.assertTrue(
            emitter.emit(
                parsed,
                outcome=autopilot._mailbox.SUPERVISION_OUTCOME_COMPLETE,
                text="supervision ended: complete",
            )
        )
        self.assertFalse(
            emitter.emit(
                parsed,
                outcome=autopilot._mailbox.SUPERVISION_OUTCOME_ERROR,
                text="supervision ended: error - RuntimeError",
            )
        )
        self.assertEqual(1, len(appended))

    def test_no_mailbox_command_emits_nothing(self) -> None:
        command = f"{self.command} --no-mailbox"
        result = {
            "ok": True,
            "stage": autopilot.RUN_STAGE,
            "reason": "complete",
            "command": autopilot.parse_command(command),
        }

        exit_code, _ = self._run_main(command, lambda args: result)

        self.assertEqual(0, exit_code)
        self.assertFalse(
            autopilot._mailbox.mailbox_path_for_plan(
                self.plan_dir, self.state_dir
            ).exists()
        )

    def test_parse_failure_emits_nothing(self) -> None:
        # `plan_dir` is untrustworthy when the command never parsed, so nothing
        # is appended even though the failing command names a real plan.
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            exit_code = autopilot.main(["--command", f"{self.command} | cat", "--json"])

        self.assertEqual(1, exit_code)
        self.assertEqual("shell-syntax", json.loads(buffer.getvalue())["reason"])
        self.assertFalse(
            autopilot._mailbox.mailbox_path_for_plan(
                self.plan_dir, self.state_dir
            ).exists()
        )

    def test_raised_supervisor_exception_emits_error_and_propagates(self) -> None:
        def supervision(args):
            raise RuntimeError("supervisor exploded")

        self._patch(autopilot, "run_autopilot", supervision)
        with self.assertRaises(RuntimeError), redirect_stdout(io.StringIO()):
            autopilot.main(["--command", self.command, "--json"])

        records = self._supervision_ends(self.plan_dir, self.state_dir)
        self.assertEqual(1, len(records))
        self.assertEqual(
            autopilot._mailbox.SUPERVISION_OUTCOME_ERROR,
            records[0].supervision_outcome,
        )
        self.assertEqual("supervision ended: error - RuntimeError", records[0].text)

    def test_append_failure_leaves_result_and_exit_code_untouched(self) -> None:
        def boom(*args, **kwargs):
            raise OSError("mailbox is unwritable")

        self._patch(autopilot._mailbox, "append_supervision_end", boom)
        result = {
            "ok": True,
            "stage": autopilot.RUN_STAGE,
            "reason": "complete",
            "command": autopilot.parse_command(self.command),
        }

        exit_code, stdout = self._run_main(self.command, lambda args: result)

        self.assertEqual(0, exit_code)
        self.assertTrue(result["ok"])
        self.assertEqual(
            {"emitted": False, "error": "OSError: mailbox is unwritable"},
            result["supervision_end"],
        )
        # The result was already on stdout when the append failed, so the
        # failure note is reported in-process only.
        self.assertNotIn("supervision_end", json.loads(stdout))

    def test_termination_signal_emits_and_restores_handlers(self) -> None:
        def supervision(args):
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(5)
            raise AssertionError("SIGTERM was never delivered")

        previous_term = signal.getsignal(signal.SIGTERM)
        previous_int = signal.getsignal(signal.SIGINT)
        self._patch(autopilot, "run_autopilot", supervision)
        with (
            self.assertRaises(SystemExit) as caught,
            redirect_stdout(io.StringIO()),
        ):
            autopilot.main(["--command", self.command, "--json"])

        self.assertEqual(128 + int(signal.SIGTERM), caught.exception.code)
        self.assertIs(previous_term, signal.getsignal(signal.SIGTERM))
        self.assertIs(previous_int, signal.getsignal(signal.SIGINT))
        records = self._supervision_ends(self.plan_dir, self.state_dir)
        self.assertEqual(1, len(records))
        self.assertEqual(
            autopilot._mailbox.SUPERVISION_OUTCOME_ERROR,
            records[0].supervision_outcome,
        )
        self.assertEqual("supervision ended: error - SIGTERM", records[0].text)

    def test_record_is_appended_after_the_result_is_flushed(self) -> None:
        events: list = []
        stdout = _OrderRecordingStdout(events)
        real_append = autopilot._mailbox.append_supervision_end

        def recording_append(*args, **kwargs):
            events.append(("append", stdout.getvalue()))
            return real_append(*args, **kwargs)

        self._patch(autopilot._mailbox, "append_supervision_end", recording_append)
        result = {
            "ok": True,
            "stage": autopilot.RUN_STAGE,
            "reason": "complete",
            "command": autopilot.parse_command(self.command),
        }
        self._patch(autopilot, "run_autopilot", lambda args: result)

        with redirect_stdout(stdout):
            exit_code = autopilot.main(["--command", self.command, "--json"])

        self.assertEqual(0, exit_code)
        self.assertEqual(["flush", "append"], [event[0] for event in events])
        flushed = events[1][1]
        self.assertTrue(flushed.endswith("\n"))
        self.assertEqual("complete", json.loads(flushed)["reason"])
        self.assertEqual(1, len(self._supervision_ends(self.plan_dir, self.state_dir)))


if __name__ == "__main__":
    unittest.main()
