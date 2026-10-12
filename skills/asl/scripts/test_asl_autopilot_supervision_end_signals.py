#!/usr/bin/env python3
"""Tests for supervision-end records under termination signals at each seam."""

from __future__ import annotations

import io
import json
import signal
import unittest
from contextlib import contextmanager, redirect_stdout

from asl_autopilot_test_bases import SupervisionEndBase
from asl_autopilot_test_support import (  # noqa: F401
    autopilot,
    setUpModule,
    tearDownModule,
)


class SupervisionEndSignalSeamTest(SupervisionEndBase):
    def test_signal_after_result_flush_still_emits_error_record(self) -> None:
        # `_supervision_end_fields` is the first statement the helper runs once
        # the result is flushed, so firing from it lands the signal exactly in
        # the window that used to sit outside the `BaseException` guard.
        def terminated_fields(signal_number):
            def fields(result):
                signal.raise_signal(signal_number)
                raise AssertionError("the termination signal was never delivered")

            return fields

        for signal_number in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=signal_number):
                plan_dir, state_dir, command = self._plan(f"flush-{signal_number:d}")
                result = self._complete_result(command)
                self._patch(
                    autopilot, "run_autopilot", lambda args, result=result: result
                )
                self._patch(
                    autopilot,
                    "_supervision_end_fields",
                    terminated_fields(signal_number),
                )
                previous_term = signal.getsignal(signal.SIGTERM)
                previous_int = signal.getsignal(signal.SIGINT)
                stdout = io.StringIO()

                with (
                    self.assertRaises(SystemExit) as caught,
                    redirect_stdout(stdout),
                ):
                    autopilot.main(["--command", command, "--json"])

                self.assertEqual(128 + int(signal_number), caught.exception.code)
                self.assertIs(previous_term, signal.getsignal(signal.SIGTERM))
                self.assertIs(previous_int, signal.getsignal(signal.SIGINT))
                self.assertEqual("complete", json.loads(stdout.getvalue())["reason"])
                records = self._supervision_ends(plan_dir, state_dir)
                self.assertEqual(1, len(records))
                self.assertEqual(
                    autopilot._mailbox.SUPERVISION_OUTCOME_ERROR,
                    records[0].supervision_outcome,
                )
                self.assertEqual(
                    f"supervision ended: error - {signal.Signals(signal_number).name}",
                    records[0].text,
                )

    def test_signal_before_the_terminal_write_emits_error_record(self) -> None:
        real_guard = autopilot._deferred_termination_signals
        # `_termination_handlers` holds the same guard around its install and
        # restore loops, so the fake arms itself only for the emitter's window.
        armed = {"value": False}

        @contextmanager
        def signalling_guard():
            with real_guard() as deferred:
                if armed["value"]:
                    armed["value"] = False
                    # Inside the deferral window and before the record is
                    # written: recorded rather than raised, so the outcome
                    # flips to error.
                    signal.raise_signal(signal.SIGTERM)
                yield deferred

        result = self._complete_result(self.command)

        def supervision(args):
            armed["value"] = True
            return result

        self._patch(autopilot, "run_autopilot", supervision)
        self._patch(autopilot, "_deferred_termination_signals", signalling_guard)
        previous_term = signal.getsignal(signal.SIGTERM)
        previous_int = signal.getsignal(signal.SIGINT)

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

    def test_signal_at_an_install_seam_still_emits_one_error_record(self) -> None:
        # MED-1: the handlers used to go in before `main` opened the guard
        # that writes the terminal record, so a signal landing between the two
        # installs exited with no record and a leaked disposition.
        for call in (1, 2):
            for number in autopilot.TERMINATION_SIGNALS:
                with self.subTest(call=call, signal=number.name):
                    plan_dir, state_dir, command = self._plan(f"seam-{call}-{number}")

                    def fire(swap, number=number):
                        previous = swap()
                        # Blocked by the install hold, so it stays pending
                        # until the mask lifts inside `main`'s guard.
                        signal.raise_signal(number)
                        return previous

                    self._counting_signal(call=call, fire=fire)
                    launched: list = []
                    self._patch(
                        autopilot,
                        "run_autopilot",
                        lambda args, launched=launched: launched.append(args),
                    )
                    previous_term = signal.getsignal(signal.SIGTERM)
                    previous_int = signal.getsignal(signal.SIGINT)

                    with (
                        self.assertRaises(SystemExit) as caught,
                        redirect_stdout(io.StringIO()),
                    ):
                        autopilot.main(["--command", command, "--json"])

                    self.assertEqual(128 + int(number), caught.exception.code)
                    self.assertEqual([], launched)
                    self.assertIs(previous_term, signal.getsignal(signal.SIGTERM))
                    self.assertIs(previous_int, signal.getsignal(signal.SIGINT))
                    records = self._supervision_ends(plan_dir, state_dir)
                    self.assertEqual(1, len(records))
                    self.assertEqual(
                        autopilot._mailbox.SUPERVISION_OUTCOME_ERROR,
                        records[0].supervision_outcome,
                    )
                    self.assertEqual(
                        f"supervision ended: error - {number.name}",
                        records[0].text,
                    )

    def test_failed_install_restores_the_partial_swap(self) -> None:
        def fire(swap):
            # The second install never happens, so only SIGTERM was swapped.
            raise RuntimeError("dispositions are unavailable")

        self._counting_signal(call=2, fire=fire)
        self._patch(autopilot, "run_autopilot", lambda args: self.fail("launched"))
        previous_term = signal.getsignal(signal.SIGTERM)
        previous_int = signal.getsignal(signal.SIGINT)

        with self.assertRaises(RuntimeError), redirect_stdout(io.StringIO()):
            autopilot.main(["--command", self.command, "--json"])

        self.assertIs(previous_term, signal.getsignal(signal.SIGTERM))
        self.assertIs(previous_int, signal.getsignal(signal.SIGINT))
        records = self._supervision_ends(self.plan_dir, self.state_dir)
        self.assertEqual(1, len(records))
        self.assertEqual(
            autopilot._mailbox.SUPERVISION_OUTCOME_ERROR,
            records[0].supervision_outcome,
        )
        self.assertEqual("supervision ended: error - RuntimeError", records[0].text)

    def test_signal_at_the_restore_seam_keeps_the_committed_record(self) -> None:
        # The accepted nuance: the record is already committed, so the signal
        # only decides the exit code. In production the restored disposition is
        # `SIG_DFL` and the kernel ends the process; standing in a raising
        # handler observes the same outcome without killing the test runner.
        delivered: list = []

        def prior_term(number, frame):
            delivered.append(number)
            raise autopilot._TerminatedBySignal(number)

        real_signal = signal.signal
        previous_term = real_signal(signal.SIGTERM, prior_term)
        self.addCleanup(real_signal, signal.SIGTERM, previous_term)

        def fire(swap):
            # Call 3 restores SIGINT; SIGTERM is still ours until call 4, and
            # the hold keeps the signal pending across both.
            previous = swap()
            signal.raise_signal(signal.SIGTERM)
            return previous

        self._counting_signal(call=3, fire=fire)
        result = self._complete_result(self.command)
        self._patch(autopilot, "run_autopilot", lambda args: result)
        previous_int = signal.getsignal(signal.SIGINT)

        with (
            self.assertRaises(SystemExit) as caught,
            redirect_stdout(io.StringIO()),
        ):
            autopilot.main(["--command", self.command, "--json"])

        self.assertEqual(128 + int(signal.SIGTERM), caught.exception.code)
        self.assertEqual([int(signal.SIGTERM)], delivered)
        self.assertIs(prior_term, signal.getsignal(signal.SIGTERM))
        self.assertIs(previous_int, signal.getsignal(signal.SIGINT))
        records = self._supervision_ends(self.plan_dir, self.state_dir)
        self.assertEqual(1, len(records))
        self.assertEqual(
            autopilot._mailbox.SUPERVISION_OUTCOME_COMPLETE,
            records[0].supervision_outcome,
        )

    def test_signal_during_the_terminal_write_records_the_termination(self) -> None:
        real_append = autopilot._mailbox.append_supervision_end

        def signalling_append(*args, **kwargs):
            # The append has begun and can still be waiting on the lock or
            # reading the log; the outcome is resolved after all of that.
            signal.raise_signal(signal.SIGINT)
            return real_append(*args, **kwargs)

        result = self._complete_result(self.command)
        self._patch(autopilot, "run_autopilot", lambda args: result)
        self._patch(autopilot._mailbox, "append_supervision_end", signalling_append)

        with (
            self.assertRaises(SystemExit) as caught,
            redirect_stdout(io.StringIO()),
        ):
            autopilot.main(["--command", self.command, "--json"])

        self.assertEqual(128 + int(signal.SIGINT), caught.exception.code)
        # `_supervision_ends` refuses rejected lines, so one record here means
        # the deferral carried the write through whole.
        records = self._supervision_ends(self.plan_dir, self.state_dir)
        self.assertEqual(1, len(records))
        self.assertEqual(
            autopilot._mailbox.SUPERVISION_OUTCOME_ERROR,
            records[0].supervision_outcome,
        )
        self.assertEqual("supervision ended: error - SIGINT", records[0].text)

    def test_signal_at_the_mailbox_write_still_records_the_termination(
        self,
    ) -> None:
        real_write = autopilot._mailbox.os.write

        def signalling_write(signal_number):
            def write(fd, data):
                # Fired after the commit-time resolver has already answered and
                # the record's bytes exist: the mask holds the signal, so only
                # a decision taken after the write can see it.
                if b'"kind":"supervision-end"' in data:
                    signal.raise_signal(signal_number)
                return real_write(fd, data)

            return write

        for signal_number in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=signal_number):
                plan_dir, state_dir, command = self._plan(f"write-{signal_number:d}")
                result = self._complete_result(command)
                self._patch(
                    autopilot, "run_autopilot", lambda args, result=result: result
                )
                self._patch(
                    autopilot._mailbox.os,
                    "write",
                    signalling_write(signal_number),
                )
                previous_term = signal.getsignal(signal.SIGTERM)
                previous_int = signal.getsignal(signal.SIGINT)
                stdout = io.StringIO()

                with (
                    self.assertRaises(SystemExit) as caught,
                    redirect_stdout(stdout),
                ):
                    autopilot.main(["--command", command, "--json"])

                self.assertEqual(128 + int(signal_number), caught.exception.code)
                self.assertIs(previous_term, signal.getsignal(signal.SIGTERM))
                self.assertIs(previous_int, signal.getsignal(signal.SIGINT))
                self.assertEqual("complete", json.loads(stdout.getvalue())["reason"])
                records = self._supervision_ends(plan_dir, state_dir)
                self.assertEqual(1, len(records))
                self.assertEqual(
                    autopilot._mailbox.SUPERVISION_OUTCOME_ERROR,
                    records[0].supervision_outcome,
                )
                self.assertEqual(
                    f"supervision ended: error - {signal.Signals(signal_number).name}",
                    records[0].text,
                )

    def test_signal_at_the_durability_barrier_still_records_the_termination(
        self,
    ) -> None:
        real_fullsync = autopilot._mailbox.durability.fullsync

        def signalling_fullsync(signal_number):
            fired: list[int] = []

            def fullsync(fd):
                # Only the terminal append holds termination, so a blocked
                # signal is what identifies this sync as the record's own. The
                # injection lands after the resolver that precedes the write
                # has already answered `complete`.
                held = signal.pthread_sigmask(signal.SIG_BLOCK, ())
                if not fired and signal_number in held:
                    fired.append(fd)
                    signal.raise_signal(signal_number)
                return real_fullsync(fd)

            return fullsync

        for signal_number in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=signal_number):
                plan_dir, state_dir, command = self._plan(f"sync-{signal_number:d}")
                result = self._complete_result(command)
                self._patch(
                    autopilot, "run_autopilot", lambda args, result=result: result
                )
                self._patch(
                    autopilot._mailbox.durability,
                    "fullsync",
                    signalling_fullsync(signal_number),
                )
                previous_term = signal.getsignal(signal.SIGTERM)
                previous_int = signal.getsignal(signal.SIGINT)
                stdout = io.StringIO()

                with (
                    self.assertRaises(SystemExit) as caught,
                    redirect_stdout(stdout),
                ):
                    autopilot.main(["--command", command, "--json"])

                self.assertEqual(128 + int(signal_number), caught.exception.code)
                self.assertIs(previous_term, signal.getsignal(signal.SIGTERM))
                self.assertIs(previous_int, signal.getsignal(signal.SIGINT))
                self.assertEqual("complete", json.loads(stdout.getvalue())["reason"])
                records = self._supervision_ends(plan_dir, state_dir)
                self.assertEqual(1, len(records))
                self.assertEqual(
                    autopilot._mailbox.SUPERVISION_OUTCOME_ERROR,
                    records[0].supervision_outcome,
                )
                self.assertEqual(
                    f"supervision ended: error - {signal.Signals(signal_number).name}",
                    records[0].text,
                )

    def test_signal_while_the_hold_is_taken_still_emits_one_error_record(
        self,
    ) -> None:
        # The hold used to be a two-step handler swap: a signal landing between
        # the two installations escaped as `_TerminatedBySignal` with the
        # emitter already spent, so no record was ever written.
        real_guard = autopilot._deferred_termination_signals
        armed = {"value": False}

        @contextmanager
        def interrupted_guard():
            if armed["value"]:
                armed["value"] = False
                raise autopilot._TerminatedBySignal(signal.SIGTERM)
            with real_guard() as deferred:
                yield deferred

        result = self._complete_result(self.command)

        def supervision(args):
            # Handler installation is done; the next window is the emitter's.
            armed["value"] = True
            return result

        self._patch(autopilot, "run_autopilot", supervision)
        self._patch(autopilot, "_deferred_termination_signals", interrupted_guard)
        previous_term = signal.getsignal(signal.SIGTERM)
        previous_int = signal.getsignal(signal.SIGINT)

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


if __name__ == "__main__":
    unittest.main()
