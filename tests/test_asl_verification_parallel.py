"""Parallel verification tests."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    contracts,
    plans_session_ids,
    verification,
    verification_transitions,
)
from tests.support import (
    assert_marked_pids_exited,
    kill_marked_pids,
    write_process_tree_scripts,
)


class ParallelVerificationTests(unittest.TestCase):
    """Opt-in `parallel` recipes stay observationally identical to sequential ones."""

    #: Rendezvous on every peer having started, then finish after a per-check
    #: delay and append the check name to a shared completion log. A sequential
    #: executor can never clear the rendezvous, so it exits 1 on the deadline.
    RENDEZVOUS_SOURCE = (
        "import os, sys, time\n"
        "name, delay, root, total = sys.argv[1:5]\n"
        "started = os.path.join(root, 'started')\n"
        "os.makedirs(started, exist_ok=True)\n"
        "open(os.path.join(started, name), 'w').close()\n"
        "deadline = time.monotonic() + 10\n"
        "while len(os.listdir(started)) < int(total):\n"
        "    if time.monotonic() > deadline:\n"
        "        sys.exit(1)\n"
        "    time.sleep(0.01)\n"
        "time.sleep(float(delay))\n"
        "with open(os.path.join(root, 'order.txt'), 'a') as handle:\n"
        "    handle.write(name + '\\n')\n"
    )

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo_root = Path(self._tmp.name)
        self.session_id = plans_session_ids.parse_session_id("1")

    def _contract(
        self,
        checks: tuple[contracts.VerificationCheck, ...],
        *,
        parallel: bool,
    ) -> contracts.CompletionContract:
        return contracts.CompletionContract(
            session_id=self.session_id,
            done_when=("done",),
            verify=contracts.VerifyRecipe(checks=checks, parallel=parallel),
        )

    def _recording_observer(
        self,
    ) -> tuple[verification.VerificationObserver, list[tuple[str, str, str]]]:
        seen: list[tuple[str, str, str]] = []
        lock = threading.Lock()

        def started(event: verification.CheckStartedEvent) -> None:
            with lock:
                seen.append(("started", event.name, event.kind))

        def finished(event: verification.CheckFinishedEvent) -> None:
            with lock:
                seen.append(("finished", event.name, event.kind))

        observer = verification.VerificationObserver(
            on_check_started=started, on_check_finished=finished
        )
        return observer, seen

    def _rendezvous_checks(
        self, delays: tuple[float, ...]
    ) -> tuple[contracts.CommandCheck, ...]:
        script = self.repo_root / "rendezvous.py"
        script.write_text(self.RENDEZVOUS_SOURCE, encoding="utf-8")
        return tuple(
            contracts.CommandCheck(
                name=f"cmd-{index}",
                argv=(
                    "python3",
                    str(script),
                    f"cmd-{index}",
                    str(delay),
                    str(self.repo_root),
                    str(len(delays)),
                ),
                timeout_seconds=30.0,
            )
            for index, delay in enumerate(delays)
        )

    def test_command_checks_run_concurrently_and_report_in_declared_order(
        self,
    ) -> None:
        # Longest delay first, so real completion order is the reverse of the
        # declared order and any completion-ordered assembly would show it.
        checks = self._rendezvous_checks((0.4, 0.2, 0.0))
        observer, seen = self._recording_observer()

        result = verification.run_verification(
            self._contract(checks, parallel=True),
            repo_root=self.repo_root,
            observer=observer,
        )

        self.assertTrue(result.passed, [check.detail for check in result.checks])
        self.assertEqual(
            ["cmd-0", "cmd-1", "cmd-2"], [check.name for check in result.checks]
        )
        completion_order = (
            (self.repo_root / "order.txt").read_text(encoding="utf-8").split()
        )
        self.assertEqual(["cmd-2", "cmd-1", "cmd-0"], completion_order)
        self.assertEqual(
            {("started", f"cmd-{i}", "command") for i in range(3)}
            | {("finished", f"cmd-{i}", "command") for i in range(3)},
            set(seen),
        )

    def test_file_and_regex_checks_complete_before_commands_start(self) -> None:
        (self.repo_root / "README.md").write_text("hello\n", encoding="utf-8")
        checks = (
            self._rendezvous_checks((0.0, 0.0))[0],
            contracts.FileExistsCheck(name="readme", path="README.md"),
            self._rendezvous_checks((0.0, 0.0))[1],
            contracts.RegexInFileCheck(
                name="greeting", path="README.md", pattern="hello"
            ),
        )
        observer, seen = self._recording_observer()

        result = verification.run_verification(
            self._contract(checks, parallel=True),
            repo_root=self.repo_root,
            observer=observer,
        )

        self.assertTrue(result.passed, [check.detail for check in result.checks])
        self.assertEqual(
            ["cmd-0", "readme", "cmd-1", "greeting"],
            [check.name for check in result.checks],
        )
        # The non-command prefix is fully drained, in declared order, before
        # the first command check is dispatched.
        self.assertEqual(
            [
                ("started", "readme", "file_exists"),
                ("finished", "readme", "file_exists"),
                ("started", "greeting", "regex_in_file"),
                ("finished", "greeting", "regex_in_file"),
            ],
            seen[:4],
        )

    def test_single_command_check_recipe_stays_sequential(self) -> None:
        (self.repo_root / "README.md").write_text("hello\n", encoding="utf-8")
        checks = (
            contracts.CommandCheck(
                name="only-command",
                argv=("python3", "-c", "pass"),
                timeout_seconds=30.0,
            ),
            contracts.FileExistsCheck(name="readme", path="README.md"),
        )
        observer, seen = self._recording_observer()

        result = verification.run_verification(
            self._contract(checks, parallel=True),
            repo_root=self.repo_root,
            observer=observer,
        )

        self.assertTrue(result.passed)
        self.assertEqual(
            ["only-command", "readme"], [check.name for check in result.checks]
        )
        self.assertEqual(
            [
                ("started", "only-command", "command"),
                ("finished", "only-command", "command"),
                ("started", "readme", "file_exists"),
                ("finished", "readme", "file_exists"),
            ],
            seen,
        )

    def test_per_check_timeouts_are_enforced_under_parallel_dispatch(self) -> None:
        checks = (
            contracts.CommandCheck(
                name="sleeper",
                argv=("python3", "-c", "import time; time.sleep(30)"),
                timeout_seconds=0.2,
            ),
            contracts.CommandCheck(
                name="quick",
                argv=("python3", "-c", "pass"),
                timeout_seconds=30.0,
            ),
        )

        result = verification.run_verification(
            self._contract(checks, parallel=True), repo_root=self.repo_root
        )

        self.assertFalse(result.passed)
        self.assertEqual(["sleeper", "quick"], [check.name for check in result.checks])
        self.assertEqual("command-timeout", result.checks[0].failure_reason)
        self.assertEqual("pass", result.checks[1].status)

    def _failing_checks(self) -> tuple[contracts.CommandCheck, ...]:
        return tuple(
            contracts.CommandCheck(
                name=name,
                argv=(
                    "python3",
                    "-c",
                    f"import sys; sys.stdout.write({name!r}); sys.exit({code})",
                ),
                timeout_seconds=30.0,
            )
            for name, code in (("alpha", 3), ("beta", 4), ("gamma", 5))
        )

    def test_parallel_failures_match_sequential_details_and_fingerprint(self) -> None:
        checks = self._failing_checks()

        sequential = verification.run_verification(
            self._contract(checks, parallel=False), repo_root=self.repo_root
        )
        parallel = verification.run_verification(
            self._contract(checks, parallel=True), repo_root=self.repo_root
        )

        def details(
            result: verification.VerificationResult, parallel_flag: bool
        ) -> dict:
            payload = verification_transitions.verification_failure_details(
                result, self._contract(checks, parallel=parallel_flag)
            )
            for row in [*payload["checks"], *payload["failures"]]:
                row.pop("duration_seconds")
                # Per-execution evidence like timings: the sweep report names
                # the PIDs of *this* run, so it can never match across two.
                self.assertIsNotNone(row.pop("sweep"))
            return payload

        sequential_details = details(sequential, False)
        parallel_details = details(parallel, True)
        self.assertEqual(
            json.dumps(sequential_details, sort_keys=True),
            json.dumps(parallel_details, sort_keys=True),
        )
        self.assertEqual(
            verification_transitions.verification_fingerprint(sequential_details),
            verification_transitions.verification_fingerprint(parallel_details),
        )
        self.assertIsNotNone(
            verification_transitions.verification_fingerprint(parallel_details)
        )

    def test_keyboard_interrupt_cancels_pending_checks_and_re_raises(self) -> None:
        total = 6
        marker_dir = self.repo_root / "markers"
        marker_dir.mkdir()
        checks = tuple(
            contracts.CommandCheck(
                name=f"cmd-{index}",
                argv=(
                    "python3",
                    "-c",
                    "import pathlib, sys, time\n"
                    "pathlib.Path(sys.argv[1]).touch()\n"
                    "time.sleep(2)\n",
                    str(marker_dir / f"cmd-{index}"),
                ),
                timeout_seconds=10.0,
            )
            for index in range(total)
        )
        interrupted = threading.Event()

        def started(event: verification.CheckStartedEvent) -> None:
            if not interrupted.is_set():
                interrupted.set()
                raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            verification.run_verification(
                self._contract(checks, parallel=True),
                repo_root=self.repo_root,
                observer=verification.VerificationObserver(on_check_started=started),
            )

        # At most `max_workers` checks ever started, and the futures still
        # queued behind them were cancelled rather than executed.
        self.assertLess(len(list(marker_dir.iterdir())), total)

    #: Publish this worker's PID under `started/` (via an atomic rename, so the
    #: marker never appears half-written), sleep far longer than the interrupt
    #: window, then record completion under `done/`. A `done` marker or a live
    #: PID after the interrupt means the process outlived the cancellation.
    LONG_SLEEP_SOURCE = (
        "import os, pathlib, sys, time\n"
        "name, root = sys.argv[1], pathlib.Path(sys.argv[2])\n"
        "pending = root / 'pending' / name\n"
        "pending.write_text(str(os.getpid()))\n"
        "os.replace(pending, root / 'started' / name)\n"
        "time.sleep(60)\n"
        "(root / 'done' / name).write_text('completed')\n"
    )

    def test_interrupt_terminates_running_command_processes(self) -> None:
        root = self.repo_root / "interrupt"
        for name in ("pending", "started", "done"):
            (root / name).mkdir(parents=True)
        artifact_dir = self.repo_root / "artifacts"
        workers = 4
        total = workers + 2
        checks = tuple(
            contracts.CommandCheck(
                name=f"cmd-{index}",
                argv=(
                    "python3",
                    "-c",
                    self.LONG_SLEEP_SOURCE,
                    f"cmd-{index}",
                    str(root),
                ),
                timeout_seconds=120.0,
            )
            for index in range(total)
        )
        finished: list[str] = []
        observer = verification.VerificationObserver(
            on_check_finished=lambda event: finished.append(event.name)
        )

        def interrupt_once_running(futures: object) -> None:
            # Stands in for a Ctrl-C on the thread driving the dispatch, timed
            # so that every worker is blocked inside a live command process.
            deadline = time.monotonic() + 30
            while len(list((root / "started").iterdir())) < workers:
                if time.monotonic() > deadline:
                    raise AssertionError("command checks never started")
                time.sleep(0.01)
            raise KeyboardInterrupt

        start = time.monotonic()
        with (
            mock.patch.object(verification, "as_completed", interrupt_once_running),
            self.assertRaises(KeyboardInterrupt),
        ):
            verification.run_verification(
                self._contract(checks, parallel=True),
                repo_root=self.repo_root,
                observer=observer,
                artifact_dir=artifact_dir,
            )
        elapsed = time.monotonic() - start

        started = sorted((root / "started").iterdir())
        # Every worker slot was occupied, and the checks queued behind them
        # were cancelled before they could spawn anything.
        self.assertEqual(workers, len(started))
        # Control returned long before the commands would have exited on their
        # own (60s sleep) or hit their 120s check timeout.
        self.assertLess(elapsed, 30.0)
        for marker in started:
            pid = int(marker.read_text(encoding="utf-8"))
            with self.assertRaises(
                ProcessLookupError, msg=f"{marker.name} survived the interrupt"
            ):
                os.kill(pid, 0)
        # Nothing an abandoned check would have published exists: no completion
        # side effect, no failure artifact, no finished callback.
        self.assertEqual([], sorted((root / "done").iterdir()))
        self.assertFalse(artifact_dir.exists())
        self.assertEqual([], finished)

    def test_interrupt_terminates_whole_command_process_groups(self) -> None:
        root = self.repo_root / "trees"
        parent_script, grandchild_script = write_process_tree_scripts(root)
        artifact_dir = self.repo_root / "artifacts"
        total = 3  # Below the four-worker cap, so every tree is live at once.
        checks = tuple(
            contracts.CommandCheck(
                name=f"cmd-{index}",
                argv=(
                    "python3",
                    str(parent_script),
                    f"cmd-{index}",
                    str(root),
                    str(grandchild_script),
                ),
                timeout_seconds=120.0,
            )
            for index in range(total)
        )
        finished: list[str] = []
        observer = verification.VerificationObserver(
            on_check_finished=lambda event: finished.append(event.name)
        )

        def interrupt_once_trees_are_live(_futures: object) -> None:
            # A `started/` marker means that check's grandchild is live too, so
            # the interrupt lands with every process of every tree running.
            deadline = time.monotonic() + 30
            while len(list((root / "started").iterdir())) < total:
                if time.monotonic() > deadline:
                    raise AssertionError("process trees never started")
                time.sleep(0.01)
            raise KeyboardInterrupt

        start = time.monotonic()
        with (
            mock.patch.object(
                verification, "as_completed", interrupt_once_trees_are_live
            ),
            self.assertRaises(KeyboardInterrupt),
        ):
            verification.run_verification(
                self._contract(checks, parallel=True),
                repo_root=self.repo_root,
                observer=observer,
                artifact_dir=artifact_dir,
            )
        elapsed = time.monotonic() - start

        # Control returned without waiting for the 60s sleeps, and without a
        # grandchild's inherited pipe holding the worker joins open.
        self.assertLess(elapsed, 30.0)
        self.assertEqual(total, len(list((root / "started").iterdir())))
        self.assertEqual(total, len(list((root / "grandchildren").iterdir())))
        assert_marked_pids_exited(root / "started", label="command process")
        assert_marked_pids_exited(root / "grandchildren", label="grandchild")
        # Every process that stayed in the group is gone, so the late side
        # effect a survivor writes on being orphaned cannot appear. The
        # detached case is the sibling test below.
        self.assertEqual([], list((root / "late").iterdir()))
        self.assertEqual([], finished)
        self.assertFalse(artifact_dir.exists())

    def test_interrupt_sweeps_detached_descendants(self) -> None:
        """Cancellation reaches `setsid` escapees through the pre-kill snapshot.

        The kill runs on the interrupted thread rather than in the workers, so
        the snapshot has to be taken there too -- once the group kill lands the
        escapee is reparented to pid 1 and its owning worker can no longer walk
        to it. Everything else about cancellation is unchanged: control returns
        bounded, and the abandoned checks publish no result, artifact, or event.
        """
        root = self.repo_root / "trees"
        parent_script, grandchild_script = write_process_tree_scripts(root)
        artifact_dir = self.repo_root / "artifacts"
        self.addCleanup(kill_marked_pids, root / "grandchildren")
        total = 3  # Below the four-worker cap, so every tree is live at once.
        checks = tuple(
            contracts.CommandCheck(
                name=f"cmd-{index}",
                argv=(
                    "python3",
                    str(parent_script),
                    f"cmd-{index}",
                    str(root),
                    str(grandchild_script),
                    "detached",
                ),
                timeout_seconds=120.0,
            )
            for index in range(total)
        )
        finished: list[str] = []
        observer = verification.VerificationObserver(
            on_check_finished=lambda event: finished.append(event.name)
        )

        def interrupt_once_trees_are_live(_futures: object) -> None:
            deadline = time.monotonic() + 30
            while len(list((root / "started").iterdir())) < total:
                if time.monotonic() > deadline:
                    raise AssertionError("process trees never started")
                time.sleep(0.01)
            raise KeyboardInterrupt

        start = time.monotonic()
        with (
            mock.patch.object(
                verification, "as_completed", interrupt_once_trees_are_live
            ),
            self.assertRaises(KeyboardInterrupt),
        ):
            verification.run_verification(
                self._contract(checks, parallel=True),
                repo_root=self.repo_root,
                observer=observer,
                artifact_dir=artifact_dir,
            )
        elapsed = time.monotonic() - start

        self.assertEqual(total, len(list((root / "started").iterdir())))
        self.assertEqual(total, len(list((root / "grandchildren").iterdir())))
        assert_marked_pids_exited(root / "started", label="command process")
        # Every escapee outlived the group kill -- the late marker is written
        # only once being orphaned changes its parent PID -- and was then swept.
        self.assertEqual(total, len(list((root / "late").iterdir())))
        assert_marked_pids_exited(root / "grandchildren", label="detached grandchild")
        # An escaped descendant holds the captured pipes open, so cancellation
        # pays the bounded reader join instead of returning immediately -- but
        # it is still bounded, not the escapee's full 60s lifetime.
        self.assertLess(elapsed, 30.0)
        self.assertEqual([], finished)
        self.assertFalse(artifact_dir.exists())
