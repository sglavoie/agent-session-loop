#!/usr/bin/env python3
"""Tests for the supervision-end terminal write contract."""

from __future__ import annotations

import io
import json
import signal
import unittest
from contextlib import redirect_stdout

from asl_autopilot_test_bases import SupervisionEndBase
from asl_autopilot_test_support import (  # noqa: F401
    _TruncatingStdout,
    autopilot,
    setUpModule,
    tearDownModule,
)

from agent_session_loop import mailbox_appenders, mailbox_vocabulary


class SupervisionEndTerminalWriteTest(SupervisionEndBase):
    def test_failed_append_still_reports_a_deferred_signal(self) -> None:
        def boom(*args, **kwargs):
            signal.raise_signal(signal.SIGTERM)
            raise OSError("mailbox is unwritable")

        result = self._complete_result(self.command)
        self._patch(autopilot, "run_autopilot", lambda args: result)
        self._patch(mailbox_appenders, "append_supervision_end", boom)

        with (
            self.assertRaises(SystemExit) as caught,
            redirect_stdout(io.StringIO()),
        ):
            autopilot.main(["--command", self.command, "--json"])

        # The record is lost with the mailbox, but the signal is not: an
        # operator who sent SIGTERM still sees the process die of it.
        self.assertEqual(128 + int(signal.SIGTERM), caught.exception.code)
        self.assertFalse(
            autopilot._mailbox.mailbox_path_for_plan(
                self.plan_dir, self.state_dir
            ).exists()
        )

    def test_a_normal_exit_never_reports_the_error_outcome(self) -> None:
        # The contract is readable from the outcome alone only while `error`
        # stays reserved for an abnormal exit: no mapped reason, and no
        # unmapped one, may reach it from the normal path.
        error = mailbox_vocabulary.SUPERVISION_OUTCOME_ERROR
        self.assertNotIn(error, set(autopilot.SUPERVISION_END_OUTCOMES.values()))
        self.assertNotEqual(error, autopilot.DEFAULT_SUPERVISION_OUTCOME)
        outcome, _ = autopilot._supervision_end_fields({"reason": "no-such-reason"})
        self.assertNotEqual(error, outcome)

        result = self._complete_result(self.command)
        exit_code, stdout = self._run_main(self.command, lambda args: result)

        self.assertEqual(0, exit_code)
        records = self._supervision_ends(self.plan_dir, self.state_dir)
        self.assertNotEqual(error, records[0].supervision_outcome)
        self._assert_terminal_contract(records, stdout)

    def test_a_raised_supervision_writes_no_result_at_all(self) -> None:
        def supervision(args):
            raise RuntimeError("supervisor exploded")

        self._patch(autopilot, "run_autopilot", supervision)
        stdout = io.StringIO()
        with self.assertRaises(RuntimeError), redirect_stdout(stdout):
            autopilot.main(["--command", self.command, "--json"])

        self.assertEqual("", stdout.getvalue())
        records = self._supervision_ends(self.plan_dir, self.state_dir)
        self._assert_terminal_contract(records, stdout.getvalue())
        self.assertEqual("supervision ended: error - RuntimeError", records[0].text)

    def test_an_unserializable_result_leaves_no_partial_json(self) -> None:
        # Serialization fails at the one point where the helper used to stream
        # straight to stdout; a supervisor must find nothing rather than half
        # an object.
        result = self._complete_result(self.command)
        result["hang"] = object()
        self._patch(autopilot, "run_autopilot", lambda args: result)
        stdout = io.StringIO()

        with self.assertRaises(TypeError), redirect_stdout(stdout):
            autopilot.main(["--command", self.command, "--json"])

        self.assertEqual("", stdout.getvalue())
        records = self._supervision_ends(self.plan_dir, self.state_dir)
        self._assert_terminal_contract(records, stdout.getvalue())
        self.assertEqual("supervision ended: error - TypeError", records[0].text)

    def test_termination_before_the_result_write_leaves_only_the_record(self) -> None:
        def terminated(signal_number):
            def supervision(args):
                signal.raise_signal(signal_number)
                raise AssertionError("the termination signal was never delivered")

            return supervision

        for signal_number in autopilot.TERMINATION_SIGNALS:
            with self.subTest(signal=signal_number.name):
                plan_dir, state_dir, command = self._plan(f"pre-{signal_number:d}")
                self._patch(autopilot, "run_autopilot", terminated(signal_number))
                stdout = io.StringIO()

                with (
                    self.assertRaises(SystemExit) as caught,
                    redirect_stdout(stdout),
                ):
                    autopilot.main(["--command", command, "--json"])

                self.assertEqual(128 + int(signal_number), caught.exception.code)
                self.assertEqual("", stdout.getvalue())
                records = self._supervision_ends(plan_dir, state_dir)
                self._assert_terminal_contract(records, stdout.getvalue())
                self.assertEqual(
                    f"supervision ended: error - {signal_number.name}",
                    records[0].text,
                )

    def test_termination_during_the_result_write_truncates_stdout(self) -> None:
        for signal_number in autopilot.TERMINATION_SIGNALS:
            with self.subTest(signal=signal_number.name):
                plan_dir, state_dir, command = self._plan(f"mid-{signal_number:d}")
                result = self._complete_result(command)
                self._patch(
                    autopilot, "run_autopilot", lambda args, result=result: result
                )
                stdout = _TruncatingStdout(signal_number)

                with (
                    self.assertRaises(SystemExit) as caught,
                    redirect_stdout(stdout),
                ):
                    autopilot.main(["--command", command, "--json"])

                self.assertEqual(128 + int(signal_number), caught.exception.code)
                # Exactly what the `error` outcome exists to warn about: the
                # supervisor is awake with unusable stdout.
                self.assertNotEqual("", stdout.getvalue())
                with self.assertRaises(ValueError):
                    json.loads(stdout.getvalue())
                records = self._supervision_ends(plan_dir, state_dir)
                self._assert_terminal_contract(records, stdout.getvalue())
                self.assertEqual(
                    f"supervision ended: error - {signal_number.name}",
                    records[0].text,
                )


if __name__ == "__main__":
    unittest.main()
