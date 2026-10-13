"""Verification executor, verify CLI, and artifact storage tests.

Split from tests/test_agent_session_loop.py."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    contracts,
    plans,
    process_tree,
    run_persistence,
    verification,
)
from tests.support import (
    assert_marked_pids_alive,
    assert_marked_pids_exited,
    kill_marked_pids,
    write_process_tree_scripts,
)
from tests.support_asl_verification import (
    VerificationExecutorFixtures,
    write_detached_spawner_script,
)


class CommandCheckExecutionTests(VerificationExecutorFixtures):
    def test_command_check_passes_when_argv_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            check = contracts.CommandCheck(
                name="echo",
                argv=("python3", "-c", "import sys; sys.exit(0)"),
                timeout_seconds=10.0,
            )
            result = verification.run_check(check, repo_root=repo_root)

            self.assertEqual("pass", result.status)
            self.assertEqual("echo", result.name)
            self.assertEqual("command", result.kind)
            self.assertEqual(0, result.returncode)
            self.assertIsNone(result.failure_reason)

    def test_command_check_fails_on_nonzero_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            check = contracts.CommandCheck(
                name="exit-1",
                argv=("python3", "-c", "import sys; sys.exit(7)"),
                timeout_seconds=10.0,
            )
            result = verification.run_check(check, repo_root=repo_root)

            self.assertEqual("fail", result.status)
            self.assertEqual("command-failed", result.failure_reason)
            self.assertEqual(7, result.returncode)
            self.assertIn("exited 7", result.detail)

    def test_command_check_fails_on_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            check = contracts.CommandCheck(
                name="sleeper",
                argv=("python3", "-c", "import time; time.sleep(5)"),
                timeout_seconds=0.1,
            )
            result = verification.run_check(check, repo_root=repo_root)

            self.assertEqual("fail", result.status)
            self.assertEqual("command-timeout", result.failure_reason)
            self.assertIsNone(result.returncode)
            self.assertIn("timed out", result.detail)

    def test_command_timeout_terminates_the_whole_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parent_script, grandchild_script = write_process_tree_scripts(root)
            check = contracts.CommandCheck(
                name="tree",
                argv=(
                    "python3",
                    str(parent_script),
                    "tree",
                    str(root),
                    str(grandchild_script),
                ),
                # Ample time for both processes to spawn, far below their sleeps.
                timeout_seconds=2.0,
            )

            result = verification.run_check(check, repo_root=root)

            self.assertEqual("command-timeout", result.failure_reason)
            # The timeout landed on a fully live tree, not on a half-spawned one.
            self.assertEqual(["tree"], [p.name for p in (root / "started").iterdir()])
            self.assertEqual(
                ["tree"], [p.name for p in (root / "grandchildren").iterdir()]
            )
            assert_marked_pids_exited(root / "started", label="command process")
            assert_marked_pids_exited(root / "grandchildren", label="grandchild")
            self.assertEqual([], list((root / "late").iterdir()))

    def test_command_timeout_sweeps_detached_descendants(self) -> None:
        """`killpg` cannot reach a `setsid` escapee; the pre-kill snapshot can.

        The escapee leaves the check's process group, so the group kill misses
        it and it survives long enough to write its orphaned side effect. What
        finds it afterwards is the descendant snapshot taken while the check's
        root was still alive -- once the root is dead the escapee is reparented
        to pid 1 and no walk can reach it.
        """
        # The tempdir is torn down through addCleanup rather than a `with`
        # block so a surviving escapee is killed while its PID marker still
        # exists: cleanups run last-registered-first.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        parent_script, grandchild_script = write_process_tree_scripts(root)
        self.addCleanup(kill_marked_pids, root / "grandchildren")
        check = contracts.CommandCheck(
            name="tree",
            argv=(
                "python3",
                str(parent_script),
                "tree",
                str(root),
                str(grandchild_script),
                "detached",
            ),
            # Ample time for both processes to spawn, far below their sleeps.
            timeout_seconds=2.0,
        )

        result = verification.run_check(check, repo_root=root)

        self.assertEqual("command-timeout", result.failure_reason)
        self.assertEqual(["tree"], [p.name for p in (root / "started").iterdir()])
        self.assertEqual(["tree"], [p.name for p in (root / "grandchildren").iterdir()])
        assert_marked_pids_exited(root / "started", label="command process")
        escapee = int((root / "grandchildren" / "tree").read_text(encoding="utf-8"))
        # The late marker is the proof the escapee outlived the group kill: it
        # is only written once being orphaned changes its parent PID.
        self.assertEqual(["tree"], [p.name for p in (root / "late").iterdir()])
        self.assertEqual([escapee], result.sweep["killed"])
        self.assertEqual([], result.sweep["survivors_after"])
        # The group kill had already emptied the group, so the snapshot -- not
        # the pgid enumeration -- is what identified the escapee.
        self.assertEqual([], result.sweep["enumeration"]["pgid_members"])
        self.assertEqual([escapee], result.sweep["enumeration"]["snapshot_verified"])
        assert_marked_pids_exited(root / "grandchildren", label="detached grandchild")

    def test_command_timeout_reports_a_sweep_for_a_group_that_died_with_it(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parent_script, grandchild_script = write_process_tree_scripts(root)
            check = contracts.CommandCheck(
                name="tree",
                argv=(
                    "python3",
                    str(parent_script),
                    "tree",
                    str(root),
                    str(grandchild_script),
                ),
                timeout_seconds=2.0,
            )

            result = verification.run_check(check, repo_root=root)

            self.assertEqual("command-timeout", result.failure_reason)
            assert_marked_pids_exited(root / "grandchildren", label="grandchild")
            # Nothing outlived the group kill, so the sweep still reports --
            # it just has nothing left to do.
            self.assertEqual([], result.sweep["survivors_after"])
            self.assertEqual([], result.sweep["enumeration"]["pgid_members"])

    def test_exited_check_sweeps_a_leftover_same_group_process(self) -> None:
        """The port-4173 shape: a background process the check never reaped."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            check = contracts.CommandCheck(
                name="leftover",
                argv=("sh", "-c", self.LEFTOVER_COMMAND),
                timeout_seconds=30.0,
            )

            result = verification.run_check(check, repo_root=root)

            self.assertEqual("pass", result.status)
            sleeper = int((root / "sleepers" / "leftover").read_text(encoding="utf-8"))
            # It never left the group, so the pgid enumeration alone finds it.
            self.assertEqual([sleeper], result.sweep["enumeration"]["pgid_members"])
            self.assertEqual([sleeper], result.sweep["killed"])
            self.assertEqual([], result.sweep["survivors_after"])
            assert_marked_pids_exited(root / "sleepers", label="leftover sleeper")

    def test_exited_check_does_not_hang_on_a_detached_descendant(self) -> None:
        """The 2h20m hang shape, bounded.

        An escapee that inherited the captured pipes keeps the reader threads
        from ever seeing EOF. A check that exits 0 is never killed, so nothing
        else closes those pipes: without a bound on the join the check waits
        for the escapee's whole lifetime. The bound is the fix here -- the
        sweep that follows is pgid-only on this path and does not reach it.
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        script = write_detached_spawner_script(root)
        self.addCleanup(kill_marked_pids, root / "escapees")
        check = contracts.CommandCheck(
            name="escapee",
            argv=("python3", str(script), str(root)),
            timeout_seconds=120.0,
        )

        # Real bound is 30s; the assertion is that the join ends at the bound
        # rather than at the escapee's 120s sleep.
        with mock.patch.object(verification, "_EXITED_READER_JOIN_SECONDS", 1.0):
            start = time.monotonic()
            result = verification.run_check(check, repo_root=root)
            elapsed = time.monotonic() - start

        self.assertEqual("pass", result.status)
        self.assertLess(elapsed, 30.0)
        self.assertEqual(["escapee"], [p.name for p in (root / "escapees").iterdir()])
        assert_marked_pids_alive(root / "escapees", label="detached descendant")
        self.assertEqual([], result.sweep["enumeration"]["pgid_members"])
        self.assertEqual([], result.sweep["killed"])

    def test_sweep_opt_out_leaves_a_leftover_process_alone(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.addCleanup(kill_marked_pids, root / "sleepers")
        check = contracts.CommandCheck(
            name="leftover",
            argv=("sh", "-c", self.LEFTOVER_COMMAND),
            timeout_seconds=30.0,
        )

        with mock.patch.dict(os.environ, {process_tree.SWEEP_DISABLED_ENV_VAR: "1"}):
            result = verification.run_check(check, repo_root=root)

        self.assertEqual("pass", result.status)
        # No report at all, and the leftover is exactly where it was.
        self.assertIsNone(result.sweep)
        assert_marked_pids_alive(root / "sleepers", label="leftover sleeper")

    def test_sequential_interrupt_sweeps_detached_descendant(self) -> None:
        """A Ctrl-C during a sequential recipe still reaches a `setsid` escapee.

        The interrupt lands on the thread running the check, so that branch
        owns both halves of the cleanup: the snapshot has to be taken while the
        root is still alive -- the group kill cannot reach a descendant that
        left the group, and once the root is reaped no walk can either.
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        contract = self._detached_tree_contract(root)
        self._interrupt_when_marker_appears(root / "started" / "tree")

        start = time.monotonic()
        with self.assertRaises(KeyboardInterrupt):
            verification.run_verification(contract, repo_root=root)
        elapsed = time.monotonic() - start

        # The interrupt is what returned control, not the check's own timeout.
        self.assertLess(elapsed, 25.0)
        self.assertEqual(["tree"], [p.name for p in (root / "grandchildren").iterdir()])
        assert_marked_pids_exited(root / "started", label="command process")
        assert_marked_pids_exited(root / "grandchildren", label="detached grandchild")

    def test_sequential_interrupt_sweep_opt_out_skips_the_process_table(self) -> None:
        """Opted out, the interrupt path samples nothing and kills nothing.

        The escapee surviving here is also what proves the group kill never
        reaches it: in the sibling test above it dies because of the sweep.
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        contract = self._detached_tree_contract(root)
        self._interrupt_when_marker_appears(root / "started" / "tree")

        with (
            mock.patch.dict(os.environ, {process_tree.SWEEP_DISABLED_ENV_VAR: "1"}),
            mock.patch.object(verification, "sample_process_table") as snapshot_sampler,
            mock.patch.object(process_tree, "sample_process_table") as sweep_sampler,
            self.assertRaises(KeyboardInterrupt),
        ):
            verification.run_verification(contract, repo_root=root)

        snapshot_sampler.assert_not_called()
        sweep_sampler.assert_not_called()
        assert_marked_pids_exited(root / "started", label="command process")
        assert_marked_pids_alive(root / "grandchildren", label="detached grandchild")

    def test_sweep_report_reaches_the_serialized_check_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            check = contracts.CommandCheck(
                name="ok",
                argv=("python3", "-c", "pass"),
                timeout_seconds=30.0,
            )

            result = verification.run_check(check, repo_root=root)
            payload = verification._check_result_payload(result)

            self.assertIn("sweep", payload)
            self.assertEqual(result.sweep, payload["sweep"])
            # The compaction that strips raw output keeps the report.
            self.assertEqual(
                result.sweep, run_persistence._strip_check_output(payload)["sweep"]
            )

    def test_command_check_reports_command_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            check = contracts.CommandCheck(
                name="missing",
                argv=("definitely-not-a-real-binary-xyz",),
                timeout_seconds=5.0,
            )
            result = verification.run_check(check, repo_root=repo_root)

            self.assertEqual("fail", result.status)
            self.assertEqual("command-not-found", result.failure_reason)
            self.assertIn("definitely-not-a-real-binary-xyz", result.detail)

    def test_command_check_refuses_overly_broad_operator_health(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            check = contracts.CommandCheck(
                name="repo-wide-health",
                argv=("./operator", "health", "check-ts"),
                timeout_seconds=5.0,
            )

            result = verification.run_check(check, repo_root=repo_root)

            self.assertEqual("fail", result.status)
            self.assertEqual("command-overbroad", result.failure_reason)
            self.assertIn("overly broad verification command", result.detail)
            self.assertIsNone(result.returncode)

    def test_command_check_resolves_cwd_under_repo_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            sub = repo_root / "nested"
            sub.mkdir()
            check = contracts.CommandCheck(
                name="pwd",
                argv=(
                    "python3",
                    "-c",
                    "import os, sys; sys.stdout.write(os.getcwd())",
                ),
                timeout_seconds=10.0,
                cwd="nested",
            )
            result = verification.run_check(check, repo_root=repo_root)

            self.assertEqual("pass", result.status)
            self.assertEqual(
                str(plans.canonicalize_path(sub)),
                str(plans.canonicalize_path(result.stdout)),
            )

    def test_file_exists_check_passes_when_file_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "target.txt").write_text("data\n", encoding="utf-8")
            check = contracts.FileExistsCheck(name="target", path="target.txt")

            result = verification.run_check(check, repo_root=repo_root)

            self.assertEqual("pass", result.status)
            self.assertIsNone(result.failure_reason)

    def test_absolute_file_and_regex_paths_resolve_inside_the_repo_root(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "repo"
            repo_root.mkdir()
            target = repo_root / "inside.txt"
            target.write_text("absolute target\n", encoding="utf-8")

            file_result = verification.run_check(
                contracts.FileExistsCheck(name="absolute-file", path=str(target)),
                repo_root=repo_root,
            )
            regex_result = verification.run_check(
                contracts.RegexInFileCheck(
                    name="absolute-regex",
                    path=str(target),
                    pattern=r"^absolute target$",
                ),
                repo_root=repo_root,
            )

            self.assertEqual("pass", file_result.status)
            self.assertEqual("pass", regex_result.status)

    def test_absolute_path_outside_the_repo_root_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_root = root / "repo"
            repo_root.mkdir()
            target = root / "outside.txt"
            target.write_text("absolute target\n", encoding="utf-8")

            with self.assertRaisesRegex(plans.PlanError, "escapes the bound root"):
                verification.run_check(
                    contracts.FileExistsCheck(name="absolute-file", path=str(target)),
                    repo_root=repo_root,
                )


if __name__ == "__main__":
    unittest.main()
