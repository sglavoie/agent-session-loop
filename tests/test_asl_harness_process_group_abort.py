"""Process-group sweeps and abort deadline/signal handling."""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    harness_process,
    harnesses,
    process_tree,
)
from tests.harness_integration_support import _PythonScriptCopilotAdapter
from tests.support import (
    assert_marked_pids_exited,
    kill_marked_pids,
    wait_until,
    write_process_tree_scripts,
)


class HarnessProcessGroupSweepTests(unittest.TestCase):
    """Each turn owns a process group, and nothing it spawned outlives it."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp_dir = Path(tmp.name)
        self.root = self.tmp_dir / "trees"
        _parent_script, self.grandchild_script = write_process_tree_scripts(self.root)
        # Registered after the tempdir cleanup so it runs first: a survivor is
        # killed while its PID marker still exists.
        self.addCleanup(kill_marked_pids, self.root / "grandchildren")

    def _fake_harness(self, *, detached: bool, tail: tuple[str, ...]) -> Path:
        """A harness that spawns one grandchild, waits for it, then runs ``tail``.

        ``detached`` makes the grandchild call ``setsid``, taking it out of the
        turn's process group and so out of reach of ``killpg``.
        """
        path = self.tmp_dir / "fake-copilot"
        path.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env python3",
                    "import pathlib, subprocess, sys, time",
                    f"root = pathlib.Path({str(self.root)!r})",
                    "subprocess.Popen(",
                    "    [",
                    "        sys.executable,",
                    f"        {str(self.grandchild_script)!r},",
                    "        'turn',",
                    "        str(root),",
                    "    ],",
                    f"    start_new_session={detached!r},",
                    ")",
                    "while not (root / 'grandchildren' / 'turn').exists():",
                    "    time.sleep(0.01)",
                    *tail,
                    "",
                ]
            ),
            encoding="utf-8",
        )
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def _grandchild_pid(self) -> int:
        return int((self.root / "grandchildren" / "turn").read_text(encoding="utf-8"))

    def _wait_for_grandchild_on_spawn(self, _pid: int) -> None:
        """Start the turn timeout only after the synthetic tree is live."""
        wait_until(
            (self.root / "grandchildren" / "turn").exists,
            timeout=30,
        )

    def test_lingering_in_group_child_is_swept_after_a_successful_turn(
        self,
    ) -> None:
        """The port-4173 shape at the harness layer: a turn that exits 0 dirty."""
        fake_harness = self._fake_harness(
            detached=False, tail=("print('turn done', flush=True)",)
        )
        spawn_pids: list[int] = []

        result = _PythonScriptCopilotAdapter(fake_harness).run(
            cwd=self.tmp_dir,
            plan_dir=self.tmp_dir,
            prompt="noop",
            on_spawn=spawn_pids.append,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("turn done", result.stdout)
        # start_new_session made the turn its own group leader, which is what
        # gives the sweep a positive identification to enumerate.
        self.assertEqual(
            spawn_pids, [result.process_sweep["enumeration"]["expected_pgid"]]
        )
        lingering = self._grandchild_pid()
        # It never left the group, so the pgid enumeration alone finds it.
        self.assertEqual(
            [lingering], result.process_sweep["enumeration"]["pgid_members"]
        )
        self.assertEqual([lingering], result.process_sweep["killed"])
        self.assertEqual([], result.process_sweep["survivors_after"])
        assert_marked_pids_exited(self.root / "grandchildren", label="lingering child")

    def test_timeout_kills_the_whole_process_group(self) -> None:
        fake_harness = self._fake_harness(detached=False, tail=("time.sleep(60)",))
        spawn_pids: list[int] = []

        def on_spawn(pid: int) -> None:
            spawn_pids.append(pid)
            self._wait_for_grandchild_on_spawn(pid)

        result = _PythonScriptCopilotAdapter(fake_harness).run(
            cwd=self.tmp_dir,
            plan_dir=self.tmp_dir,
            prompt="noop",
            # Keep process startup outside the timeout: heavily parallel suite
            # runs can otherwise expire before the synthetic tree publishes
            # its PID, leaving the timeout/sweep behavior unexercised.
            timeout=2,
            on_spawn=on_spawn,
        )

        self.assertTrue(result.timed_out)
        self.assertEqual(124, result.returncode)
        assert_marked_pids_exited(
            self.root / "grandchildren", label="in-group grandchild"
        )
        # The proof that the group died rather than just the direct child: a
        # grandchild that merely outlived its parent would have observed being
        # orphaned and written its late marker before any sweep reached it.
        self.assertEqual([], list((self.root / "late").iterdir()))
        # Nothing was left for the sweep, but it still reports on the group.
        self.assertEqual(
            spawn_pids, [result.process_sweep["enumeration"]["expected_pgid"]]
        )
        self.assertEqual([], result.process_sweep["enumeration"]["pgid_members"])
        self.assertEqual([], result.process_sweep["survivors_after"])

    def test_timeout_sweeps_a_setsid_escapee_through_the_pre_kill_snapshot(
        self,
    ) -> None:
        """`killpg` cannot reach an escapee; the snapshot taken before it can.

        Once the group kill lands the escapee is reparented to pid 1 and no
        descendant walk can reach it, so a snapshot taken while the turn's root
        was still alive is the sweep's only route to the 2h20m hang class.
        """
        fake_harness = self._fake_harness(detached=True, tail=("time.sleep(60)",))

        result = _PythonScriptCopilotAdapter(fake_harness).run(
            cwd=self.tmp_dir,
            plan_dir=self.tmp_dir,
            prompt="noop",
            timeout=2,
            on_spawn=self._wait_for_grandchild_on_spawn,
        )

        self.assertTrue(result.timed_out)
        escapee = self._grandchild_pid()
        # The late marker is the proof the escapee outlived the group kill: it
        # is only written once being orphaned changes its parent PID.
        self.assertEqual(["turn"], [p.name for p in (self.root / "late").iterdir()])
        # The group kill had already emptied the group, so the snapshot -- not
        # the pgid enumeration -- is what identified the escapee.
        self.assertEqual([], result.process_sweep["enumeration"]["pgid_members"])
        self.assertEqual(
            [escapee], result.process_sweep["enumeration"]["snapshot_verified"]
        )
        self.assertEqual([escapee], result.process_sweep["killed"])
        self.assertEqual([], result.process_sweep["survivors_after"])
        assert_marked_pids_exited(
            self.root / "grandchildren", label="detached grandchild"
        )

    def test_recorded_sample_sweeps_a_setsid_escapee_of_a_clean_turn(
        self,
    ) -> None:
        """The hang sentinel's sample is the only route left on a clean exit.

        A turn that exits on its own takes no pre-kill snapshot, and by then the
        escapee has been reparented to pid 1, so a descendant walk cannot reach
        it either. A tree recorded while the turn was still alive can.
        """
        gate = self.tmp_dir / "sampled"
        fake_harness = self._fake_harness(
            detached=True,
            tail=(
                f"gate = pathlib.Path({str(gate)!r})",
                "while not gate.exists():",
                "    time.sleep(0.01)",
                "print('turn done', flush=True)",
            ),
        )
        # The cache outlives this test; leaving a sample behind would let a
        # later sweep union a tree that has nothing to do with it.
        self.addCleanup(process_tree.record_process_tree, 0, ())
        turn_pids: list[int] = []

        def on_spawn(pid: int) -> None:
            turn_pids.append(pid)
            wait_until((self.root / "grandchildren" / "turn").exists, timeout=30)
            process_tree.record_process_tree(pid, process_tree.sample_process_tree(pid))
            gate.write_text("go", encoding="utf-8")

        result = _PythonScriptCopilotAdapter(fake_harness).run(
            cwd=self.tmp_dir,
            plan_dir=self.tmp_dir,
            prompt="noop",
            on_spawn=on_spawn,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("turn done", result.stdout)
        escapee = self._grandchild_pid()
        self.assertEqual([], result.process_sweep["enumeration"]["pgid_members"])
        self.assertEqual(
            [escapee], result.process_sweep["enumeration"]["snapshot_verified"]
        )
        self.assertEqual([escapee], result.process_sweep["killed"])
        self.assertEqual([], result.process_sweep["survivors_after"])
        # Keyed by the root the sample was taken for, so no other turn's sweep
        # can union it -- and consumed by that sweep, so a later turn handed the
        # same pid cannot union it either.
        self.assertEqual((), process_tree.last_process_tree(escapee))
        self.assertEqual((), process_tree.last_process_tree(turn_pids[0]))
        assert_marked_pids_exited(self.root / "grandchildren", label="sampled escapee")

    def _interrupting_popen(self, ready: Path):
        """Patch ``Popen`` so the first ``wait`` raises once ``ready`` exists.

        Reproduces a Ctrl-C landing mid-turn: the caller is inside
        ``process.wait`` when the interrupt arrives, so the harness and
        everything it spawned are still running.
        """
        real_popen = subprocess.Popen
        raised = threading.Event()

        def popen(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            original_wait = process.wait

            def wait(timeout=None):
                if not raised.is_set():
                    raised.set()
                    wait_until(ready.exists, timeout=30)
                    raise KeyboardInterrupt
                return original_wait(timeout=timeout)

            process.wait = wait
            return process

        return mock.patch.object(subprocess, "Popen", popen)

    def test_interrupt_path_stays_intact_and_sweeps_the_group(self) -> None:
        """Explicit signalling is unchanged; the sweep takes what it left."""
        fake_harness = self._fake_harness(detached=False, tail=("time.sleep(60)",))
        spawn_pids: list[int] = []
        ready = self.root / "grandchildren" / "turn"

        with self._interrupting_popen(ready):
            result = _PythonScriptCopilotAdapter(fake_harness).run(
                cwd=self.tmp_dir,
                plan_dir=self.tmp_dir,
                prompt="noop",
                on_spawn=spawn_pids.append,
            )

        # The interrupt contract: a 130 result flagged interrupted, not a
        # timeout and not a raised KeyboardInterrupt.
        self.assertEqual(130, result.returncode)
        self.assertTrue(result.interrupted)
        self.assertFalse(result.timed_out)
        with self.assertRaises(ProcessLookupError):
            os.kill(spawn_pids[0], 0)
        self.assertEqual(
            spawn_pids, [result.process_sweep["enumeration"]["expected_pgid"]]
        )
        # terminate() only signalled the direct child, so the grandchild was
        # still in the group when the sweep enumerated it.
        lingering = self._grandchild_pid()
        self.assertEqual(
            [lingering], result.process_sweep["enumeration"]["pgid_members"]
        )
        self.assertEqual([lingering], result.process_sweep["killed"])
        self.assertEqual([], result.process_sweep["survivors_after"])
        assert_marked_pids_exited(
            self.root / "grandchildren", label="in-group grandchild"
        )


class _StubWaitProcess:
    """A process that never exits and whose wait always expires after a sleep.

    Lets the two deadlines be compared at a tick that both have already passed,
    which no real timing can be relied on to produce.
    """

    def __init__(self, wait_seconds: float) -> None:
        self.wait_seconds = wait_seconds
        self.pid = os.getpid()

    def poll(self) -> None:
        return None

    def wait(self, timeout: float | None = None) -> int:
        time.sleep(self.wait_seconds)
        raise subprocess.TimeoutExpired(cmd="stub", timeout=timeout)


class HarnessAbortDeadlineTests(unittest.TestCase):
    """The wait loop's deadline arithmetic, with no real harness involved."""

    def test_unarmed_signal_has_no_expiry(self) -> None:
        signal_ = harnesses.AbortSignal()
        self.assertFalse(signal_.armed)
        self.assertIsNone(signal_.expires_at(time.monotonic()))

    def test_armed_signal_carries_its_grace_window_and_reason(self) -> None:
        signal_ = harnesses.AbortSignal()
        before = time.monotonic()
        signal_.arm("supervisor stop", grace_seconds=5.0)

        self.assertTrue(signal_.armed)
        self.assertEqual("supervisor stop", signal_.reason)
        self.assertGreaterEqual(signal_.expires_at(before), before + 5.0)

    def test_armed_without_a_deadline_is_due_at_once(self) -> None:
        signal_ = harnesses.AbortSignal()
        signal_.event.set()

        now = time.monotonic()
        self.assertEqual(now, signal_.expires_at(now))

    def test_earlier_timeout_wins_when_both_deadlines_have_passed(self) -> None:
        abort = harnesses.AbortSignal()
        abort.arm("supervisor stop", grace_seconds=0.1)

        expired = harness_process._wait_for_turn_exit(
            _StubWaitProcess(0.2), timeout=0.05, abort=abort
        )

        self.assertEqual("timeout", expired)

    def test_earlier_abort_wins_when_both_deadlines_have_passed(self) -> None:
        abort = harnesses.AbortSignal()
        abort.arm("supervisor stop", grace_seconds=0.05)

        expired = harness_process._wait_for_turn_exit(
            _StubWaitProcess(0.2), timeout=0.1, abort=abort
        )

        self.assertEqual("abort", expired)


class HarnessAbortSignalTests(unittest.TestCase):
    """An armed abort kills the turn's whole tree and is told from a timeout."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp_dir = Path(tmp.name)
        self.root = self.tmp_dir / "trees"
        _parent_script, self.grandchild_script = write_process_tree_scripts(self.root)
        self.addCleanup(kill_marked_pids, self.root / "grandchildren")
        self.started = self.tmp_dir / "harness-started"

    def _fake_harness(self, *, spawns: bool, detached: bool, tail: str) -> Path:
        """A harness that optionally fans out, publishes liveness, then waits."""
        spawn_lines = (
            [
                "subprocess.Popen(",
                "    [",
                "        sys.executable,",
                f"        {str(self.grandchild_script)!r},",
                "        'turn',",
                "        str(root),",
                "    ],",
                f"    start_new_session={detached!r},",
                ")",
                "while not (root / 'grandchildren' / 'turn').exists():",
                "    time.sleep(0.01)",
            ]
            if spawns
            else []
        )
        path = self.tmp_dir / "fake-copilot"
        path.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env python3",
                    "import pathlib, subprocess, sys, time",
                    f"root = pathlib.Path({str(self.root)!r})",
                    *spawn_lines,
                    f"pathlib.Path({str(self.started)!r}).write_text('go')",
                    tail,
                    "",
                ]
            ),
            encoding="utf-8",
        )
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def _arm_once_live(self, abort: harnesses.AbortSignal, *, grace: float):
        """Start the grace window only once the turn's tree is up.

        Mirrors the timeout tests: process startup under a heavily parallel
        suite run can otherwise eat the whole window.
        """

        def on_spawn(_pid: int) -> None:
            wait_until(self.started.exists, timeout=30)
            abort.arm("supervisor abort", grace_seconds=grace)

        return on_spawn

    def _grandchild_pid(self) -> int:
        return int((self.root / "grandchildren" / "turn").read_text(encoding="utf-8"))

    def test_abort_deadline_kills_the_turn_and_empties_its_group(self) -> None:
        fake_harness = self._fake_harness(
            spawns=False, detached=False, tail="time.sleep(60)"
        )
        abort = harnesses.AbortSignal()
        spawn_pids: list[int] = []
        arm = self._arm_once_live(abort, grace=0.5)

        def on_spawn(pid: int) -> None:
            spawn_pids.append(pid)
            arm(pid)

        result = _PythonScriptCopilotAdapter(fake_harness).run(
            cwd=self.tmp_dir,
            plan_dir=self.tmp_dir,
            prompt="noop",
            on_spawn=on_spawn,
            abort=abort,
        )

        self.assertTrue(result.aborted)
        self.assertFalse(result.timed_out)
        # 137, not the timeout path's 124: readable without the flags.
        self.assertEqual(137, result.returncode)
        with self.assertRaises(ProcessLookupError):
            os.killpg(spawn_pids[0], 0)

    def test_turn_that_exits_inside_the_window_is_never_signalled(self) -> None:
        fake_harness = self._fake_harness(
            spawns=False, detached=False, tail="print('turn done', flush=True)"
        )
        abort = harnesses.AbortSignal()
        abort.arm("supervisor abort", grace_seconds=60.0)

        result = _PythonScriptCopilotAdapter(fake_harness).run(
            cwd=self.tmp_dir,
            plan_dir=self.tmp_dir,
            prompt="noop",
            abort=abort,
        )

        self.assertFalse(result.aborted)
        self.assertFalse(result.timed_out)
        # A killed turn reports -SIGKILL, so a clean 0 is the proof that the
        # armed-but-unexpired signal never reached the process.
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("turn done", result.stdout)

    def test_abort_kills_the_whole_process_group_and_reports_the_sweep(
        self,
    ) -> None:
        fake_harness = self._fake_harness(
            spawns=True, detached=False, tail="time.sleep(60)"
        )
        abort = harnesses.AbortSignal()
        spawn_pids: list[int] = []
        arm = self._arm_once_live(abort, grace=0.5)

        def on_spawn(pid: int) -> None:
            spawn_pids.append(pid)
            arm(pid)

        result = _PythonScriptCopilotAdapter(fake_harness).run(
            cwd=self.tmp_dir,
            plan_dir=self.tmp_dir,
            prompt="noop",
            on_spawn=on_spawn,
            abort=abort,
        )

        self.assertTrue(result.aborted)
        assert_marked_pids_exited(
            self.root / "grandchildren", label="in-group grandchild"
        )
        # The group died as one: a grandchild that merely outlived its parent
        # would have seen itself orphaned and written its late marker first.
        self.assertEqual([], list((self.root / "late").iterdir()))
        self.assertEqual(
            spawn_pids, [result.process_sweep["enumeration"]["expected_pgid"]]
        )
        self.assertEqual([], result.process_sweep["enumeration"]["pgid_members"])
        self.assertEqual([], result.process_sweep["survivors_after"])

    def test_abort_sweeps_a_setsid_escapee_through_the_pre_kill_snapshot(
        self,
    ) -> None:
        """The abort path takes the same pre-kill snapshot the timeout path does."""
        fake_harness = self._fake_harness(
            spawns=True, detached=True, tail="time.sleep(60)"
        )
        abort = harnesses.AbortSignal()

        result = _PythonScriptCopilotAdapter(fake_harness).run(
            cwd=self.tmp_dir,
            plan_dir=self.tmp_dir,
            prompt="noop",
            on_spawn=self._arm_once_live(abort, grace=0.5),
            abort=abort,
        )

        self.assertTrue(result.aborted)
        escapee = self._grandchild_pid()
        # killpg emptied the group, so only a snapshot taken while the turn was
        # still alive could name the escapee.
        self.assertEqual([], result.process_sweep["enumeration"]["pgid_members"])
        self.assertEqual(
            [escapee], result.process_sweep["enumeration"]["snapshot_verified"]
        )
        self.assertEqual([escapee], result.process_sweep["killed"])
        self.assertEqual([], result.process_sweep["survivors_after"])
        assert_marked_pids_exited(
            self.root / "grandchildren", label="detached grandchild"
        )

    def test_timeout_first_sets_only_timed_out(self) -> None:
        fake_harness = self._fake_harness(
            spawns=False, detached=False, tail="time.sleep(60)"
        )
        abort = harnesses.AbortSignal()

        result = _PythonScriptCopilotAdapter(fake_harness).run(
            cwd=self.tmp_dir,
            plan_dir=self.tmp_dir,
            prompt="noop",
            timeout=2,
            on_spawn=self._arm_once_live(abort, grace=60.0),
            abort=abort,
        )

        self.assertTrue(result.timed_out)
        self.assertFalse(result.aborted)
        self.assertEqual(124, result.returncode)

    def test_abort_first_sets_only_aborted(self) -> None:
        fake_harness = self._fake_harness(
            spawns=False, detached=False, tail="time.sleep(60)"
        )
        abort = harnesses.AbortSignal()

        result = _PythonScriptCopilotAdapter(fake_harness).run(
            cwd=self.tmp_dir,
            plan_dir=self.tmp_dir,
            prompt="noop",
            timeout=60,
            on_spawn=self._arm_once_live(abort, grace=0.5),
            abort=abort,
        )

        self.assertTrue(result.aborted)
        self.assertFalse(result.timed_out)
        self.assertEqual(137, result.returncode)
