"""Verification executor, verify CLI, and artifact storage tests.

Split from tests/test_agent_session_loop.py."""

from __future__ import annotations

import argparse
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import dataclasses
from datetime import datetime, timezone
import errno
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import pty
import select
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
import unittest
import urllib.parse
from unittest import mock

from agent_session_loop import (
    artifacts,
    authoring,
    baseline,
    baseline_cache,
    cli,
    compact_state,
    config,
    contracts,
    control_queue,
    events,
    harnesses,
    install_verify,
    plans,
    preflight,
    prepare,
    process_tree,
    progress_cli,
    progress_repair,
    qna,
    review,
    review_repair,
    run_control,
    runner,
    state,
    tail,
    verification,
    workbench,
    workbench_server,
)
from agent_session_loop.formatting import (
    format_duration_seconds,
    format_turn_finished_message,
    format_turn_started_message,
)
from tests.support import (
    REPO_ROOT,
    assert_marked_pids_alive,
    assert_marked_pids_exited,
    kill_marked_pids,
    write_plan,
    write_process_tree_scripts,
    _contract_block,
)


#: A descendant that leaves the check's process group and keeps the inherited
#: stdout/stderr open, so the reader threads draining them never see EOF while
#: it lives.
_DETACHED_ESCAPEE_SOURCE = (
    "import os, pathlib, sys, time\n"
    "root = pathlib.Path(sys.argv[1])\n"
    "pending = root / 'pending' / 'escapee'\n"
    "pending.write_text(str(os.getpid()))\n"
    "os.replace(pending, root / 'escapees' / 'escapee')\n"
    "time.sleep(120)\n"
)

#: The command a check runs: start the escapee, wait until it is live, exit 0.
_DETACHED_SPAWNER_SOURCE = (
    "import pathlib, subprocess, sys, time\n"
    "root = pathlib.Path(sys.argv[1])\n"
    "subprocess.Popen(\n"
    "    [sys.executable, str(root / 'escapee.py'), str(root)],\n"
    "    start_new_session=True,\n"
    ")\n"
    "while not (root / 'escapees' / 'escapee').exists():\n"
    "    time.sleep(0.01)\n"
)


def write_detached_spawner_script(root: Path) -> Path:
    """Lay out a command that exits 0 leaving one `setsid` escapee behind."""
    for name in ("pending", "escapees"):
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / "escapee.py").write_text(_DETACHED_ESCAPEE_SOURCE, encoding="utf-8")
    spawner = root / "spawn_detached.py"
    spawner.write_text(_DETACHED_SPAWNER_SOURCE, encoding="utf-8")
    return spawner


class VerificationExecutorTests(unittest.TestCase):
    def _session_id(self, value: str = "1") -> plans.SessionId:
        return plans.parse_session_id(value)

    def _contract(
        self,
        checks: tuple[contracts.VerificationCheck, ...],
        *,
        parallel: bool = False,
    ) -> contracts.CompletionContract:
        return contracts.CompletionContract(
            session_id=self._session_id(),
            done_when=("done",),
            verify=contracts.VerifyRecipe(checks=checks, parallel=parallel),
        )

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

    #: Leave one background process behind in the check's own process group and
    #: exit 0. Its output is redirected so it does not also hold the captured
    #: pipes -- that is the separate escapee case.
    LEFTOVER_COMMAND = (
        "mkdir -p sleepers; sleep 300 >/dev/null 2>&1 & "
        "echo $! > sleepers/leftover; exit 0"
    )

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
            sleeper = int(
                (root / "sleepers" / "leftover").read_text(encoding="utf-8")
            )
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

    def _detached_tree_contract(self, root: Path) -> contracts.CompletionContract:
        """A one-check sequential recipe whose tree leaves a `setsid` escapee."""
        parent_script, grandchild_script = write_process_tree_scripts(root)
        self.addCleanup(kill_marked_pids, root / "grandchildren")
        return self._contract(
            (
                contracts.CommandCheck(
                    name="tree",
                    argv=(
                        "python3",
                        str(parent_script),
                        "tree",
                        str(root),
                        str(grandchild_script),
                        "detached",
                    ),
                    # Far above the interrupt window, so an interrupt rather
                    # than the timeout is what ends the check; still bounded so
                    # a fixture that never starts fails instead of hanging.
                    timeout_seconds=30.0,
                ),
            )
        )

    def _interrupt_when_marker_appears(self, marker: Path) -> None:
        """Ctrl-C the calling thread once ``marker`` exists.

        The check runs in a session of its own, so a SIGINT aimed at this
        process reaches the test runner alone -- everything the check spawned
        is left for the code under test to clean up. Nothing is signalled if
        the marker never appears: the check's own timeout bounds that instead.
        """

        def interrupt() -> None:
            deadline = time.monotonic() + 20
            while not marker.exists():
                if time.monotonic() > deadline:
                    return
                time.sleep(0.01)
            os.kill(os.getpid(), signal.SIGINT)

        thread = threading.Thread(target=interrupt, daemon=True)
        self.addCleanup(thread.join, 5.0)
        thread.start()

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
                result.sweep, runner._strip_check_output(payload)["sweep"]
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

    def test_absolute_file_and_regex_paths_resolve_independently_of_repo_root(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_root = root / "repo"
            repo_root.mkdir()
            target = root / "outside.txt"
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

    def test_file_exists_check_fails_when_file_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            check = contracts.FileExistsCheck(name="absent", path="nope.txt")

            result = verification.run_check(check, repo_root=repo_root)

            self.assertEqual("fail", result.status)
            self.assertEqual("file-missing", result.failure_reason)
            self.assertIn("nope.txt", result.detail)

    def test_regex_in_file_check_passes_when_pattern_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "module.py").write_text(
                "def parse_completion_contract():\n    pass\n",
                encoding="utf-8",
            )
            check = contracts.RegexInFileCheck(
                name="symbol",
                path="module.py",
                pattern=r"^def parse_completion_contract",
            )

            result = verification.run_check(check, repo_root=repo_root)

            self.assertEqual("pass", result.status)

    def test_regex_in_file_check_fails_when_pattern_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "module.py").write_text("nothing\n", encoding="utf-8")
            check = contracts.RegexInFileCheck(
                name="symbol",
                path="module.py",
                pattern=r"^def parse_completion_contract",
            )

            result = verification.run_check(check, repo_root=repo_root)

            self.assertEqual("fail", result.status)
            self.assertEqual("regex-not-matched", result.failure_reason)
            self.assertIn("module.py", result.detail)

    def test_regex_in_file_check_fails_when_file_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            check = contracts.RegexInFileCheck(
                name="symbol",
                path="missing.py",
                pattern=r"anything",
            )

            result = verification.run_check(check, repo_root=repo_root)

            self.assertEqual("fail", result.status)
            self.assertEqual("file-missing", result.failure_reason)

    def test_run_verification_aggregates_all_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "present.txt").write_text("hi\n", encoding="utf-8")
            contract = self._contract(
                checks=(
                    contracts.FileExistsCheck(name="present", path="present.txt"),
                    contracts.FileExistsCheck(name="absent", path="absent.txt"),
                    contracts.CommandCheck(
                        name="ok",
                        argv=("python3", "-c", "import sys; sys.exit(0)"),
                        timeout_seconds=10.0,
                    ),
                )
            )

            result = verification.run_verification(contract, repo_root=repo_root)

            self.assertEqual("1", result.session_id.value)
            self.assertEqual(3, len(result.checks))
            self.assertFalse(result.passed)
            failure_names = {failure.name for failure in result.failures}
            self.assertEqual({"absent"}, failure_names)

    def test_run_verification_passes_when_all_checks_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "present.txt").write_text("hi\n", encoding="utf-8")
            contract = self._contract(
                checks=(
                    contracts.FileExistsCheck(name="present", path="present.txt"),
                    contracts.CommandCheck(
                        name="ok",
                        argv=("python3", "-c", "import sys; sys.exit(0)"),
                        timeout_seconds=10.0,
                    ),
                )
            )

            result = verification.run_verification(contract, repo_root=repo_root)

            self.assertTrue(result.passed)
            self.assertEqual((), result.failures)

    def test_passing_gate_skips_conditional_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "present.txt").write_text("ok\n", encoding="utf-8")
            marker = repo_root / "diagnostic-ran"
            contract = self._contract(
                checks=(
                    contracts.FileExistsCheck(name="suite", path="present.txt"),
                    contracts.CommandCheck(
                        name="focused",
                        argv=(
                            "python3",
                            "-c",
                            f"from pathlib import Path; Path({str(marker)!r}).touch()",
                        ),
                        timeout_seconds=10.0,
                        on_failure_of=("suite",),
                        repo="OAK",
                    ),
                )
            )
            skipped_events: list[verification.CheckSkippedEvent] = []
            observer = verification.VerificationObserver(
                on_check_skipped=skipped_events.append
            )

            result = verification.run_verification(
                contract, repo_root=repo_root, observer=observer
            )

            self.assertTrue(result.passed)
            self.assertEqual(["suite"], [check.name for check in result.checks])
            self.assertEqual(["focused"], [check.name for check in result.skipped_checks])
            self.assertFalse(marker.exists())
            self.assertEqual(1, len(skipped_events))
            self.assertEqual(("suite",), skipped_events[0].on_failure_of)
            self.assertEqual("condition-not-met", skipped_events[0].reason)
            self.assertEqual("OAK", skipped_events[0].repo)

    def test_failing_gate_runs_conditional_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            marker = repo_root / "diagnostic-ran"
            contract = self._contract(
                checks=(
                    contracts.FileExistsCheck(name="suite", path="missing.txt"),
                    contracts.CommandCheck(
                        name="focused",
                        argv=(
                            "python3",
                            "-c",
                            f"from pathlib import Path; Path({str(marker)!r}).touch()",
                        ),
                        timeout_seconds=10.0,
                        on_failure_of=("suite",),
                    ),
                )
            )

            result = verification.run_verification(contract, repo_root=repo_root)

            self.assertFalse(result.passed)
            self.assertTrue(marker.exists())
            self.assertEqual(["suite", "focused"], [check.name for check in result.checks])
            self.assertEqual(["suite"], [check.name for check in result.failures])
            self.assertEqual((), result.diagnostic_failures)
            self.assertEqual((), result.skipped_checks)

    def test_diagnostic_failure_is_reported_but_is_not_a_gate_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            contract = self._contract(
                checks=(
                    contracts.FileExistsCheck(name="suite", path="missing.txt"),
                    contracts.CommandCheck(
                        name="focused",
                        argv=("python3", "-c", "raise SystemExit(3)"),
                        timeout_seconds=10.0,
                        on_failure_of=("suite",),
                    ),
                )
            )

            result = verification.run_verification(contract, repo_root=repo_root)

            self.assertFalse(result.passed)
            self.assertEqual(["suite"], [check.name for check in result.failures])
            self.assertEqual(
                ["focused"], [check.name for check in result.diagnostic_failures]
            )

    def _record_observer(
        self,
    ) -> tuple[verification.VerificationObserver, list[tuple[str, object]]]:
        events: list[tuple[str, object]] = []
        observer = verification.VerificationObserver(
            on_check_started=lambda event: events.append(("started", event)),
            on_check_finished=lambda event: events.append(("finished", event)),
        )
        return observer, events

    def test_run_verification_emits_observer_events_for_passing_command(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            check = contracts.CommandCheck(
                name="ok",
                argv=("python3", "-c", "import sys; sys.exit(0)"),
                timeout_seconds=10.0,
                repo="SVC",
            )
            contract = self._contract(checks=(check,))
            observer, events = self._record_observer()

            result = verification.run_verification(
                contract, repo_root=repo_root, observer=observer
            )

            self.assertTrue(result.passed)
            self.assertEqual(["started", "finished"], [tag for tag, _ in events])
            started = events[0][1]
            finished = events[1][1]
            self.assertIsInstance(started, verification.CheckStartedEvent)
            self.assertIsInstance(finished, verification.CheckFinishedEvent)
            self.assertEqual(0, started.check_index)
            self.assertEqual(1, started.check_count)
            self.assertEqual("ok", started.name)
            self.assertEqual("command", started.kind)
            self.assertEqual(check.argv, started.argv)
            self.assertEqual("SVC", started.repo)
            self.assertEqual(
                str(plans.canonicalize_path(repo_root)), started.cwd
            )
            self.assertEqual(started.session_id, contract.session_id)
            self.assertEqual(0, finished.check_index)
            self.assertEqual(1, finished.check_count)
            self.assertEqual("pass", finished.result.status)
            self.assertEqual("command", finished.result.kind)
            self.assertEqual(0, finished.result.returncode)
            self.assertEqual("SVC", finished.repo)

    def test_run_verification_emits_observer_events_for_failing_command(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            check = contracts.CommandCheck(
                name="boom",
                argv=("python3", "-c", "import sys; sys.exit(3)"),
                timeout_seconds=10.0,
            )
            contract = self._contract(checks=(check,))
            observer, events = self._record_observer()

            result = verification.run_verification(
                contract, repo_root=repo_root, observer=observer
            )

            self.assertFalse(result.passed)
            self.assertEqual(["started", "finished"], [tag for tag, _ in events])
            finished = events[1][1]
            self.assertEqual("fail", finished.result.status)
            self.assertEqual("command-failed", finished.result.failure_reason)
            self.assertEqual(3, finished.result.returncode)

    def test_run_verification_emits_observer_events_for_command_timeout(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            check = contracts.CommandCheck(
                name="sleeper",
                argv=("python3", "-c", "import time; time.sleep(5)"),
                timeout_seconds=0.1,
            )
            contract = self._contract(checks=(check,))
            observer, events = self._record_observer()

            result = verification.run_verification(
                contract, repo_root=repo_root, observer=observer
            )

            self.assertFalse(result.passed)
            self.assertEqual(["started", "finished"], [tag for tag, _ in events])
            finished = events[1][1]
            self.assertEqual("fail", finished.result.status)
            self.assertEqual("command-timeout", finished.result.failure_reason)

    def test_run_verification_emits_observer_events_for_file_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "present.txt").write_text("hi\n", encoding="utf-8")
            check = contracts.FileExistsCheck(name="present", path="present.txt")
            contract = self._contract(checks=(check,))
            observer, events = self._record_observer()

            result = verification.run_verification(
                contract, repo_root=repo_root, observer=observer
            )

            self.assertTrue(result.passed)
            self.assertEqual(["started", "finished"], [tag for tag, _ in events])
            started = events[0][1]
            self.assertEqual("file_exists", started.kind)
            self.assertIsNone(started.argv)
            self.assertIsNone(started.cwd)
            finished = events[1][1]
            self.assertEqual("pass", finished.result.status)
            self.assertEqual("file_exists", finished.kind)

    def test_run_verification_emits_observer_events_for_regex_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "module.py").write_text(
                "def parse_completion_contract():\n    pass\n",
                encoding="utf-8",
            )
            check = contracts.RegexInFileCheck(
                name="symbol",
                path="module.py",
                pattern=r"^def parse_completion_contract",
            )
            contract = self._contract(checks=(check,))
            observer, events = self._record_observer()

            result = verification.run_verification(
                contract, repo_root=repo_root, observer=observer
            )

            self.assertTrue(result.passed)
            self.assertEqual(["started", "finished"], [tag for tag, _ in events])
            started = events[0][1]
            self.assertEqual("regex_in_file", started.kind)
            self.assertIsNone(started.argv)
            self.assertIsNone(started.cwd)
            finished = events[1][1]
            self.assertEqual("pass", finished.result.status)
            self.assertEqual("regex_in_file", finished.kind)

    def test_run_verification_emits_events_in_declared_check_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "present.txt").write_text("hi\n", encoding="utf-8")
            contract = self._contract(
                checks=(
                    contracts.FileExistsCheck(
                        name="present", path="present.txt"
                    ),
                    contracts.CommandCheck(
                        name="middle",
                        argv=("python3", "-c", "import sys; sys.exit(0)"),
                        timeout_seconds=10.0,
                    ),
                    contracts.FileExistsCheck(
                        name="absent", path="nope.txt"
                    ),
                )
            )
            observer, events = self._record_observer()

            result = verification.run_verification(
                contract, repo_root=repo_root, observer=observer
            )

            self.assertFalse(result.passed)
            self.assertEqual(
                [
                    ("started", 0, "present"),
                    ("finished", 0, "present"),
                    ("started", 1, "middle"),
                    ("finished", 1, "middle"),
                    ("started", 2, "absent"),
                    ("finished", 2, "absent"),
                ],
                [
                    (tag, event.check_index, event.name)
                    for tag, event in events
                ],
            )
            for tag, event in events:
                self.assertEqual(3, event.check_count)
                if tag == "finished":
                    expected_status = "pass" if event.name != "absent" else "fail"
                    self.assertEqual(expected_status, event.result.status)

    def test_run_verification_resolves_observer_cwd_for_command_subdir(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "nested").mkdir()
            check = contracts.CommandCheck(
                name="pwd",
                argv=("python3", "-c", "import sys; sys.exit(0)"),
                timeout_seconds=10.0,
                cwd="nested",
            )
            contract = self._contract(checks=(check,))
            observer, events = self._record_observer()

            verification.run_verification(
                contract, repo_root=repo_root, observer=observer
            )

            started = events[0][1]
            self.assertEqual(
                str(plans.canonicalize_path(repo_root / "nested")),
                started.cwd,
            )

    def test_run_verification_with_partial_observer_only_fires_set_callback(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "present.txt").write_text("hi\n", encoding="utf-8")
            contract = self._contract(
                checks=(
                    contracts.FileExistsCheck(name="present", path="present.txt"),
                )
            )
            finished_events: list[verification.CheckFinishedEvent] = []
            observer = verification.VerificationObserver(
                on_check_started=None,
                on_check_finished=finished_events.append,
            )

            verification.run_verification(
                contract, repo_root=repo_root, observer=observer
            )

            self.assertEqual(1, len(finished_events))
            self.assertEqual("present", finished_events[0].name)

    def test_run_verification_without_observer_matches_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "present.txt").write_text("hi\n", encoding="utf-8")
            contract = self._contract(
                checks=(
                    contracts.FileExistsCheck(name="present", path="present.txt"),
                )
            )

            result = verification.run_verification(contract, repo_root=repo_root)

            self.assertTrue(result.passed)
            self.assertEqual(1, len(result.checks))

    def test_run_verification_propagates_observer_callback_exceptions(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "present.txt").write_text("hi\n", encoding="utf-8")
            contract = self._contract(
                checks=(
                    contracts.FileExistsCheck(name="present", path="present.txt"),
                )
            )

            class _Boom(RuntimeError):
                pass

            def _explode(_event: verification.CheckStartedEvent) -> None:
                raise _Boom("observer error")

            observer = verification.VerificationObserver(
                on_check_started=_explode
            )

            with self.assertRaises(_Boom):
                verification.run_verification(
                    contract, repo_root=repo_root, observer=observer
                )

    def _command(
        self,
        argv: tuple[str, ...],
        *,
        name: str = "cmd",
        timeout_seconds: float = 10.0,
    ) -> contracts.CommandCheck:
        return contracts.CommandCheck(
            name=name, argv=argv, timeout_seconds=timeout_seconds
        )

    def test_diagnose_command_flags_missing_bare_executable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            check = self._command(("pytest", "-q"))
            diagnostic = verification.diagnose_command(
                check, repo_root=Path(tmp), which=lambda _name: None
            )

            self.assertIsNotNone(diagnostic)
            self.assertEqual("executable-not-found", diagnostic.reason)
            self.assertIn("pytest", diagnostic.detail)

    def test_diagnose_command_skips_path_bearing_executable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            check = self._command((".venv/bin/tool", "run"))
            diagnostic = verification.diagnose_command(
                check,
                repo_root=Path(tmp),
                which=lambda _name: None,
            )

            self.assertIsNone(diagnostic)

    def test_diagnose_command_flags_missing_module(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            check = self._command(("python3", "-m", "pytest", "-q"))
            diagnostic = verification.diagnose_command(
                check,
                repo_root=Path(tmp),
                which=lambda name: f"/usr/bin/{name}",
                module_probe=lambda *_a, **_k: False,
            )

            self.assertIsNotNone(diagnostic)
            self.assertEqual("module-not-importable", diagnostic.reason)
            self.assertIn("pytest", diagnostic.detail)

    def test_diagnose_command_passes_present_module(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            check = self._command(("python3", "-m", "unittest"))
            diagnostic = verification.diagnose_command(
                check,
                repo_root=Path(tmp),
                which=lambda name: f"/usr/bin/{name}",
                module_probe=lambda *_a, **_k: True,
            )

            self.assertIsNone(diagnostic)

    def test_diagnose_command_fails_open_on_probe_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            check = self._command(("python3", "-m", "pytest"))
            # A probe that returns the fail-open sentinel (``None``) yields no
            # finding even when the module might otherwise look absent.
            diagnostic = verification.diagnose_command(
                check,
                repo_root=Path(tmp),
                which=lambda name: f"/usr/bin/{name}",
                module_probe=lambda *_a, **_k: None,
            )

            self.assertIsNone(diagnostic)

    def test_diagnose_command_ignores_non_module_argv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            check = self._command(("python3", "-c", "print(1)"))
            probe_calls: list[tuple[object, ...]] = []

            def _record(interpreter: str, module: str, **_k: object) -> bool:
                probe_calls.append((interpreter, module))
                return False

            diagnostic = verification.diagnose_command(
                check,
                repo_root=Path(tmp),
                which=lambda name: f"/usr/bin/{name}",
                module_probe=_record,
            )

            self.assertIsNone(diagnostic)
            self.assertEqual([], probe_calls)

    def test_diagnose_command_none_for_runnable_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            check = self._command(("python3", "-c", "import sys; sys.exit(0)"))
            diagnostic = verification.diagnose_command(
                check, repo_root=Path(tmp)
            )

            self.assertIsNone(diagnostic)

    def test_diagnose_command_skips_when_repo_root_none(self) -> None:
        check = self._command(("pytest",))
        diagnostic = verification.diagnose_command(
            check, repo_root=None, which=lambda _name: None
        )

        self.assertIsNone(diagnostic)

    def test_diagnose_command_scope_flags_check_all(self) -> None:
        check = self._command(("./operator", "health", "check-all"))

        diagnostic = verification.diagnose_command_scope(check)

        self.assertIsNotNone(diagnostic)
        assert diagnostic is not None
        self.assertEqual("operator-health-check-all", diagnostic.reason)
        self.assertIn("forbidden", diagnostic.detail)

    def test_diagnose_command_scope_flags_unscoped_health(self) -> None:
        check = self._command(("./operator", "health", "test", "--resume"))

        diagnostic = verification.diagnose_command_scope(check)

        self.assertIsNotNone(diagnostic)
        assert diagnostic is not None
        self.assertEqual("operator-health-unscoped", diagnostic.reason)
        self.assertIn("package target flags", diagnostic.detail)

    def test_diagnose_command_scope_allows_scoped_health(self) -> None:
        check = self._command(
            ("./operator", "health", "lint", "--core-oidc", "--resume")
        )

        diagnostic = verification.diagnose_command_scope(check)

        self.assertIsNone(diagnostic)

    def test_default_module_probe_detects_present_and_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            present = verification._default_module_probe(
                sys.executable, "unittest", cwd=repo_root
            )
            missing = verification._default_module_probe(
                sys.executable,
                "definitely_not_a_real_module_xyz",
                cwd=repo_root,
            )

            self.assertIs(True, present)
            self.assertIs(False, missing)

    def test_default_module_probe_detects_missing_dotted_submodule(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outcome = verification._default_module_probe(
                sys.executable,
                "definitely_not_a_real_module_xyz.submodule",
                cwd=Path(tmp),
            )
            self.assertIs(False, outcome)

    def test_default_module_probe_detects_present_dotted_module(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outcome = verification._default_module_probe(
                sys.executable, "importlib.util", cwd=Path(tmp)
            )
            self.assertIs(True, outcome)

    # --- Streaming / bounded command-output regression coverage (session 2) ---
    # These live in VerificationExecutorTests so the session-2 completion
    # contract's verify recipe (which names this class) exercises them.

    def test_passing_command_records_bounded_metadata_no_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "repo"
            repo_root.mkdir()
            artifact_dir = Path(tmp) / "artifacts"
            check = self._command(
                (
                    "python3",
                    "-c",
                    "import sys; sys.stdout.write('hello world')",
                )
            )

            result = verification.run_check(
                check, repo_root=repo_root, artifact_dir=artifact_dir
            )

            self.assertEqual("pass", result.status)
            self.assertEqual("hello world", result.stdout)
            self.assertFalse(result.stdout_truncated)
            self.assertEqual(len(b"hello world"), result.stdout_byte_count)
            self.assertEqual(
                artifacts.sha256_hex(b"hello world"), result.stdout_sha256
            )
            # Passing output never writes a full raw artifact.
            self.assertIsNone(result.stdout_artifact_path)
            self.assertIsNone(result.stderr_artifact_path)
            self.assertFalse(artifact_dir.exists())

    def test_large_passing_output_is_previewed_not_embedded_whole(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "repo"
            repo_root.mkdir()
            # Emit far more than the 16 KiB preview window.
            size = 400 * 1024
            check = self._command(
                (
                    "python3",
                    "-c",
                    f"import sys; sys.stdout.write('a' * {size})",
                )
            )

            result = verification.run_check(check, repo_root=repo_root)

            self.assertEqual("pass", result.status)
            self.assertEqual(size, result.stdout_byte_count)
            self.assertTrue(result.stdout_truncated)
            # The serialized result cannot contain the full large output.
            self.assertLess(len(result.stdout), size)
            self.assertLessEqual(
                len(result.stdout),
                artifacts.PREVIEW_HEAD_BYTES
                + artifacts.PREVIEW_TAIL_BYTES
                + len(artifacts.PREVIEW_TRUNCATION_MARKER),
            )
            self.assertIn(artifacts.PREVIEW_TRUNCATION_MARKER, result.stdout)

    def test_failed_command_retains_full_output_recoverable_from_gzip(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "repo"
            repo_root.mkdir()
            artifact_dir = Path(tmp) / "artifacts"
            size = 200 * 1024
            check = self._command(
                (
                    "python3",
                    "-c",
                    "import sys\n"
                    f"sys.stdout.write('x' * {size})\n"
                    "sys.exit(5)",
                ),
                name="boom",
            )

            result = verification.run_check(
                check, repo_root=repo_root, artifact_dir=artifact_dir
            )

            self.assertEqual("fail", result.status)
            self.assertEqual("command-failed", result.failure_reason)
            self.assertTrue(result.stdout_truncated)
            self.assertIsNotNone(result.stdout_artifact_path)
            recovered = artifacts.read_gzip(Path(result.stdout_artifact_path))
            self.assertEqual(b"x" * size, recovered)
            self.assertEqual(
                artifacts.sha256_hex(recovered), result.stdout_sha256
            )

    def test_colliding_check_names_write_distinct_recoverable_artifacts(
        self,
    ) -> None:
        # Each pair sanitizes to the same stem; without a collision-resistant
        # identifier the second failed check would overwrite the first
        # check's artifact via os.replace. The second pair additionally
        # shares the first 8 hex chars of the SHA-256 digests of its
        # "-stdout" artifact names, so a short digest suffix still collided.
        pairs = (
            ("a/b", "a?b"),
            ("a$%#:??/b", "a%%#%$:/b"),
        )
        for first_name, second_name in pairs:
            with self.subTest(pair=(first_name, second_name)):
                with tempfile.TemporaryDirectory() as tmp:
                    repo_root = Path(tmp) / "repo"
                    repo_root.mkdir()
                    artifact_dir = Path(tmp) / "artifacts"
                    results = {}
                    for name, payload in (
                        (first_name, "first-output"),
                        (second_name, "second-output"),
                    ):
                        check = self._command(
                            (
                                "python3",
                                "-c",
                                "import sys; "
                                f"sys.stdout.write({payload!r}); sys.exit(1)",
                            ),
                            name=name,
                        )
                        result = verification.run_check(
                            check, repo_root=repo_root, artifact_dir=artifact_dir
                        )
                        self.assertEqual("fail", result.status)
                        self.assertIsNotNone(result.stdout_artifact_path)
                        results[name] = (result, payload)

                    first, first_payload = results[first_name]
                    second, second_payload = results[second_name]
                    self.assertNotEqual(
                        first.stdout_artifact_path, second.stdout_artifact_path
                    )
                    self.assertNotEqual(
                        first.stderr_artifact_path, second.stderr_artifact_path
                    )
                    self.assertEqual(
                        first_payload.encode("utf-8"),
                        artifacts.read_gzip(Path(first.stdout_artifact_path)),
                    )
                    self.assertEqual(
                        second_payload.encode("utf-8"),
                        artifacts.read_gzip(Path(second.stdout_artifact_path)),
                    )

    def test_failed_command_without_artifact_dir_keeps_previews_only(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "repo"
            repo_root.mkdir()
            check = self._command(
                (
                    "python3",
                    "-c",
                    "import sys; sys.stderr.write('nope'); sys.exit(2)",
                )
            )

            result = verification.run_check(check, repo_root=repo_root)

            self.assertEqual("fail", result.status)
            self.assertEqual("nope", result.stderr)
            self.assertEqual(len(b"nope"), result.stderr_byte_count)
            self.assertIsNone(result.stdout_artifact_path)
            self.assertIsNone(result.stderr_artifact_path)

    def test_timeout_retains_partial_output_under_failure_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "repo"
            repo_root.mkdir()
            artifact_dir = Path(tmp) / "artifacts"
            check = self._command(
                (
                    "python3",
                    "-c",
                    "import sys, time\n"
                    "sys.stdout.write('partial')\n"
                    "sys.stdout.flush()\n"
                    "time.sleep(5)",
                ),
                name="slow",
                timeout_seconds=0.3,
            )

            result = verification.run_check(
                check, repo_root=repo_root, artifact_dir=artifact_dir
            )

            self.assertEqual("fail", result.status)
            self.assertEqual("command-timeout", result.failure_reason)
            # Whatever was flushed before the timeout is retained as a bounded
            # preview and, because timeout is a failure, an artifact was written.
            self.assertIn("partial", result.stdout)
            self.assertIsNotNone(result.stdout_artifact_path)
            recovered = artifacts.read_gzip(Path(result.stdout_artifact_path))
            self.assertEqual(b"partial", recovered)

    def test_stream_capture_fails_open_when_artifact_write_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "repo"
            repo_root.mkdir()
            # Point the artifact directory at a path blocked by an existing file
            # so directory creation raises; verification must still succeed with
            # bounded metadata and no artifact path.
            blocker = Path(tmp) / "blocker"
            blocker.write_text("not a dir\n", encoding="utf-8")
            artifact_dir = blocker / "sub"
            check = self._command(
                (
                    "python3",
                    "-c",
                    "import sys; sys.stdout.write('data'); sys.exit(9)",
                )
            )

            result = verification.run_check(
                check, repo_root=repo_root, artifact_dir=artifact_dir
            )

            self.assertEqual("fail", result.status)
            self.assertEqual("data", result.stdout)
            self.assertEqual(
                artifacts.sha256_hex(b"data"), result.stdout_sha256
            )
            self.assertIsNone(result.stdout_artifact_path)

    def test_run_verification_forwards_artifact_dir_to_command_checks(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "repo"
            repo_root.mkdir()
            artifact_dir = Path(tmp) / "artifacts"
            contract = contracts.CompletionContract(
                session_id=plans.parse_session_id("1"),
                done_when=("done",),
                verify=contracts.VerifyRecipe(
                    checks=(
                        self._command(
                            (
                                "python3",
                                "-c",
                                "import sys; sys.stdout.write('boom'); "
                                "sys.exit(1)",
                            ),
                            name="fail-check",
                        ),
                    )
                ),
            )

            result = verification.run_verification(
                contract, repo_root=repo_root, artifact_dir=artifact_dir
            )

            self.assertFalse(result.passed)
            check_result = result.checks[0]
            self.assertIsNotNone(check_result.stdout_artifact_path)
            self.assertTrue(
                Path(check_result.stdout_artifact_path).exists()
            )

    # --- Incremental streaming regression coverage (M1) ---
    # Verification must bound command output as it arrives instead of
    # buffering the full stream before truncation.

    def test_large_output_peak_memory_stays_bounded(self) -> None:
        # Regression (M1): before streaming, subprocess.run buffered the full
        # stream in memory, so the traced-heap peak was at least the output
        # size. Streaming keeps the peak far below it.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "repo"
            repo_root.mkdir()
            size = 8 * 1024 * 1024
            check = self._command(
                (
                    "python3",
                    "-c",
                    "import sys\n"
                    "chunk = 'a' * 65536\n"
                    f"for _ in range({size} // 65536):\n"
                    "    sys.stdout.write(chunk)\n",
                ),
                timeout_seconds=120.0,
            )

            tracemalloc.start()
            try:
                result = verification.run_check(check, repo_root=repo_root)
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()

            self.assertEqual("pass", result.status)
            self.assertEqual(size, result.stdout_byte_count)
            self.assertTrue(result.stdout_truncated)
            self.assertLess(peak, size // 2)

    def test_streamed_digest_and_preview_match_buffered_reference(self) -> None:
        # The streamed capture must be indistinguishable from the previous
        # fully-buffered implementation: same digest, preview, truncation
        # flag, and byte count as store_stream_output over the whole stream.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "repo"
            repo_root.mkdir()
            # Patterned output larger than the 16 KiB preview window,
            # including bytes that are invalid UTF-8.
            expected = bytes(range(256)) * 256
            check = self._command(
                (
                    "python3",
                    "-c",
                    "import sys\n"
                    "sys.stdout.buffer.write(bytes(range(256)) * 256)\n",
                )
            )

            result = verification.run_check(check, repo_root=repo_root)

            reference = artifacts.store_stream_output(expected, failed=False)
            self.assertEqual("pass", result.status)
            self.assertEqual(reference.byte_count, result.stdout_byte_count)
            self.assertEqual(reference.sha256, result.stdout_sha256)
            self.assertEqual(reference.preview, result.stdout)
            self.assertEqual(reference.truncated, result.stdout_truncated)


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
        self.session_id = plans.parse_session_id("1")

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
            payload = runner._verification_failure_details(
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
            runner._verification_fingerprint(sequential_details),
            runner._verification_fingerprint(parallel_details),
        )
        self.assertIsNotNone(runner._verification_fingerprint(parallel_details))

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
        with mock.patch.object(
            verification, "as_completed", interrupt_once_running
        ):
            with self.assertRaises(KeyboardInterrupt):
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
        with mock.patch.object(
            verification, "as_completed", interrupt_once_trees_are_live
        ):
            with self.assertRaises(KeyboardInterrupt):
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
        with mock.patch.object(
            verification, "as_completed", interrupt_once_trees_are_live
        ):
            with self.assertRaises(KeyboardInterrupt):
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


class VerifyCliTests(unittest.TestCase):
    def _run_verify(
        self,
        plan_dir: Path,
        *,
        session: str,
        cwd: Path,
        json_output: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        argv = [
            sys.executable,
            "-m",
            "agent_session_loop",
            "verify",
            str(plan_dir),
            "--session",
            session,
            "--cwd",
            str(cwd),
        ]
        if json_output:
            argv.append("--json")
        return subprocess.run(
            argv,
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_verify_json_passes_when_all_checks_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            (repo_root / "target.txt").write_text("ok\n", encoding="utf-8")
            contract = _contract_block(
                '{"type": "file_exists", "path": "target.txt", '
                '"name": "target", "repo": "SVC"}'
            )
            write_plan(
                plan_dir, ("pending",), contracts_by_id={"1": contract}
            )

            completed = self._run_verify(plan_dir, session="1", cwd=repo_root)

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual("", completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertTrue(payload["ok"])
            self.assertTrue(payload["passed"])
            self.assertEqual("verify", payload["command"])
            self.assertEqual("ok", payload["reason"])
            self.assertEqual("1", payload["session"])
            self.assertEqual(
                str(plans.canonicalize_path(repo_root)), payload["cwd"]
            )
            self.assertEqual(str(plan_dir), payload["plan_dir"])
            self.assertEqual(1, payload["recipe"]["check_count"])
            self.assertEqual(1, payload["recipe"]["gate_count"])
            self.assertEqual(0, payload["recipe"]["diagnostic_count"])
            self.assertEqual(["file_exists"], payload["recipe"]["kinds"])
            self.assertEqual(["target"], payload["recipe"]["names"])
            self.assertEqual(["SVC"], payload["recipe"]["repos"])
            self.assertEqual([], payload["skipped_checks"])
            self.assertEqual([], payload["diagnostic_failures"])
            self.assertEqual(1, len(payload["checks"]))
            check = payload["checks"][0]
            self.assertEqual("target", check["name"])
            self.assertEqual("file_exists", check["kind"])
            self.assertEqual("pass", check["status"])
            self.assertIsNone(check["failure_reason"])

    def test_verify_json_failure_reports_check_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            contract = _contract_block(
                '{"type": "file_exists", "path": "absent.txt", "name": "absent"}'
            )
            write_plan(
                plan_dir, ("pending",), contracts_by_id={"1": contract}
            )

            completed = self._run_verify(plan_dir, session="1", cwd=repo_root)

            self.assertEqual(1, completed.returncode)
            self.assertEqual("", completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertFalse(payload["ok"])
            self.assertFalse(payload["passed"])
            self.assertFalse(payload["paused"])
            self.assertEqual("verify-failed", payload["reason"])
            self.assertEqual("1", payload["session"])
            self.assertIn("verification check", payload["error"])
            self.assertEqual(1, len(payload["checks"]))
            check = payload["checks"][0]
            self.assertEqual("absent", check["name"])
            self.assertEqual("fail", check["status"])
            self.assertEqual("file-missing", check["failure_reason"])
            self.assertIn("absent.txt", check["detail"])

    def test_verify_json_skips_conditional_diagnostic_when_gate_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            (repo_root / "target.txt").write_text("ok\n", encoding="utf-8")
            contract = _contract_block(
                ', '.join(
                    (
                        '{"type": "file_exists", "path": "target.txt", '
                        '"name": "suite"}',
                        '{"type": "file_exists", "path": "focused.txt", '
                        '"name": "focused", "on_failure_of": ["suite"]}',
                    )
                )
            )
            write_plan(plan_dir, ("pending",), contracts_by_id={"1": contract})

            completed = self._run_verify(plan_dir, session="1", cwd=repo_root)

            self.assertEqual(0, completed.returncode, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertTrue(payload["passed"])
            self.assertEqual(1, payload["recipe"]["gate_count"])
            self.assertEqual(1, payload["recipe"]["diagnostic_count"])
            self.assertEqual(["suite"], [row["name"] for row in payload["checks"]])
            self.assertEqual(
                ["focused"], [row["name"] for row in payload["skipped_checks"]]
            )
            self.assertEqual(
                ["suite"], payload["skipped_checks"][0]["on_failure_of"]
            )
            self.assertEqual([], payload["diagnostic_failures"])

    def test_verify_json_no_contract_returns_no_contract_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            write_plan(plan_dir, ("pending",))

            completed = self._run_verify(plan_dir, session="1", cwd=repo_root)

            self.assertEqual(1, completed.returncode)
            self.assertEqual("", completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertFalse(payload["ok"])
            self.assertEqual("no-contract", payload["reason"])
            self.assertEqual("1", payload["session"])
            self.assertEqual([], payload["checks"])
            self.assertEqual(0, payload["recipe"]["check_count"])
            self.assertEqual(0, payload["recipe"]["gate_count"])
            self.assertEqual(0, payload["recipe"]["diagnostic_count"])
            self.assertEqual([], payload["skipped_checks"])
            self.assertEqual([], payload["diagnostic_failures"])
            self.assertFalse(payload["passed"])

    def test_verify_json_no_contract_attaches_skill_remediation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            write_plan(plan_dir, ("pending",))

            completed = self._run_verify(plan_dir, session="1", cwd=repo_root)

            payload = json.loads(completed.stdout)
            self.assertEqual("no-contract", payload["reason"])
            # The runtime repair hint resolves for no-contract.
            self.assertEqual(
                "runtime.no-contract", payload["repair_hint"]["check_id"]
            )
            remediation = payload["skill_remediation"]
            # Names both escape hatches with the plan directory substituted.
            self.assertIn("--allow-not-loop-ready", remediation["message"])
            self.assertIn("--mark-loop-ready", remediation["message"])
            commands = [
                step["command"] for step in remediation["next_steps"]
            ]
            self.assertIn(
                f"asl run {plan_dir} --allow-not-loop-ready", commands
            )
            self.assertIn(
                f"asl preflight {plan_dir} --json --mark-loop-ready", commands
            )

    def test_no_contract_remediation_commands_are_shell_safe(self) -> None:
        # A plan directory carrying whitespace and shell metacharacters must
        # still round-trip as a single argument in every advertised no-contract
        # remediation command (both next_steps commands and the message text).
        weird_plan = "/plans/de mo; rm -rf $(evil) 'q'"
        remediation = verification._no_contract_skill_remediation(weird_plan)
        commands = [step["command"] for step in remediation["next_steps"]]
        self.assertEqual(
            [
                ["asl", "run", weird_plan, "--allow-not-loop-ready"],
                ["asl", "preflight", weird_plan, "--json", "--mark-loop-ready"],
            ],
            [shlex.split(command) for command in commands],
        )
        # The prose message embeds the same shell-safe command strings, so a
        # reader copy-pasting them into a shell also preserves the path.
        for command in commands:
            self.assertIn(f"`{command}`", remediation["message"])
            self.assertNotIn(weird_plan, remediation["message"].replace(command, ""))

    def test_verify_json_command_not_found_hint_names_interpreter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            argv = ["definitely-not-a-real-binary-xyz", "--version"]
            contract = _contract_block(
                '{"type": "command", "name": "cmd", '
                f'"argv": {json.dumps(argv)}, "timeout_seconds": 30}}'
            )
            write_plan(
                plan_dir, ("pending",), contracts_by_id={"1": contract}
            )

            completed = self._run_verify(plan_dir, session="1", cwd=repo_root)

            self.assertEqual(1, completed.returncode)
            payload = json.loads(completed.stdout)
            self.assertEqual("verify-failed", payload["reason"])
            self.assertEqual(
                "command-not-found", payload["checks"][0]["failure_reason"]
            )
            hint = payload["repair_hint"]
            self.assertEqual(
                "runtime.verify-failed.command-not-found", hint["check_id"]
            )
            # Interpreter guidance now lives authoritatively in the hint.
            self.assertIn("python3", hint["message"])

    def test_verify_json_failure_attaches_repair_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            contract = _contract_block(
                '{"type": "file_exists", "path": "absent.txt", "name": "absent"}'
            )
            write_plan(
                plan_dir, ("pending",), contracts_by_id={"1": contract}
            )

            completed = self._run_verify(plan_dir, session="1", cwd=repo_root)

            payload = json.loads(completed.stdout)
            self.assertEqual("verify-failed", payload["reason"])
            self.assertEqual(
                "runtime.verify-failed.file-missing",
                payload["repair_hint"]["check_id"],
            )
            # A non-no-contract failure carries no skill_remediation.
            self.assertNotIn("skill_remediation", payload)

    def test_verify_json_malformed_contract_reports_plan_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            contract = "\n".join(
                [
                    "## Completion Contract",
                    "",
                    "### Done when",
                    "- something",
                    "",
                    "### Verify",
                    "",
                    "```json",
                    "{not valid json",
                    "```",
                ]
            )
            write_plan(
                plan_dir, ("pending",), contracts_by_id={"1": contract}
            )

            completed = self._run_verify(plan_dir, session="1", cwd=repo_root)

            self.assertEqual(1, completed.returncode)
            self.assertEqual("", completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertFalse(payload["ok"])
            self.assertEqual("malformed-contract", payload["reason"])
            self.assertEqual("1", payload["session"])
            self.assertIn("Verify JSON is invalid", payload["error"])

    def test_verify_json_invalid_session_id_reports_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            write_plan(plan_dir, ("pending",))

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "verify",
                    str(plan_dir),
                    "--session",
                    "not-a-session",
                    "--cwd",
                    str(repo_root),
                    "--json",
                ],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(1, completed.returncode)
            self.assertEqual("", completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertFalse(payload["ok"])
            self.assertEqual("invalid-session", payload["reason"])
            self.assertEqual("not-a-session", payload["session"])

    def test_verify_text_output_shows_per_check_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            (repo_root / "present.txt").write_text("yes\n", encoding="utf-8")
            contract = _contract_block(
                '{"type": "file_exists", "path": "present.txt", "name": "present"}'
            )
            write_plan(
                plan_dir, ("pending",), contracts_by_id={"1": contract}
            )

            completed = self._run_verify(
                plan_dir, session="1", cwd=repo_root, json_output=False
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertIn("Session: 1", completed.stdout)
            self.assertIn("[PASS] file_exists present", completed.stdout)
            self.assertIn("Verify: passed", completed.stdout)

    def test_verify_text_output_reports_failure_on_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            contract = _contract_block(
                '{"type": "file_exists", "path": "absent.txt", "name": "absent"}'
            )
            write_plan(
                plan_dir, ("pending",), contracts_by_id={"1": contract}
            )

            completed = self._run_verify(
                plan_dir, session="1", cwd=repo_root, json_output=False
            )

            self.assertEqual(1, completed.returncode)
            self.assertIn("[FAIL] file_exists absent", completed.stdout)
            self.assertIn("Verify: failed", completed.stderr)

    def test_verify_json_check_payload_carries_bounded_stream_metadata(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            argv = [
                "python3",
                "-c",
                "import sys; sys.stdout.write('ok output'); sys.exit(0)",
            ]
            contract = _contract_block(
                '{"type": "command", "name": "cmd", '
                f'"argv": {json.dumps(argv)}, "timeout_seconds": 30}}'
            )
            write_plan(
                plan_dir, ("pending",), contracts_by_id={"1": contract}
            )

            completed = self._run_verify(plan_dir, session="1", cwd=repo_root)

            self.assertEqual(0, completed.returncode, completed.stderr)
            payload = json.loads(completed.stdout)
            check = payload["checks"][0]
            self.assertEqual("ok output", check["stdout"])
            self.assertEqual(len(b"ok output"), check["stdout_byte_count"])
            self.assertFalse(check["stdout_truncated"])
            self.assertIsNone(check["stdout_artifact_path"])
            self.assertEqual(64, len(check["stdout_sha256"]))

    def test_verify_artifact_dir_retains_failed_command_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            artifact_dir = tmp_dir / "verify-artifacts"
            argv = [
                "python3",
                "-c",
                "import sys; sys.stdout.write('failure detail'); sys.exit(4)",
            ]
            contract = _contract_block(
                '{"type": "command", "name": "cmd", '
                f'"argv": {json.dumps(argv)}, "timeout_seconds": 30}}'
            )
            write_plan(
                plan_dir, ("pending",), contracts_by_id={"1": contract}
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "verify",
                    str(plan_dir),
                    "--session",
                    "1",
                    "--cwd",
                    str(repo_root),
                    "--artifact-dir",
                    str(artifact_dir),
                    "--json",
                ],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(1, completed.returncode)
            payload = json.loads(completed.stdout)
            check = payload["checks"][0]
            self.assertEqual("command-failed", check["failure_reason"])
            self.assertEqual("failure detail", check["stdout"])
            artifact_path = check["stdout_artifact_path"]
            self.assertIsNotNone(artifact_path)
            recovered = artifacts.read_gzip(Path(artifact_path))
            self.assertEqual(b"failure detail", recovered)


class ArtifactStorageTests(unittest.TestCase):
    def test_sha256_and_counts_are_exact(self) -> None:
        data = b"hello world"
        meta = artifacts.store_stream_output(data, failed=False)

        self.assertEqual(artifacts.OUTPUT_METADATA_VERSION, meta.version)
        self.assertEqual(len(data), meta.byte_count)
        self.assertEqual(len("hello world"), meta.char_count)
        self.assertEqual(
            hashlib.sha256(data).hexdigest(), meta.sha256
        )
        self.assertFalse(meta.truncated)
        self.assertEqual("hello world", meta.preview)
        self.assertIsNone(meta.artifact_path)

    def test_empty_stream_produces_bounded_metadata(self) -> None:
        meta = artifacts.store_stream_output(b"", failed=False)

        self.assertEqual(0, meta.byte_count)
        self.assertEqual(0, meta.char_count)
        self.assertFalse(meta.truncated)
        self.assertEqual("", meta.preview)
        self.assertEqual(hashlib.sha256(b"").hexdigest(), meta.sha256)

    def test_synthetic_14mb_stream_produces_bounded_metadata(self) -> None:
        data = b"A" * (14 * 1024 * 1024)
        meta = artifacts.store_stream_output(data, failed=False)

        self.assertTrue(meta.truncated)
        self.assertEqual(len(data), meta.byte_count)
        self.assertEqual(len(data), meta.char_count)
        # Preview is bounded to head + tail (plus the truncation marker),
        # never the full 14 MB stream.
        self.assertLessEqual(
            len(meta.preview),
            artifacts.PREVIEW_HEAD_BYTES
            + artifacts.PREVIEW_TAIL_BYTES
            + len(artifacts.PREVIEW_TRUNCATION_MARKER),
        )
        self.assertIn(artifacts.PREVIEW_TRUNCATION_MARKER, meta.preview)
        self.assertTrue(meta.preview.startswith("A" * 32))
        self.assertTrue(meta.preview.endswith("A" * 32))

    def test_preview_uses_first_and_last_8kib(self) -> None:
        head = b"H" * artifacts.PREVIEW_HEAD_BYTES
        middle = b"M" * 4096
        tail = b"T" * artifacts.PREVIEW_TAIL_BYTES
        preview, truncated = artifacts.bounded_preview(head + middle + tail)

        self.assertTrue(truncated)
        self.assertNotIn("M", preview)
        self.assertEqual(
            "H" * artifacts.PREVIEW_HEAD_BYTES,
            preview[: artifacts.PREVIEW_HEAD_BYTES],
        )
        self.assertEqual(
            "T" * artifacts.PREVIEW_TAIL_BYTES,
            preview[-artifacts.PREVIEW_TAIL_BYTES :],
        )

    def test_preview_not_truncated_at_exact_boundary(self) -> None:
        data = b"X" * (
            artifacts.PREVIEW_HEAD_BYTES + artifacts.PREVIEW_TAIL_BYTES
        )
        preview, truncated = artifacts.bounded_preview(data)

        self.assertFalse(truncated)
        self.assertEqual(data.decode("utf-8"), preview)

    def test_invalid_utf8_is_handled_in_preview_and_counts(self) -> None:
        data = b"ok\xff\xfebytes"
        meta = artifacts.store_stream_output(data, failed=False)

        self.assertEqual(len(data), meta.byte_count)
        # Invalid bytes decode to U+FFFD replacement characters, so the
        # character count reflects the replaced string, not the raw bytes.
        self.assertEqual(
            len(data.decode("utf-8", errors="replace")), meta.char_count
        )
        self.assertIn("�", meta.preview)
        self.assertEqual(hashlib.sha256(data).hexdigest(), meta.sha256)

    def test_failed_output_is_recoverable_byte_for_byte(self) -> None:
        data = bytes(range(256)) * 2048  # 512 KiB of every byte value
        with tempfile.TemporaryDirectory() as tmp:
            meta = artifacts.store_stream_output(
                data,
                failed=True,
                artifact_dir=tmp,
                artifact_name="stdout",
            )

            self.assertIsNotNone(meta.artifact_path)
            artifact = Path(meta.artifact_path)
            self.assertTrue(artifact.exists())
            self.assertEqual("stdout.gz", artifact.name)
            recovered = artifacts.read_gzip(artifact)
            self.assertEqual(data, recovered)
            self.assertEqual(meta.sha256, artifacts.sha256_hex(recovered))

    def test_passing_output_creates_no_full_raw_artifact(self) -> None:
        data = b"C" * (1024 * 1024)
        with tempfile.TemporaryDirectory() as tmp:
            meta = artifacts.store_stream_output(
                data,
                failed=False,
                artifact_dir=tmp,
                artifact_name="stdout",
            )

            self.assertIsNone(meta.artifact_path)
            self.assertEqual([], list(Path(tmp).iterdir()))

    def test_empty_failed_output_still_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            meta = artifacts.store_stream_output(
                b"", failed=True, artifact_dir=tmp, artifact_name="stderr"
            )

            self.assertIsNotNone(meta.artifact_path)
            self.assertEqual(b"", artifacts.read_gzip(meta.artifact_path))

    def test_artifact_name_is_sanitized(self) -> None:
        cases = {
            "../../etc/passwd": "etc-passwd",
            "weird name!@#.log": "weird-name-.log",
            "  ": "output",
            "": "output",
            "...": "output",
            "already_safe.txt": "already_safe.txt",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(expected, artifacts.sanitize_artifact_name(raw))

    def test_derive_artifact_stem_keeps_safe_names_unchanged(self) -> None:
        for name in ("stdout", "boom-stdout", "already_safe.txt"):
            with self.subTest(name=name):
                self.assertEqual(name, artifacts.derive_artifact_stem(name))
        self.assertEqual("output", artifacts.derive_artifact_stem(None))
        self.assertEqual("output", artifacts.derive_artifact_stem(""))

    def test_derive_artifact_stem_distinguishes_colliding_names(self) -> None:
        first = artifacts.derive_artifact_stem("a/b-stdout")
        second = artifacts.derive_artifact_stem("a?b-stdout")

        self.assertNotEqual(first, second)
        # Both keep the human-readable sanitized stem as a prefix and stay
        # single-path-segment safe.
        self.assertTrue(first.startswith("a-b-stdout-"))
        self.assertTrue(second.startswith("a-b-stdout-"))
        self.assertEqual(first, artifacts.sanitize_artifact_name(first))
        self.assertEqual(second, artifacts.sanitize_artifact_name(second))

    def test_derive_artifact_stem_distinguishes_digest_prefix_collisions(
        self,
    ) -> None:
        # These names sanitize to the same stem AND share the first 8 hex
        # chars of their SHA-256 digests ("cc47903e"), so an 8-char digest
        # suffix still collided; the full digest must keep them apart.
        first = artifacts.derive_artifact_stem("a$%#:??/b-stdout")
        second = artifacts.derive_artifact_stem("a%%#%$:/b-stdout")

        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("a-b-stdout-"))
        self.assertTrue(second.startswith("a-b-stdout-"))
        self.assertEqual(first, artifacts.sanitize_artifact_name(first))
        self.assertEqual(second, artifacts.sanitize_artifact_name(second))

    def test_derive_artifact_stem_is_idempotent_and_deterministic(self) -> None:
        derived = artifacts.derive_artifact_stem("../../etc/passwd")
        self.assertEqual(derived, artifacts.derive_artifact_stem(derived))
        self.assertEqual(
            derived, artifacts.derive_artifact_stem("../../etc/passwd")
        )

    def test_derive_artifact_stem_byte_caps_overlong_names(self) -> None:
        # A ~192-char sanitized name plus the full 64-hex digest used to
        # yield a 260-byte .gz filename (267 with the live -stdout suffix),
        # past the 255-byte per-component limit once the atomic-write
        # ``.{name}.tmp`` decoration is added.
        first = "a" * 190 + "/x-stdout"
        second = "b" + "a" * 189 + "/x-stdout"
        stems = [
            artifacts.derive_artifact_stem(first),
            artifacts.derive_artifact_stem(second),
        ]

        self.assertNotEqual(stems[0], stems[1])
        for stem in stems:
            with self.subTest(stem=stem):
                # The longest generated component is the temp form of the
                # gzip artifact; it must fit the 255-byte limit.
                tmp_component = f".{stem}.gz.tmp"
                self.assertLessEqual(len(tmp_component.encode("utf-8")), 255)
                self.assertEqual(stem, artifacts.sanitize_artifact_name(stem))
                self.assertEqual(stem, artifacts.derive_artifact_stem(stem))
        # The readable prefix survives truncation.
        self.assertTrue(stems[0].startswith("a" * 32))

    def test_derive_artifact_stem_caps_safe_but_overlong_names(self) -> None:
        # Names that sanitize losslessly can still overflow the component
        # limit on their own; they get the same truncate-plus-digest
        # treatment, and distinct names stay distinct.
        first = artifacts.derive_artifact_stem("a" * 250)
        second = artifacts.derive_artifact_stem("a" * 251)

        self.assertNotEqual(first, second)
        for stem in (first, second):
            with self.subTest(stem=stem):
                self.assertLessEqual(len(f".{stem}.gz.tmp".encode()), 255)
                self.assertEqual(stem, artifacts.derive_artifact_stem(stem))

    def test_truncate_utf8_never_splits_multibyte_characters(self) -> None:
        # Sanitized stems are ASCII today, but the byte cap must stay safe if
        # sanitization ever preserves multibyte characters: a cut that lands
        # mid-sequence drops the partial character instead of splitting it.
        text = "é" * 100  # two bytes per character
        for max_bytes in (0, 1, 2, 3, 199, 200, 201):
            with self.subTest(max_bytes=max_bytes):
                truncated = artifacts._truncate_utf8(text, max_bytes)
                self.assertLessEqual(
                    len(truncated.encode("utf-8")), max_bytes
                )
                self.assertTrue(text.startswith(truncated))

    def test_overlong_check_name_yields_recoverable_live_artifact(self) -> None:
        # Live verification names failure artifacts ``{check.name}-stdout``;
        # a ~192-char check name must still produce a bounded, recoverable
        # artifact instead of losing the spool at promotion time.
        data = b"failure output for an overlong check name"
        names = ["a" * 190 + "/x-stdout", "b" + "a" * 189 + "/x-stdout"]
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for name in names:
                capture = artifacts.StreamCapture(spool=True)
                capture.feed(data)
                meta = capture.finalize(
                    failed=True, artifact_dir=tmp, artifact_name=name
                )
                with self.subTest(name=name):
                    self.assertIsNotNone(meta.artifact_path)
                    artifact = Path(meta.artifact_path)
                    self.assertLessEqual(
                        len(artifact.name.encode("utf-8")), 255
                    )
                    self.assertLessEqual(
                        len(f".{artifact.name}.tmp".encode("utf-8")), 255
                    )
                    self.assertEqual(data, artifacts.read_gzip(artifact))
                paths.append(meta.artifact_path)
            self.assertNotEqual(paths[0], paths[1])

    def test_colliding_artifact_names_do_not_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = artifacts.store_stream_output(
                b"first", failed=True, artifact_dir=tmp, artifact_name="a/b"
            )
            second = artifacts.store_stream_output(
                b"second", failed=True, artifact_dir=tmp, artifact_name="a?b"
            )

            self.assertNotEqual(first.artifact_path, second.artifact_path)
            self.assertEqual(b"first", artifacts.read_gzip(first.artifact_path))
            self.assertEqual(
                b"second", artifacts.read_gzip(second.artifact_path)
            )

    def test_sanitized_name_has_no_path_separators(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            meta = artifacts.store_stream_output(
                b"data",
                failed=True,
                artifact_dir=tmp,
                artifact_name="../escape/attempt",
            )

            artifact = Path(meta.artifact_path)
            self.assertEqual(Path(tmp), artifact.parent)
            self.assertNotIn("/", artifact.name.replace(".gz", ""))

    def test_interrupted_write_cleans_up_and_leaves_destination_untouched(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "output.gz"
            target.write_bytes(b"original")

            with mock.patch(
                "agent_session_loop.artifacts.os.replace",
                side_effect=OSError("boom"),
            ):
                with self.assertRaises(artifacts.ArtifactError):
                    artifacts.atomic_write_bytes(target, b"new-data")

            # Destination is unchanged and no stray temp file remains.
            self.assertEqual(b"original", target.read_bytes())
            self.assertEqual(["output.gz"], [p.name for p in Path(tmp).iterdir()])

    def test_gzip_write_is_atomic_and_deterministic(self) -> None:
        data = b"deterministic payload" * 100
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "a.gz"
            second = Path(tmp) / "b.gz"
            artifacts.atomic_write_gzip(first, data)
            artifacts.atomic_write_gzip(second, data)

            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(data, artifacts.read_gzip(first))

    def test_read_gzip_rejects_non_gzip_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bogus = Path(tmp) / "bogus.gz"
            bogus.write_bytes(b"not gzip at all")
            with self.assertRaises(artifacts.ArtifactError):
                artifacts.read_gzip(bogus)

    def test_metadata_roundtrips_through_dict(self) -> None:
        meta = artifacts.store_stream_output(b"payload", failed=False)
        restored = artifacts.OutputMetadata.from_dict(meta.to_dict())
        self.assertEqual(meta, restored)

    def test_metadata_from_dict_rejects_unknown_version(self) -> None:
        payload = artifacts.store_stream_output(b"x", failed=False).to_dict()
        payload["version"] = 999
        with self.assertRaises(artifacts.ArtifactError):
            artifacts.OutputMetadata.from_dict(payload)

    def test_stream_capture_matches_store_stream_output_across_chunkings(
        self,
    ) -> None:
        # Multi-byte characters ensure chunk boundaries split UTF-8 sequences;
        # the incremental capture must still produce metadata identical to the
        # whole-buffer reference for every chunking.
        data = ("héllo wörld ✓" * 3000).encode("utf-8")
        reference = artifacts.store_stream_output(data, failed=False)
        for chunk_size in (1, 7, 1024, len(data)):
            with self.subTest(chunk_size=chunk_size):
                capture = artifacts.StreamCapture()
                for offset in range(0, len(data), chunk_size):
                    capture.feed(data[offset : offset + chunk_size])
                meta = capture.finalize(failed=False)
                self.assertEqual(reference, meta)

    def test_stream_capture_matches_reference_below_preview_bound(self) -> None:
        data = b"short output\n"
        reference = artifacts.store_stream_output(data, failed=False)
        capture = artifacts.StreamCapture()
        capture.feed(data)
        self.assertEqual(reference, capture.finalize(failed=False))

    def test_stream_capture_empty_stream_matches_reference(self) -> None:
        reference = artifacts.store_stream_output(b"", failed=False)
        capture = artifacts.StreamCapture()
        self.assertEqual(reference, capture.finalize(failed=False))

    def test_stream_capture_spool_retains_failed_stream_byte_for_byte(
        self,
    ) -> None:
        data = bytes(range(256)) * 512  # 128 KiB of every byte value
        with tempfile.TemporaryDirectory() as tmp:
            capture = artifacts.StreamCapture(spool=True)
            for offset in range(0, len(data), 4096):
                capture.feed(data[offset : offset + 4096])
            meta = capture.finalize(
                failed=True, artifact_dir=tmp, artifact_name="stdout"
            )

            self.assertIsNotNone(meta.artifact_path)
            artifact = Path(meta.artifact_path)
            self.assertEqual("stdout.gz", artifact.name)
            self.assertEqual(data, artifacts.read_gzip(artifact))
            self.assertEqual(artifacts.sha256_hex(data), meta.sha256)
            # The spooled artifact is byte-identical to the buffered writer's
            # output, so compressed-artifact digests stay stable across runs.
            reference = Path(tmp) / "reference.gz"
            artifacts.atomic_write_gzip(reference, data)
            self.assertEqual(reference.read_bytes(), artifact.read_bytes())

    def test_stream_capture_spool_deleted_when_not_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            capture = artifacts.StreamCapture(spool=True)
            capture.feed(b"passing output")
            spool_path = capture._spool_path
            self.assertIsNotNone(spool_path)

            meta = capture.finalize(
                failed=False, artifact_dir=tmp, artifact_name="stdout"
            )

            self.assertIsNone(meta.artifact_path)
            self.assertEqual([], list(Path(tmp).iterdir()))
            self.assertFalse(spool_path.exists())

    def test_stream_capture_promotion_failure_fails_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "blocker"
            blocker.write_text("not a dir\n", encoding="utf-8")
            capture = artifacts.StreamCapture(spool=True)
            capture.feed(b"failure output")
            spool_path = capture._spool_path

            meta = capture.finalize(
                failed=True,
                artifact_dir=blocker / "sub",
                artifact_name="stdout",
            )

            self.assertIsNone(meta.artifact_path)
            self.assertEqual("failure output", meta.preview)
            self.assertEqual(
                artifacts.sha256_hex(b"failure output"), meta.sha256
            )
            self.assertFalse(spool_path.exists())

    def test_stream_capture_discard_removes_spool(self) -> None:
        capture = artifacts.StreamCapture(spool=True)
        capture.feed(b"aborted")
        spool_path = capture._spool_path
        self.assertIsNotNone(spool_path)

        capture.discard()

        self.assertFalse(spool_path.exists())


class ContractCheckCliTests(unittest.TestCase):
    """`asl contract-check` static per-check contract diagnosis."""

    def _run(self, plan_dir: Path, cwd: Path, *extra: str) -> tuple[int, dict]:
        args = cli.build_parser().parse_args(
            ["contract-check", str(plan_dir), "--cwd", str(cwd), "--json", *extra]
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.resolve_handler(args)(args)
        return code, json.loads(stdout.getvalue())

    def test_parser_wiring(self) -> None:
        args = cli.build_parser().parse_args(
            ["contract-check", "/plan", "--session", "2"]
        )
        self.assertIs(verification.contract_check_from_args, cli.resolve_handler(args))
        self.assertEqual("/plan", args.plan_dir)
        self.assertEqual("2", args.session)
        self.assertEqual(".", args.cwd)
        self.assertFalse(args.json)

    def test_all_ok_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            (repo_root / "target.txt").write_text("hello\n", encoding="utf-8")
            contract = _contract_block(
                '{"type": "file_exists", "path": "target.txt", "name": "f"}, '
                '{"type": "command", "name": "c", "argv": ["true"]}'
            )
            write_plan(plan_dir, ("pending",), contracts_by_id={"1": contract})

            code, payload = self._run(plan_dir, repo_root)

            self.assertEqual(0, code)
            self.assertTrue(payload["ok"])
            self.assertEqual("ok", payload["reason"])
            self.assertEqual(0, payload["issue_count"])
            self.assertEqual(
                {"ok"}, {row["status"] for row in payload["checks"]}
            )
            self.assertEqual([None, None], [row["repo"] for row in payload["checks"]])

    def test_static_contract_row_reports_repository_alias(self) -> None:
        contract = contracts.CompletionContract(
            session_id=plans.parse_session_id("1"),
            done_when=("done",),
            verify=contracts.VerifyRecipe(
                checks=(
                    contracts.CommandCheck(
                        name="c",
                        argv=("true",),
                        timeout_seconds=5,
                        repo="SVC",
                    ),
                )
            ),
        )
        with mock.patch.object(
            verification, "read_completion_contract", return_value=contract
        ):
            rows = verification._contract_check_rows(
                "/plan", plans.parse_session_id("1"), repo_root=Path(".")
            )

        self.assertEqual("SVC", rows[0]["repo"])

    def test_file_missing_is_factual_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            contract = _contract_block(
                '{"type": "file_exists", "path": "absent.txt", "name": "f"}'
            )
            write_plan(plan_dir, ("pending",), contracts_by_id={"1": contract})

            code, payload = self._run(plan_dir, repo_root)

            self.assertEqual(1, code)
            self.assertFalse(payload["ok"])
            self.assertEqual("contract-issues", payload["reason"])
            row = payload["checks"][0]
            self.assertEqual("factual-issue", row["status"])
            self.assertEqual("file-missing", row["reason"])
            self.assertEqual("1", row["session"])

    def test_regex_not_matched_is_factual_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            (repo_root / "target.txt").write_text("nothing here\n", encoding="utf-8")
            contract = _contract_block(
                '{"type": "regex_in_file", "path": "target.txt", '
                '"pattern": "WANTED", "name": "r"}'
            )
            write_plan(plan_dir, ("pending",), contracts_by_id={"1": contract})

            code, payload = self._run(plan_dir, repo_root)

            self.assertEqual(1, code)
            row = payload["checks"][0]
            self.assertEqual("regex-not-matched", row["reason"])

    def test_command_not_runnable_is_factual_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            contract = _contract_block(
                '{"type": "command", "name": "c", '
                '"argv": ["asl-contract-check-missing-xyz"]}'
            )
            write_plan(plan_dir, ("pending",), contracts_by_id={"1": contract})

            code, payload = self._run(plan_dir, repo_root)

            self.assertEqual(1, code)
            row = payload["checks"][0]
            self.assertEqual("command-not-runnable", row["reason"])

    def test_unversioned_python_is_factual_issue(self) -> None:
        # An unversioned ``python`` interpreter is frequently unavailable where
        # ``python3`` exists, so it stays actionable with the ``bare-python``
        # reason and a "use `python3`" detail (matching session-expand).
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            contract = _contract_block(
                '{"type": "command", "name": "c", '
                '"argv": ["python", "-c", "raise SystemExit(0)"]}'
            )
            write_plan(plan_dir, ("pending",), contracts_by_id={"1": contract})

            code, payload = self._run(plan_dir, repo_root)

            self.assertEqual(1, code)
            row = payload["checks"][0]
            self.assertEqual("factual-issue", row["status"])
            self.assertEqual("bare-python", row["reason"])
            self.assertIn("use `python3`", row["detail"])

    def test_versioned_python3_inline_check_is_ok(self) -> None:
        # ``python3 -c`` and ``python3 <script>`` are valid inline evidence
        # checks and must not be rejected solely for lacking ``-m``.
        for argv in (
            '["python3", "-c", "raise SystemExit(0)"]',
            '["python3", "check_evidence.py"]',
            '["python3.11", "-c", "raise SystemExit(0)"]',
        ):
            with self.subTest(argv=argv):
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_dir = Path(tmp)
                    plan_dir = tmp_dir / "plan"
                    repo_root = tmp_dir / "repo"
                    repo_root.mkdir()
                    contract = _contract_block(
                        f'{{"type": "command", "name": "c", "argv": {argv}}}'
                    )
                    write_plan(
                        plan_dir, ("pending",), contracts_by_id={"1": contract}
                    )

                    code, payload = self._run(plan_dir, repo_root)

                    row = payload["checks"][0]
                    self.assertNotEqual("bare-python", row["reason"])

    def test_missing_contract_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            write_plan(plan_dir, ("pending",))

            code, payload = self._run(plan_dir, repo_root)

            self.assertEqual(1, code)
            row = payload["checks"][0]
            self.assertEqual("missing-contract", row["status"])
            self.assertIsNone(row["name"])

    def test_schema_error_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            # A command check with no argv fails the contract parser.
            contract = _contract_block('{"type": "command", "name": "c"}')
            write_plan(plan_dir, ("pending",), contracts_by_id={"1": contract})

            code, payload = self._run(plan_dir, repo_root)

            self.assertEqual(1, code)
            row = payload["checks"][0]
            self.assertEqual("schema-error", row["status"])

    def test_session_flag_narrows_to_single_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            (repo_root / "ok.txt").write_text("x\n", encoding="utf-8")
            good = _contract_block(
                '{"type": "file_exists", "path": "ok.txt", "name": "f"}'
            )
            bad = _contract_block(
                '{"type": "file_exists", "path": "absent.txt", "name": "f"}',
                session="2",
            )
            write_plan(
                plan_dir,
                ("pending", "pending"),
                contracts_by_id={"1": good, "2": bad},
            )

            # Session 1 alone is clean even though session 2 has an issue.
            code, payload = self._run(plan_dir, repo_root, "--session", "1")
            self.assertEqual(0, code)
            self.assertEqual(
                {"1"}, {row["session"] for row in payload["checks"]}
            )
            self.assertEqual("1", payload["session"])

    def test_unknown_session_is_invalid_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            write_plan(plan_dir, ("pending",))

            code, payload = self._run(plan_dir, repo_root, "--session", "9")
            self.assertEqual(1, code)
            self.assertEqual("invalid-session", payload["reason"])


if __name__ == "__main__":
    unittest.main()
