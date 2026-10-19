"""PostResultRecoverySentinelTests.

Split from tests/test_asl_control.py."""

from __future__ import annotations

import signal
import subprocess
import sys
import unittest
from datetime import UTC, datetime
from typing import Any
from unittest import mock

from agent_session_loop import (
    process_sampling,
    process_tree,
    turn_execution_constants,
    turn_execution_hang_sentinel,
)


class PostResultRecoverySentinelTests(unittest.TestCase):
    NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)

    @staticmethod
    def tree() -> list[dict[str, Any]]:
        return [
            {
                "pid": 123,
                "ppid": 1,
                "state": "S",
                "cpu_seconds": 5.0,
                "birth": "root-birth",
                "command": "claude --print",
            },
            {
                "pid": 456,
                "ppid": 123,
                "state": "S",
                "cpu_seconds": 1.0,
                "birth": "leaf-birth",
                "command": "eza -la",
            },
        ]

    def suspected_result(
        self, sentinel: turn_execution_hang_sentinel.HangSentinel
    ) -> turn_execution_hang_sentinel.HangAssessment:
        sentinel.note_harness_pid(123)
        sentinel.note_final_result()
        sentinel.evaluate(idle_seconds=60.0, now=self.NOW)
        sentinel.evaluate(idle_seconds=90.0, now=self.NOW)
        assessment = sentinel.evaluate(idle_seconds=120.0, now=self.NOW)
        self.assertEqual("suspected", assessment.transition)
        return assessment

    def make_sentinel(self) -> turn_execution_hang_sentinel.HangSentinel:
        return turn_execution_hang_sentinel.HangSentinel(
            sample_tree=lambda pid: self.tree(),
            monotonic=lambda: 100.0,
            trigger_idle_seconds=120.0,
            confirm_samples=2,
            cpu_epsilon_seconds=0.5,
            result_grace_seconds=0.0,
        )

    def test_terminal_result_attempts_sigterm_once_and_attaches_report(self) -> None:
        sentinel = self.make_sentinel()
        assessment = self.suspected_result(sentinel)
        report = {
            "policy": process_tree.POST_RESULT_RECOVERY_POLICY,
            "attempted_at": "now",
            "signal": "SIGTERM",
            "candidate_pids": [456],
            "signaled_pids": [],
            "already_gone_pids": [],
            "identity_mismatches": [],
            "signal_failures": [],
        }
        with (
            mock.patch.object(
                process_tree,
                "prepare_verified_leaf_recovery",
                return_value=(report, (456,)),
            ) as prepare,
            mock.patch.object(
                process_tree,
                "signal_verified_leaf_recovery",
                return_value={**report, "signaled_pids": [456]},
            ) as deliver,
        ):
            recovered = sentinel.recover_post_result(assessment)
            refreshed = sentinel.evaluate(idle_seconds=150.0, now=self.NOW)
            sentinel.recover_post_result(refreshed)

        assert recovered.suspected is not None
        self.assertEqual([456], recovered.suspected["recovery"]["signaled_pids"])
        prepare.assert_called_once()
        deliver.assert_called_once()

    def test_generic_idle_suspicion_never_attempts_recovery(self) -> None:
        sentinel = self.make_sentinel()
        sentinel.note_harness_pid(123)
        sentinel.evaluate(idle_seconds=120.0, now=self.NOW)
        sentinel.evaluate(idle_seconds=150.0, now=self.NOW)
        assessment = sentinel.evaluate(idle_seconds=180.0, now=self.NOW)
        self.assertEqual(
            turn_execution_constants.HANG_REASON_ZERO_CPU,
            assessment.suspected["reason"],
        )

        with mock.patch.object(
            process_tree, "prepare_verified_leaf_recovery"
        ) as prepare:
            unchanged = sentinel.recover_post_result(assessment)

        self.assertIs(assessment, unchanged)
        prepare.assert_not_called()

    def root_sentinel(self):
        ticks = [100.0]
        sentinel = turn_execution_hang_sentinel.HangSentinel(
            sample_tree=lambda pid: self.tree()[:1],
            monotonic=lambda: ticks[0],
            confirm_samples=2,
            result_grace_seconds=0,
            root_shutdown_grace_seconds=30,
        )
        return sentinel, ticks

    def test_root_shutdown_is_bounded_and_preserves_both_attempts(self) -> None:
        sentinel, ticks = self.root_sentinel()
        assessment = self.suspected_result(sentinel)
        table = {123: self.tree()[0]}
        kills = []
        signal_root = process_tree.signal_verified_root_recovery

        def deliver(report, pids, *, force=False):
            return signal_root(
                report,
                pids,
                force=force,
                kill=lambda pid, sig: kills.append((pid, sig)),
            )

        with (
            mock.patch.object(
                process_sampling, "sample_process_table", return_value=table
            ),
            mock.patch.object(
                process_tree, "signal_verified_root_recovery", side_effect=deliver
            ),
        ):
            sentinel.recover_post_result(assessment)
            ticks[0] = 129
            refreshed = sentinel.evaluate(idle_seconds=150, now=self.NOW)
            sentinel.recover_post_result(refreshed)
            self.assertEqual([(123, signal.SIGTERM)], kills)
            self.assertEqual([123], refreshed.suspected["recovery"]["signaled_pids"])
            ticks[0] = 130
            escalated = sentinel.recover_post_result(
                sentinel.evaluate(idle_seconds=180, now=self.NOW)
            )
            self.assertEqual([(123, signal.SIGTERM), (123, signal.SIGKILL)], kills)
            self.assertEqual(
                "SIGTERM", escalated.suspected["recovery"]["previous_attempt"]["signal"]
            )
            ticks[0] = 200
            sentinel.recover_post_result(
                sentinel.evaluate(idle_seconds=240, now=self.NOW)
            )
            self.assertEqual(2, len(kills))

    class HeldInput:
        """A transport whose stdin is still open after a terminal result."""

        def __init__(self, reason: str = "background-tasks") -> None:
            self.reason: str | None = reason
            self.releases = 0

        def input_hold_reason(self) -> str | None:
            return self.reason

        def release_input(self) -> bool:
            self.releases += 1
            self.reason = None
            return True

    def test_held_stdin_on_an_idle_root_is_closed_before_any_signal(self) -> None:
        # ASI-151: the CLI kept stdin open for a task it never reported
        # finished, and sat alone and idle behind it. EOF is the graceful ask,
        # tried once; the verified SIGTERM path is the fallback after grace.
        sentinel, ticks = self.root_sentinel()
        held = self.HeldInput()
        sentinel.note_input_channel(held)  # type: ignore[arg-type]
        assessment = self.suspected_result(sentinel)
        kills = []
        signal_root = process_tree.signal_verified_root_recovery

        def deliver(report, pids, *, force=False):
            return signal_root(
                report,
                pids,
                force=force,
                kill=lambda pid, sig: kills.append((pid, sig)),
            )

        with (
            mock.patch.object(
                process_sampling,
                "sample_process_table",
                return_value={123: self.tree()[0]},
            ),
            mock.patch.object(
                process_tree, "signal_verified_root_recovery", side_effect=deliver
            ),
        ):
            released = sentinel.recover_post_result(assessment)
            recovery = released.suspected["recovery"]
            self.assertEqual(
                turn_execution_constants.POST_RESULT_STDIN_RELEASE_POLICY,
                recovery["policy"],
            )
            self.assertEqual("background-tasks", recovery["hold_reason"])
            self.assertTrue(recovery["released"])
            self.assertEqual(1, held.releases)
            self.assertEqual([], kills)

            ticks[0] = 129
            sentinel.recover_post_result(
                sentinel.evaluate(idle_seconds=150, now=self.NOW)
            )
            self.assertEqual([], kills)

            ticks[0] = 130
            term = sentinel.recover_post_result(
                sentinel.evaluate(idle_seconds=180, now=self.NOW)
            )
            self.assertEqual([(123, signal.SIGTERM)], kills)
            self.assertEqual(
                turn_execution_constants.POST_RESULT_STDIN_RELEASE_POLICY,
                term.suspected["recovery"]["previous_attempt"]["policy"],
            )
        self.assertEqual(1, held.releases)

    def test_held_stdin_with_live_descendants_reaps_nothing(self) -> None:
        # The hold may be for a background task still running below the
        # root, so neither stdin nor any leaf is touched.
        sentinel = self.make_sentinel()
        held = self.HeldInput()
        sentinel.note_input_channel(held)  # type: ignore[arg-type]
        assessment = self.suspected_result(sentinel)

        with mock.patch.object(
            process_tree, "prepare_verified_leaf_recovery"
        ) as prepare:
            unchanged = sentinel.recover_post_result(assessment)

        self.assertIs(assessment, unchanged)
        prepare.assert_not_called()
        self.assertEqual(0, held.releases)

    def test_root_escalation_checks_the_original_identity(self) -> None:
        sentinel, ticks = self.root_sentinel()
        assessment = self.suspected_result(sentinel)
        table = {123: dict(self.tree()[0])}
        with (
            mock.patch.object(
                process_sampling, "sample_process_table", return_value=table
            ),
            mock.patch.object(
                process_tree,
                "signal_verified_root_recovery",
                wraps=process_tree.signal_verified_root_recovery,
            ) as deliver,
        ):
            # Prepare only, avoiding a real signal to the fixture pid.
            deliver.side_effect = lambda report, pids, **kwargs: {
                **report,
                "signaled_pids": list(pids),
            }
            sentinel.recover_post_result(assessment)
            ticks[0] = 131
            table[123]["birth"] = "reused-pid"
            recovered = sentinel.recover_post_result(
                sentinel.evaluate(idle_seconds=180, now=self.NOW)
            )
            self.assertEqual((), deliver.call_args.args[1])
            self.assertTrue(recovered.suspected["recovery"]["identity_mismatches"])

    def test_a_failed_sigterm_never_escalates_to_sigkill(self) -> None:
        sentinel, ticks = self.root_sentinel()
        assessment = self.suspected_result(sentinel)
        report = {
            "policy": process_tree.POST_RESULT_ROOT_RECOVERY_POLICY,
            "signal": "SIGTERM",
            "signaled_pids": [],
            "signal_failures": [{"pid": 123, "error": "denied"}],
        }
        with (
            mock.patch.object(
                process_tree,
                "prepare_verified_root_recovery",
                return_value=(report, (123,)),
            ) as prepare,
            mock.patch.object(
                process_tree, "signal_verified_root_recovery", return_value=report
            ) as deliver,
        ):
            sentinel.recover_post_result(assessment)
            ticks[0] = 200
            refreshed = sentinel.recover_post_result(
                sentinel.evaluate(idle_seconds=240, now=self.NOW)
            )
        prepare.assert_called_once()
        deliver.assert_called_once()
        self.assertEqual(report, refreshed.suspected["recovery"])

    def test_resumed_activity_cancels_prepared_root_shutdown(self) -> None:
        sentinel, ticks = self.root_sentinel()
        assessment = self.suspected_result(sentinel)

        def prepare(**kwargs):
            sentinel.note_activity(request_started=True)
            return ({"candidate_pids": [123]}, (123,))

        with (
            mock.patch.object(
                process_tree, "prepare_verified_root_recovery", side_effect=prepare
            ),
            mock.patch.object(process_tree, "signal_verified_root_recovery") as deliver,
        ):
            stale = sentinel.recover_post_result(assessment)
        self.assertIsNone(stale.suspected)
        deliver.assert_not_called()

    def test_real_terminal_process_ignoring_sigterm_is_reaped(self) -> None:
        script = (
            "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);"
            "print('terminal-result',flush=True);time.sleep(60)"
        )
        with subprocess.Popen(
            [sys.executable, "-u", "-c", script], stdout=subprocess.PIPE, text=True
        ) as process:
            self.addCleanup(process_tree.clear_process_tree, process.pid)
            try:
                self.assertEqual("terminal-result\n", process.stdout.readline())
                sentinel = turn_execution_hang_sentinel.HangSentinel(
                    confirm_samples=1,
                    result_grace_seconds=0,
                    root_shutdown_grace_seconds=0,
                )
                sentinel.note_harness_pid(process.pid)
                sentinel.note_final_result()
                sentinel.evaluate(idle_seconds=120, now=self.NOW)
                term = sentinel.recover_post_result(
                    sentinel.evaluate(idle_seconds=150, now=self.NOW)
                )
                self.assertEqual(
                    [process.pid], term.suspected["recovery"]["signaled_pids"]
                )
                self.assertIsNone(process.poll())
                killed = sentinel.recover_post_result(
                    sentinel.evaluate(idle_seconds=180, now=self.NOW)
                )
                self.assertEqual("SIGKILL", killed.suspected["recovery"]["signal"])
                self.assertEqual(-signal.SIGKILL, process.wait(timeout=5))
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)

    def test_resumed_output_during_revalidation_cancels_signals(self) -> None:
        sentinel = self.make_sentinel()
        assessment = self.suspected_result(sentinel)

        def prepare(**kwargs):
            sentinel.note_activity()
            return ({"candidate_pids": [456]}, (456,))

        with (
            mock.patch.object(
                process_tree, "prepare_verified_leaf_recovery", side_effect=prepare
            ),
            mock.patch.object(process_tree, "signal_verified_leaf_recovery") as deliver,
        ):
            stale = sentinel.recover_post_result(assessment)

        self.assertIsNone(stale.suspected)
        deliver.assert_not_called()


if __name__ == "__main__":
    unittest.main()
