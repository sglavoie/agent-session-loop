"""A connector stop waits for launchd to release the job before returning."""

from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from asl_workbench import lifecycle_common as common
from asl_workbench import lifecycle_native

ASL_TEST_PORTABLE = True


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class AwaitUnloadedTests(unittest.TestCase):
    def test_returns_once_the_job_is_gone(self) -> None:
        answers = iter([True, True, False])
        clock = _Clock()

        common.await_unloaded(
            lambda: next(answers), sleep=clock.sleep, monotonic=clock.monotonic
        )

        self.assertEqual(0.5, clock.now)

    def test_refuses_a_job_launchd_never_releases(self) -> None:
        clock = _Clock()

        with self.assertRaises(common.LifecycleError):
            common.await_unloaded(
                lambda: True, timeout=5, sleep=clock.sleep, monotonic=clock.monotonic
            )

        self.assertGreaterEqual(clock.now, 5)

    def test_outlasts_the_launch_agents_exit_timeout(self) -> None:
        self.assertGreater(
            common.UNLOAD_WAIT_SECONDS, common.CONNECTOR_EXIT_TIMEOUT_SECONDS
        )


class ConnectorStopTests(unittest.TestCase):
    def test_stop_returns_only_after_bootout_has_taken_effect(self) -> None:
        # launchctl bootout returns while the job is still loaded; a bootstrap
        # issued then fails with "Bootstrap failed: 5: Input/output error".
        loaded = iter([True, True, True, False])
        calls: list[list[str]] = []

        def command(argv: list[str], check: bool = True) -> subprocess.CompletedProcess:
            calls.append(argv)
            returncode = 0
            if argv[1] == "print":
                returncode = 0 if next(loaded) else 113
            return subprocess.CompletedProcess(argv, returncode, b"", b"")

        manager = lifecycle_native.NativeManager(common.ROLE_CONNECTOR)
        with (
            mock.patch.object(common, "command", side_effect=command),
            mock.patch.object(common.time, "sleep"),
        ):
            manager.stop()

        verbs = [argv[1] for argv in calls]
        self.assertEqual(["print", "bootout", "print", "print", "print"], verbs)


if __name__ == "__main__":
    unittest.main()
