"""LiveWorkerTerminationTests coverage."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
import unittest

from agent_session_loop import (
    process_tree,
    redirect_control,
)
from tests.asl_redirect_control_support import (
    _WORKER_SOURCE,
    PATIENCE,
    _FakeCancellation,
)


@unittest.skipUnless(sys.platform in ("darwin", "linux"), "needs POSIX process groups")
class LiveWorkerTerminationTests(unittest.TestCase):
    """Real processes: a successor cannot run beside an uncollected predecessor."""

    def setUp(self) -> None:
        self.worker = subprocess.Popen(
            [sys.executable, "-c", _WORKER_SOURCE],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
        self.addCleanup(self._reap)
        line = self.worker.stdout.readline().strip()
        self.assertTrue(line.isdigit(), "worker never reported its grandchild")
        self.escapee = int(line)
        self.addCleanup(self._kill_escapee)

    def _kill_escapee(self) -> None:
        with contextlib.suppress(OSError):
            os.kill(self.escapee, signal.SIGKILL)

    def _reap(self) -> None:
        with contextlib.suppress(OSError):
            os.killpg(self.worker.pid, signal.SIGKILL)
        if self.worker.stdout is not None:
            self.worker.stdout.close()
        try:
            self.worker.wait(timeout=PATIENCE)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            self.worker.kill()
            self.worker.wait(timeout=PATIENCE)

    def _controller(self, cancellation) -> redirect_control.UrgentController:
        controller = redirect_control.UrgentController(
            worker=redirect_control.OwnedWorker.session_leader(self.worker.pid),
            cancellation=cancellation,
            # Long enough for a real ``ps`` to finish while the worker lives,
            # short enough to keep the test quick. The assertion on
            # ``collected`` below makes a lost race fail loudly rather than
            # silently weakening the case.
            window_seconds=0.5,
            poll_seconds=0.01,
            evidence_wait_seconds=PATIENCE,
            collection_wait_seconds=0.1,
        )
        self.addCleanup(controller.join, PATIENCE)
        return controller

    @staticmethod
    def _alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _wait_until_clear(self, controller, timeout: float = PATIENCE):
        deadline = time.monotonic() + timeout
        last: Exception | None = None
        while time.monotonic() < deadline:
            try:
                return controller.require_successor_allowed()
            except redirect_control.SuccessorBlockedError as exc:
                last = exc
                time.sleep(0.05)
        raise AssertionError(f"successor never cleared: {last}")

    def test_the_worker_group_and_its_escapee_are_both_stopped(self) -> None:
        cancellation = _FakeCancellation()
        controller = self._controller(cancellation)
        controller.arm(operation_id="op-live")
        controller.confirm()

        self.assertTrue(controller.wait_escalated(PATIENCE))
        report = controller.termination

        self.assertIsNotNone(report)
        self.assertTrue(report.forced)
        self.assertTrue(report.group_stop["signalled"])
        # The sample has to have happened while the worker was alive, or the
        # escapee below would be invisible to any descendant walk.
        self.assertTrue(report.evidence["collected"], report.evidence)
        self.assertIn(self.escapee, report.evidence["escaped_pids"])
        self.assertIn(self.escapee, report.cleanup["killed"])

        self._reap()
        deadline = time.monotonic() + PATIENCE
        while self._alive(self.escapee) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(self._alive(self.escapee))

    def test_a_successor_is_refused_while_the_predecessor_is_unreaped(self) -> None:
        cancellation = _FakeCancellation()
        controller = self._controller(cancellation)
        controller.arm(operation_id="op-live")
        controller.confirm()
        self.assertTrue(controller.wait_escalated(PATIENCE))

        # Signalled, dead, and still this process's child: the pid is not free
        # and the transport is not released until the owner waits on it.
        with self.assertRaises(redirect_control.SuccessorBlockedError) as caught:
            controller.require_successor_allowed()

        self.assertIn(
            caught.exception.reason,
            {
                "predecessor-uncollected",
                process_tree.INCOMPLETE_OWNERSHIP_UNKNOWN,
                process_tree.INCOMPLETE_GROUP_NOT_STOPPED,
            },
        )

    def test_the_successor_is_admitted_only_after_the_owner_collects(self) -> None:
        cancellation = _FakeCancellation()
        controller = self._controller(cancellation)
        controller.arm(operation_id="op-live")
        controller.confirm()
        self.assertTrue(controller.wait_escalated(PATIENCE))
        with self.assertRaises(redirect_control.SuccessorBlockedError):
            controller.require_successor_allowed()

        self._reap()
        cancellation.collect()

        report = self._wait_until_clear(controller)
        self.assertTrue(report.permits_continuation)
        self.assertIsNone(report.blocking_reason)
        self.assertEqual(process_tree.GROUP_STOPPED, report.confirmation["group"])

    def test_an_unreaped_group_leader_is_not_reported_as_stopped(self) -> None:
        # The mechanism the gate rests on, asserted directly: a killed but
        # uncollected leader keeps its process group answering signal 0.
        cancellation = _FakeCancellation(completed=True)
        controller = self._controller(cancellation)
        controller.arm(operation_id="op-live")
        controller.confirm()
        self.assertTrue(controller.wait_escalated(PATIENCE))

        report = controller.termination

        self.assertNotEqual(process_tree.GROUP_STOPPED, report.confirmation["group"])
        self.assertFalse(report.complete)


if __name__ == "__main__":
    unittest.main()
