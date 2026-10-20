"""The bounded commit subprocess: generation bounds and the outer timeout backstop."""

from __future__ import annotations

import argparse
import contextlib
import os
import signal
import sys
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    commit_message,
    run_commit,
    run_commit_process,
)
from tests.asl_turn_commit_support import TurnCommitRunCase
from tests.support import (
    GENERATION_TIMEOUT_PROBE_SECONDS,
    fake_commit_harness_env,
    wait_for_process_exit,
)


class TurnCommitSubprocessTests(TurnCommitRunCase):
    """Bounds and cleanup for the nested commit command."""

    def test_runner_commit_generation_is_bounded(self) -> None:
        # Every run-owned commit carries a bound, including the default
        # selection a run that named no harness produces: an unbounded nested
        # commit is what turns one stalled message harness into a stalled loop.
        default_argv = run_commit._asl_commit_argv(run_commit._CommitGeneration())
        self.assertIn("--timeout", default_argv)
        self.assertEqual(
            str(commit_message.DEFAULT_GENERATION_TIMEOUT_SECONDS),
            default_argv[default_argv.index("--timeout") + 1],
        )
        # The run's own `--timeout` bounds a session turn and is routinely
        # unset, so it must not be what the commit bound depends on.
        unset = argparse.Namespace(harness="codex", timeout=None, model=None)
        self.assertEqual(
            commit_message.DEFAULT_GENERATION_TIMEOUT_SECONDS,
            run_commit._commit_generation(unset).timeout,
        )

        repo = self.repositories["SVC"]
        (repo / "declared.txt").write_text("session work\n", encoding="utf-8")
        pid_path = self.tmp_path / "hang-child.pid"
        env = fake_commit_harness_env(self.tmp_path)
        env["FAKE_CODEX_COMMIT_MODE"] = "hang"
        env["FAKE_CODEX_COMMIT_HANG_CHILD_PID"] = str(pid_path)

        started = time.monotonic()
        with mock.patch.dict(os.environ, env):
            commit = run_commit._run_asl_commit(
                cwd=repo,
                run_dir=self.tmp_path / "bounded-run",
                commit_index=1,
                session_id="1",
                repository_alias="SVC",
                qualified_paths=["SVC:declared.txt"],
                staging_paths=["SVC:declared.txt"],
                generation=run_commit._CommitGeneration(
                    harness="codex", timeout=GENERATION_TIMEOUT_PROBE_SECONDS
                ),
            )
        elapsed = time.monotonic() - started

        # The nested command enforced its own bound and exited with a
        # classified failure, so the run never reached its outer backstop.
        self.assertLess(elapsed, 120, "the nested commit outlived its bound")
        argv = list(commit["argv"])
        self.assertEqual(
            str(GENERATION_TIMEOUT_PROBE_SECONDS),
            argv[argv.index("--timeout") + 1],
        )
        self.assertEqual(
            GENERATION_TIMEOUT_PROBE_SECONDS, commit["generation_timeout_seconds"]
        )
        self.assertFalse(commit["timed_out"])
        self.assertNotEqual(0, commit["returncode"])
        self.assertEqual("nonzero-exit", commit["status_result"])
        self.assertFalse(commit["success"])
        stderr = Path(str(commit["stderr_path"])).read_text(encoding="utf-8")
        self.assertIn(
            "did not produce a commit message within "
            f"{GENERATION_TIMEOUT_PROBE_SECONDS}s",
            stderr,
        )
        # Nothing was committed, and the stalled harness's whole process tree
        # went down with the bound rather than outliving the run.
        self.assertEqual(commit["head_before"], commit["head_after"])
        child_pid = int(pid_path.read_text(encoding="utf-8").strip())
        self.assertFalse(
            wait_for_process_exit(child_pid),
            "a harness descendant outlived the nested commit bound",
        )

    def test_a_wedged_commit_subprocess_is_killed_by_the_outer_backstop(self) -> None:
        # The nested bound cannot cover a commit process that never reaches its
        # own timeout logic, so the run bounds the subprocess as well. The stub
        # stands in for that: it spawns a helper into the same process group and
        # then sleeps past anything the test would wait for.
        repo = self.repositories["SVC"]
        pid_path = self.tmp_path / "wedged-child.pid"
        stub = [
            sys.executable,
            "-c",
            "import subprocess, sys, time;"
            "child = subprocess.Popen([sys.executable, '-c',"
            " 'import time; time.sleep(600)']);"
            f"open({str(pid_path)!r}, 'w').write(str(child.pid));"
            "time.sleep(600)",
        ]

        started = time.monotonic()
        with (
            mock.patch.object(run_commit, "_asl_commit_argv", return_value=stub),
            mock.patch.object(run_commit, "COMMIT_SUBPROCESS_GRACE_SECONDS", 1),
        ):
            commit = run_commit._run_asl_commit(
                cwd=repo,
                run_dir=self.tmp_path / "wedged-run",
                commit_index=1,
                session_id="1",
                repository_alias="SVC",
                generation=run_commit._CommitGeneration(harness="codex", timeout=1),
            )
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 120, "the commit subprocess outlived its bound")
        self.assertTrue(commit["timed_out"])
        # 124 is `run_harness`'s timeout code, so a bounded-out commit reads as
        # an ordinary nonzero commit exit everywhere downstream.
        self.assertEqual(124, commit["returncode"])
        self.assertEqual("nonzero-exit", commit["status_result"])
        self.assertFalse(commit["success"])
        self.assertEqual(2, commit["subprocess_timeout_seconds"])
        stderr = Path(str(commit["stderr_path"])).read_text(encoding="utf-8")
        self.assertIn("exceeded its 2s bound", stderr)
        child_pid = int(pid_path.read_text(encoding="utf-8").strip())
        self.assertFalse(
            wait_for_process_exit(child_pid),
            "a commit-subprocess descendant outlived the outer backstop",
        )

    def test_outer_commit_timeout_kills_detached_descendants(self) -> None:
        # The real nested harness starts a session of its own, so a group kill
        # aimed at the commit subprocess never reaches it. The stub models that
        # exactly: the helper spawns with `start_new_session=True`, leaving the
        # group the backstop signals while still holding the captured pipes —
        # which is also what makes the drain time out rather than return.
        repo = self.repositories["SVC"]
        pid_path = self.tmp_path / "detached-child.pid"
        stub = [
            sys.executable,
            "-c",
            "import subprocess, sys, time;"
            "child = subprocess.Popen([sys.executable, '-c',"
            " 'import time; time.sleep(600)'], start_new_session=True);"
            f"open({str(pid_path)!r}, 'w').write(str(child.pid));"
            "time.sleep(600)",
        ]

        started = time.monotonic()
        with (
            mock.patch.object(run_commit, "_asl_commit_argv", return_value=stub),
            mock.patch.object(run_commit, "COMMIT_SUBPROCESS_GRACE_SECONDS", 1),
        ):
            commit = run_commit._run_asl_commit(
                cwd=repo,
                run_dir=self.tmp_path / "detached-run",
                commit_index=1,
                session_id="1",
                repository_alias="SVC",
                generation=run_commit._CommitGeneration(harness="codex", timeout=1),
            )
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 120, "the commit subprocess outlived its bound")
        # The escaped descendant changes nothing about how the expiry reports:
        # still exit 124, still the ordinary soft-failure path.
        self.assertTrue(commit["timed_out"])
        self.assertEqual(124, commit["returncode"])
        self.assertEqual("nonzero-exit", commit["status_result"])
        self.assertFalse(commit["success"])
        stderr = Path(str(commit["stderr_path"])).read_text(encoding="utf-8")
        self.assertIn("exceeded its 2s bound", stderr)

        child_pid = int(pid_path.read_text(encoding="utf-8").strip())
        self.assertFalse(
            wait_for_process_exit(child_pid),
            "a detached commit descendant outlived the outer backstop",
        )
        # The group kill could not have reached it, so the sweep is what did:
        # assert on the report rather than only on the process being gone.
        sweep = commit["process_sweep"]
        self.assertIsNotNone(sweep, "the backstop recorded no descendant sweep")
        self.assertIn(child_pid, sweep["killed"])
        # The child is reaped before the sweep, so the group route is available
        # rather than skipped as a stranger's — and it still does not list the
        # detached descendant. The pre-kill snapshot is the only route to it.
        self.assertNotIn("pgid_skipped", sweep["enumeration"])
        self.assertNotIn(child_pid, sweep["enumeration"]["pgid_members"])

    def test_outer_commit_timeout_kills_descendants_forked_after_the_snapshot(
        self,
    ) -> None:
        # The test above forks its detached helper before the bound expires, so
        # the pre-kill snapshot names it. A real harness keeps spawning, and a
        # child born after that snapshot is in no snapshot and — being detached
        # — in no process group either. Releasing the fork from inside the
        # snapshot call is what makes the ordering a fact rather than a race:
        # the late child provably cannot exist until the snapshot is taken.
        repo = self.repositories["SVC"]
        early_pid_path = self.tmp_path / "early-detached.pid"
        late_pid_path = self.tmp_path / "late-detached.pid"
        release_path = self.tmp_path / "fork-now"
        forker = self.tmp_path / "late_forker.py"
        forker.write_text(
            "import os, subprocess, sys, time\n"
            "release, late_pid_path = sys.argv[1], sys.argv[2]\n"
            "while not os.path.exists(release):\n"
            "    time.sleep(0.01)\n"
            "child = subprocess.Popen(\n"
            "    [sys.executable, '-c', 'import time; time.sleep(600)'],\n"
            "    start_new_session=True,\n"
            ")\n"
            "open(late_pid_path, 'w').write(str(child.pid))\n"
            "time.sleep(600)\n",
            encoding="utf-8",
        )
        stub = [
            sys.executable,
            "-c",
            "import subprocess, sys, time;"
            "child = subprocess.Popen("
            "[sys.executable, sys.argv[1], sys.argv[2], sys.argv[3]],"
            " start_new_session=True);"
            "open(sys.argv[4], 'w').write(str(child.pid));"
            "time.sleep(600)",
            str(forker),
            str(release_path),
            str(late_pid_path),
            str(early_pid_path),
        ]
        self.addCleanup(self._reap_recorded_pids, early_pid_path, late_pid_path)

        take_snapshot = run_commit_process._snapshot_commit_descendants
        captured: dict[str, Any] = {}

        def fork_after_snapshot(root_pid: int) -> tuple[Any, ...]:
            recorded = take_snapshot(root_pid)
            captured["snapshot_pids"] = [entry["pid"] for entry in recorded]
            release_path.write_text("go", encoding="utf-8")
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and not late_pid_path.exists():
                time.sleep(0.01)
            return recorded

        started = time.monotonic()
        with (
            mock.patch.object(run_commit, "_asl_commit_argv", return_value=stub),
            mock.patch.object(run_commit, "COMMIT_SUBPROCESS_GRACE_SECONDS", 1),
            mock.patch.object(
                run_commit_process, "_snapshot_commit_descendants", fork_after_snapshot
            ),
        ):
            commit = run_commit._run_asl_commit(
                cwd=repo,
                run_dir=self.tmp_path / "late-fork-run",
                commit_index=1,
                session_id="1",
                repository_alias="SVC",
                generation=run_commit._CommitGeneration(harness="codex", timeout=1),
            )
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 120, "the commit subprocess outlived its bound")
        # The late fork changes nothing about how the expiry reports: still
        # exit 124, still the ordinary soft-failure path.
        self.assertTrue(commit["timed_out"])
        self.assertEqual(124, commit["returncode"])
        self.assertEqual("nonzero-exit", commit["status_result"])
        self.assertFalse(commit["success"])

        self.assertTrue(late_pid_path.exists(), "the late child never forked")
        early_pid = int(early_pid_path.read_text(encoding="utf-8").strip())
        late_pid = int(late_pid_path.read_text(encoding="utf-8").strip())
        # The premise of the test, asserted rather than assumed: the snapshot
        # named the parent and could not have named the child.
        self.assertIn(early_pid, captured["snapshot_pids"])
        self.assertNotIn(late_pid, captured["snapshot_pids"])

        sweep = commit["process_sweep"]
        self.assertIsNotNone(sweep, "the backstop recorded no descendant sweep")
        # Neither of the two pre-existing routes could reach the late child, so
        # walking the live ancestry of a snapshot-verified process is what did.
        self.assertNotIn(late_pid, sweep["enumeration"]["pgid_members"])
        self.assertNotIn(late_pid, sweep["enumeration"]["snapshot_verified"])
        self.assertIn(late_pid, sweep["enumeration"]["late_descendants"])
        self.assertIn(late_pid, sweep["killed"])
        # `survivors_after` is re-sampled inside the sweep, so an empty entry
        # for the late child proves it was gone before the call returned.
        self.assertNotIn(late_pid, sweep["survivors_after"])
        self.assertFalse(
            wait_for_process_exit(late_pid),
            "a descendant forked after the snapshot outlived the outer backstop",
        )
        self.assertFalse(
            wait_for_process_exit(early_pid),
            "a detached commit descendant outlived the outer backstop",
        )

    def _reap_recorded_pids(self, *pid_paths: Path) -> None:
        """Kill whatever a sweep regression left behind when it fails.

        The stubs sleep for ten minutes, so a failing assertion would otherwise
        leak processes into the rest of the suite rather than just reporting.
        """
        for pid_path in pid_paths:
            if not pid_path.exists():
                continue
            try:
                pid = int(pid_path.read_text(encoding="utf-8").strip())
            except ValueError:
                continue
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)


if __name__ == "__main__":
    unittest.main()
