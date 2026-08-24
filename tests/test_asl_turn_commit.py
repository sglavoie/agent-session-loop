"""One canonical commit path publishes an accepted turn exactly once.

Covers the seam where a turn stops being an execution record and becomes an
accepted, immutable statement: which function may emit ``turn_finished``, what
must already have happened when it does, and what the event stream says instead
when a post-execution guard refuses the turn.
"""

from __future__ import annotations

import argparse
import ast
import inspect
import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import MappingProxyType
from typing import Any
from unittest import mock

from agent_session_loop import (
    commit_message,
    events,
    harnesses,
    plans,
    review_repair,
    runner,
    state,
    supervisor_audit,
)
from agent_session_loop import verification as verification_module
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    _contract_block,
    fake_commit_harness_env,
    init_multi_repo_workspace,
    read_jsonl,
    wait_for_process_exit,
    write_multi_repo_plan,
)

SPECS = DEFAULT_MULTI_REPO_SPECS[:2]


def _turn_finished_emitters() -> set[str]:
    """Names of the runner functions that mention the turn_finished event."""
    module = ast.parse(Path(inspect.getsourcefile(runner) or "").read_text("utf-8"))
    emitters: set[str] = set()
    for node in ast.walk(module):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for inner in ast.walk(node):
            names_event = (
                isinstance(inner, ast.Attribute) and inner.attr == "TURN_FINISHED_EVENT"
            ) or (isinstance(inner, ast.Name) and inner.id == "TURN_FINISHED_EVENT")
            names_literal = (
                isinstance(inner, ast.Constant) and inner.value == "turn_finished"
            )
            if names_event or names_literal:
                emitters.add(node.name)
                break
    return emitters


class TurnCommitSourceTests(unittest.TestCase):
    def test_only_the_commit_function_names_the_turn_finished_event(self) -> None:
        self.assertEqual({"_commit_turn"}, _turn_finished_emitters())

    def test_commit_persists_the_result_and_state_before_the_event(self) -> None:
        source = inspect.getsource(runner._commit_turn)
        written = source.index('_write_json_file(paths["result"]')
        state_written = source.index("persistence.write_state(payload)")
        appended = source.index("TURN_FINISHED_EVENT")
        self.assertLess(written, state_written)
        self.assertLess(state_written, appended)

    def test_commit_notifies_consumers_after_the_durable_event(self) -> None:
        source = inspect.getsource(runner._commit_turn)
        appended = source.index("TURN_FINISHED_EVENT")
        reported = source.index("progress.turn_finished(")
        notified = source.index("_notify_agent_mux_loop_turn_finished(")
        self.assertLess(appended, reported)
        self.assertLess(appended, notified)

    def test_the_run_loop_commits_after_every_guard(self) -> None:
        source = inspect.getsource(runner._run_locked)
        commit = source.index("_commit_turn(")
        for earlier in (
            "_enforce_workspace_write_guard(",
            "_enforce_review_mutation_guard(",
            "_enforce_mailbox_completion_guard(",
            "_enforce_session_verification(",
            "_append_progress_entry(",
            "plans.sync_derived_status_ledgers(",
        ):
            self.assertLess(source.index(earlier), commit, earlier)


class CommittedTurnImmutabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.plan_dir = self.tmp_path / "plan"
        write_multi_repo_plan(self.plan_dir, ("complete",), specs=SPECS)
        self.snapshot = plans.inspect_plan(self.plan_dir)

    def _commit(self, turn_payload: dict[str, Any]) -> Any:
        payload: dict[str, Any] = {"run_id": "run-1", "turns": [turn_payload]}
        return runner._commit_turn(
            event_path=self.tmp_path / "events.jsonl",
            payload=payload,
            persistence=runner._RunPersistence(write_state=False),
            progress=runner._ProgressReporter(enabled=False),
            turn_payload=turn_payload,
            live_turn=dict(turn_payload),
            accepted=self.snapshot,
            plan_cache=plans.PlanSnapshotCache(),
            paths={"result": self.tmp_path / "result.json"},
        ), payload

    def test_committed_turn_cannot_be_rewritten(self) -> None:
        committed, _payload = self._commit({"turn_index": 1, "session": "1"})
        self.assertIsInstance(committed, MappingProxyType)
        with self.assertRaises(TypeError):
            committed["session"] = "2"  # type: ignore[index]

    def test_committed_turn_replaces_the_in_flight_state_record(self) -> None:
        turn_payload = {"turn_index": 1, "session": "1"}
        committed, payload = self._commit(turn_payload)
        self.assertEqual(1, len(payload["turns"]))
        self.assertTrue(payload["turns"][0]["committed"])
        self.assertNotIn("committed", turn_payload)
        self.assertEqual(dict(committed), payload["turns"][0])

    def test_committed_turn_carries_the_accepted_snapshot(self) -> None:
        committed, _payload = self._commit(
            {"turn_index": 1, "session": "1", "after": {"stale": True}}
        )
        self.assertEqual(runner._snapshot_payload(self.snapshot), committed["after"])


class TurnCommitRunTests(unittest.TestCase):
    """Full runs, so the assertions are about the durable record, not calls."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.workspace_root = self.tmp_path / "workspace"
        self.repositories = init_multi_repo_workspace(self.workspace_root, SPECS)
        self.plan_dir = self.tmp_path / "plan"
        self.state_dir = self.tmp_path / "state"
        write_multi_repo_plan(
            self.plan_dir,
            ("pending",),
            specs=SPECS,
            contracts_by_id={
                "1": _contract_block(
                    '{"type": "file_exists", "repo": "SVC", '
                    '"path": "README.md", "name": "svc-readme"}',
                    session="1",
                )
            },
        )
        session_path = self.plan_dir / "session-1.md"
        session_path.write_text(
            session_path.read_text(encoding="utf-8").replace(
                "**Reads repositories**: SVC, OAK\n**Writes repositories**: SVC, OAK",
                "**Reads repositories**: SVC\n**Writes repositories**: SVC",
            ),
            encoding="utf-8",
        )
        registry = plans.read_repository_registry(self.plan_dir)
        assert registry is not None
        plans.save_workspace_plan_metadata(
            self.plan_dir, self.workspace_root, registry.digest
        )
        # Real runs carry readiness metadata, so every turn rewrites manifest.md.
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=plans.stable_readiness_sha256(self.plan_dir),
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        self.context = verification_module.VerificationContext.from_plan(
            self.plan_dir, self.workspace_root
        )

    def _invoke(self, run_id: str) -> tuple[int, dict[str, Any]]:
        args = argparse.Namespace(
            plan_dir=str(self.plan_dir),
            cwd=str(self.workspace_root),
            harness="codex",
            state_dir=str(self.state_dir),
            json=True,
            dry_run=False,
            unsafe_bypass=True,
            timeout=None,
            max_sessions=None,
            commit_mode=None,
            model=None,
            effort=None,
            note=None,
            allow_not_loop_ready=True,
        )
        lock_info = state.acquire_lock(self.plan_dir, self.state_dir)
        try:
            return runner._run_locked(
                args=args,
                cwd=self.workspace_root,
                timeout=None,
                max_sessions=None,
                commit_mode=None,
                lock_info=lock_info,
                run_id=run_id,
                verification_context=self.context,
            )
        finally:
            state.release_lock(self.plan_dir, self.state_dir)

    def _records(self) -> list[dict[str, Any]]:
        event_path = (
            state.plan_state_dir(self.plan_dir, self.state_dir)
            / events.EVENTS_FILE_NAME
        )
        return read_jsonl(event_path)

    def _names(self) -> list[str]:
        return [record["name"] for record in self._records()]

    def _payloads(self, name: str) -> list[dict[str, Any]]:
        return [
            record["payload"] for record in self._records() if record["name"] == name
        ]

    def _harness(self, *, writes: str, declares: str):
        """Complete the session by writing one file and one handoff line."""

        def run(*_args: object, **_kwargs: object) -> harnesses.HarnessResult:
            alias, _, relative = writes.partition(":")
            (self.repositories[alias] / relative).write_text(
                "changed by the harness\n", encoding="utf-8"
            )
            session_path = self.plan_dir / "session-1.md"
            lines = session_path.read_text(encoding="utf-8").splitlines(keepends=True)
            for index, line in enumerate(lines):
                if line.startswith(plans.STATUS_PREFIX):
                    lines[index] = f"{plans.STATUS_PREFIX} complete\n"
                elif line.startswith(plans.HANDOFF_FILES_PREFIX):
                    lines[index] = f"{plans.HANDOFF_FILES_PREFIX} {declares}\n"
            session_path.write_text("".join(lines), encoding="utf-8")
            return harnesses.HarnessResult(
                name="codex",
                argv=["fake"],
                returncode=0,
                stdout="ok\n",
                stderr="",
            )

        return run

    def _run_accepted(self) -> dict[str, Any]:
        with mock.patch.object(
            runner.harnesses,
            "run_harness",
            side_effect=self._harness(writes="SVC:README.md", declares="SVC:README.md"),
        ):
            exit_code, payload = self._invoke("commit-run")
        self.assertEqual(0, exit_code)
        self.assertEqual("all-complete", payload["reason"])
        return payload

    def test_acceptance_follows_execution_and_every_guard(self) -> None:
        self._run_accepted()
        names = self._names()
        self.assertEqual(1, names.count(events.TURN_FINISHED_EVENT))
        accepted = names.index(events.TURN_FINISHED_EVENT)
        for earlier in (
            events.TURN_EXECUTION_FINISHED_EVENT,
            events.VERIFICATION_STARTED_EVENT,
            events.VERIFICATION_FINISHED_EVENT,
        ):
            self.assertLess(names.index(earlier), accepted, earlier)
        self.assertLess(accepted, names.index("run_finished"))
        self.assertEqual([], self._payloads(events.TURN_REJECTED_EVENT))

    def test_event_result_and_state_describe_the_same_committed_turn(self) -> None:
        payload = self._run_accepted()
        finished = self._payloads(events.TURN_FINISHED_EVENT)[0]
        self.assertTrue(finished["committed"])
        result_path = Path(str(finished["result_path"]))
        self.assertEqual(json.loads(result_path.read_text(encoding="utf-8")), finished)
        self.assertEqual([finished], payload["turns"])
        state_payload = json.loads(
            Path(str(payload["state_path"])).read_text(encoding="utf-8")
        )
        self.assertEqual(
            finished["turn_index"], state_payload["turns"][0]["turn_index"]
        )
        self.assertTrue(state_payload["turns"][0]["committed"])

    def test_committed_turn_reports_the_guarded_snapshot(self) -> None:
        self._run_accepted()
        finished = self._payloads(events.TURN_FINISHED_EVENT)[0]
        self.assertEqual(
            runner._snapshot_payload(plans.inspect_plan(self.plan_dir)),
            finished["after"],
        )

    def test_authored_session_remains_uncommitted_under_null_mode(self) -> None:
        payload = self._run_accepted()

        self.assertIsNone(payload["commit_mode"])
        self.assertEqual([], payload["commits"])
        self.assertIn(
            "README.md",
            runner._run_git(self.repositories["SVC"], ["status", "--porcelain"]).stdout,
        )

    def _mark_repair_session(self) -> None:
        """Make session 1 a review repair, which owns its own commit."""
        session_path = self.plan_dir / "session-1.md"
        session_path.write_text(
            review_repair.REVIEW_REPAIR_SESSION_MARKER
            + "\n"
            + review_repair.review_repair_session_version_marker()
            + "\n"
            + session_path.read_text(encoding="utf-8"),
            encoding="utf-8",
        )

    def test_registered_repair_commits_only_changed_writable_aliases(self) -> None:
        self._mark_repair_session()
        (self.repositories["OAK"] / "preexisting.txt").write_text(
            "read-only dirty state\n", encoding="utf-8"
        )
        oak_head = runner._read_git_head(self.repositories["OAK"])["head"]
        commit_log = self.tmp_path / "commit-turns.jsonl"
        env = fake_commit_harness_env(self.tmp_path, log_path=commit_log)

        with (
            mock.patch.dict(os.environ, env),
            mock.patch.object(
                runner.harnesses,
                "run_harness",
                side_effect=self._harness(
                    writes="SVC:repair.txt", declares="SVC:repair.txt"
                ),
            ),
        ):
            exit_code, payload = self._invoke("registered-repair-commit")

        self.assertEqual(0, exit_code)
        self.assertIsNone(payload["commit_mode"])
        self.assertEqual(1, len(payload["commits"]))
        self.assertEqual("SVC", payload["commits"][0]["repository_alias"])
        # Only the writable alias ran a message-generation turn, and it ran
        # with that repository as its cwd.
        self.assertEqual(
            [str(self.repositories["SVC"].resolve())],
            [record["cwd"] for record in read_jsonl(commit_log)],
        )
        self.assertEqual(
            "",
            runner._run_git(
                self.repositories["SVC"], ["status", "--porcelain"]
            ).stdout.strip(),
        )
        self.assertEqual(
            oak_head,
            runner._read_git_head(self.repositories["OAK"])["head"],
        )
        self.assertTrue(
            runner._run_git(
                self.repositories["OAK"], ["status", "--porcelain"]
            ).stdout.strip()
        )

    def test_pathspec_commit_carries_only_the_paths_it_names(self) -> None:
        repo = self.repositories["SVC"]
        (repo / "declared.txt").write_text("session work\n", encoding="utf-8")
        (repo / "unrelated.txt").write_text("somebody else's edit\n", encoding="utf-8")
        env = fake_commit_harness_env(self.tmp_path)

        with mock.patch.dict(os.environ, env):
            commit = runner._run_asl_commit(
                cwd=repo,
                run_dir=self.tmp_path / "pathspec-run",
                commit_index=1,
                session_id="1",
                repository_alias="SVC",
                qualified_paths=["SVC:declared.txt"],
                staging_paths=["SVC:declared.txt"],
                generation=runner._CommitGeneration(harness="codex"),
            )

        self.assertEqual(0, commit["returncode"])
        self.assertEqual(["--pathspec", "declared.txt"], commit["argv"][-2:])
        self.assertEqual(
            ["declared.txt"],
            runner._run_git(
                repo, ["show", "--name-only", "--format=", "HEAD"]
            ).stdout.split(),
        )
        # A pathspec commit only promises the paths it names, so the file the
        # turn never touched survives the commit as it was — which is also why
        # the probe reports `dirty-after-commit` rather than success.
        self.assertEqual(
            "?? unrelated.txt",
            runner._run_git(repo, ["status", "--porcelain"]).stdout.strip(),
        )
        self.assertEqual("dirty-after-commit", commit["status_result"])
        self.assertFalse(commit["success"])

    def test_pathspec_commit_leaves_a_preexisting_index_entry_staged(self) -> None:
        repo = self.repositories["SVC"]
        (repo / "declared.txt").write_text("session work\n", encoding="utf-8")
        (repo / "unrelated.txt").write_text("somebody else's edit\n", encoding="utf-8")
        # Somebody staged their own work before the run's commit boundary.
        runner._run_git(repo, ["add", "--", "unrelated.txt"])
        env = fake_commit_harness_env(self.tmp_path)

        with mock.patch.dict(os.environ, env):
            commit = runner._run_asl_commit(
                cwd=repo,
                run_dir=self.tmp_path / "prestaged-run",
                commit_index=1,
                session_id="1",
                repository_alias="SVC",
                qualified_paths=["SVC:declared.txt"],
                staging_paths=["SVC:declared.txt"],
                generation=runner._CommitGeneration(harness="codex"),
            )

        self.assertEqual(0, commit["returncode"])
        self.assertEqual(
            ["declared.txt"],
            runner._run_git(
                repo, ["show", "--name-only", "--format=", "HEAD"]
            ).stdout.split(),
        )
        # The pre-staged path was neither committed nor unstaged by the scoped
        # commit; it is still staged, now against the new HEAD.
        self.assertEqual(
            "A  unrelated.txt",
            runner._run_git(repo, ["status", "--porcelain"]).stdout.strip(),
        )

    def test_runner_commit_generation_is_bounded(self) -> None:
        # Every run-owned commit carries a bound, including the default
        # selection a run that named no harness produces: an unbounded nested
        # commit is what turns one stalled message harness into a stalled loop.
        default_argv = runner._asl_commit_argv(runner._CommitGeneration())
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
            runner._commit_generation(unset).timeout,
        )

        repo = self.repositories["SVC"]
        (repo / "declared.txt").write_text("session work\n", encoding="utf-8")
        pid_path = self.tmp_path / "hang-child.pid"
        env = fake_commit_harness_env(self.tmp_path)
        env["FAKE_CODEX_COMMIT_MODE"] = "hang"
        env["FAKE_CODEX_COMMIT_HANG_CHILD_PID"] = str(pid_path)

        started = time.monotonic()
        with mock.patch.dict(os.environ, env):
            commit = runner._run_asl_commit(
                cwd=repo,
                run_dir=self.tmp_path / "bounded-run",
                commit_index=1,
                session_id="1",
                repository_alias="SVC",
                qualified_paths=["SVC:declared.txt"],
                staging_paths=["SVC:declared.txt"],
                generation=runner._CommitGeneration(harness="codex", timeout=2),
            )
        elapsed = time.monotonic() - started

        # The nested command enforced its own bound and exited with a
        # classified failure, so the run never reached its outer backstop.
        self.assertLess(elapsed, 120, "the nested commit outlived its bound")
        argv = list(commit["argv"])
        self.assertEqual("2", argv[argv.index("--timeout") + 1])
        self.assertEqual(2, commit["generation_timeout_seconds"])
        self.assertFalse(commit["timed_out"])
        self.assertNotEqual(0, commit["returncode"])
        self.assertEqual("nonzero-exit", commit["status_result"])
        self.assertFalse(commit["success"])
        stderr = Path(str(commit["stderr_path"])).read_text(encoding="utf-8")
        self.assertIn("did not produce a commit message within 2s", stderr)
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
            mock.patch.object(runner, "_asl_commit_argv", return_value=stub),
            mock.patch.object(runner, "COMMIT_SUBPROCESS_GRACE_SECONDS", 1),
        ):
            commit = runner._run_asl_commit(
                cwd=repo,
                run_dir=self.tmp_path / "wedged-run",
                commit_index=1,
                session_id="1",
                repository_alias="SVC",
                generation=runner._CommitGeneration(harness="codex", timeout=1),
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
            mock.patch.object(runner, "_asl_commit_argv", return_value=stub),
            mock.patch.object(runner, "COMMIT_SUBPROCESS_GRACE_SECONDS", 1),
        ):
            commit = runner._run_asl_commit(
                cwd=repo,
                run_dir=self.tmp_path / "detached-run",
                commit_index=1,
                session_id="1",
                repository_alias="SVC",
                generation=runner._CommitGeneration(harness="codex", timeout=1),
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

    def test_ignored_evidence_is_recorded_but_never_staged(self) -> None:
        self._mark_repair_session()
        repo = self.repositories["SVC"]
        # A `.gitignore` naming itself is invisible to the untracked scan, so
        # rewriting it leaves `worktree_ignore` as its only evidence; the same
        # goes for `.git/info/exclude`, which no worktree scan reaches at all.
        ignore_path = repo / ".gitignore"
        ignore_path.write_text(".gitignore\ncache/\n", encoding="utf-8")
        exclude_path = repo / ".git" / "info" / "exclude"
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        exclude_path.write_text("# baseline\n", encoding="utf-8")
        self.assertEqual(
            "", runner._run_git(repo, ["status", "--porcelain"]).stdout.strip()
        )
        commit_log = self.tmp_path / "commit-turns.jsonl"
        env = fake_commit_harness_env(self.tmp_path, log_path=commit_log)
        complete_session = self._harness(
            writes="SVC:repair.txt", declares="SVC:repair.txt"
        )

        def run(*args: object, **kwargs: object) -> harnesses.HarnessResult:
            ignore_path.write_text(".gitignore\ncache/\nbuild/\n", encoding="utf-8")
            exclude_path.write_text("# baseline\n*.tmp\n", encoding="utf-8")
            return complete_session(*args, **kwargs)

        with (
            mock.patch.dict(os.environ, env),
            mock.patch.object(runner.harnesses, "run_harness", side_effect=run),
        ):
            exit_code, payload = self._invoke("ignored-evidence-commit")

        self.assertEqual(0, exit_code)
        self.assertEqual(1, len(payload["commits"]))
        commit = payload["commits"][0]
        # The guard still reports both ignore sources — dropping them from the
        # evidence would let a turn hide behind the rules it writes.
        self.assertEqual(
            ["SVC:repair.txt", "SVC:.gitignore", "SVC:.git/info/exclude"],
            commit["qualified_paths"],
        )
        self.assertEqual(["SVC:repair.txt"], commit["staging_paths"])
        self.assertEqual(["--pathspec", "repair.txt"], commit["argv"][-2:])
        self.assertEqual(
            ["repair.txt"],
            runner._run_git(
                repo, ["show", "--name-only", "--format=", "HEAD"]
            ).stdout.split(),
        )
        self.assertEqual("success", commit["status_result"])
        self.assertEqual(
            ".gitignore\ncache/\nbuild/\n", ignore_path.read_text(encoding="utf-8")
        )
        self.assertEqual(
            [str(self.repositories["SVC"].resolve())],
            [record["cwd"] for record in read_jsonl(commit_log)],
        )

    def test_turn_finished_digest_matches_stored_result_json(self) -> None:
        payload = self._run_accepted()
        self.assertTrue(payload["readiness_state_active"])
        finished = self._payloads(events.TURN_FINISHED_EVENT)[0]
        stored = json.loads(
            Path(str(finished["result_path"])).read_text(encoding="utf-8")
        )
        self.assertEqual(stored["after"], finished["after"])
        self.assertEqual(
            plans.plan_content_sha256(self.plan_dir),
            finished["after"]["content_sha256"],
        )
        self.assertEqual(finished["after"], payload["final_snapshot"])

    def test_scope_violation_is_rejected_and_reopened_without_acceptance(self) -> None:
        with mock.patch.object(
            runner.harnesses,
            "run_harness",
            side_effect=self._harness(writes="OAK:README.md", declares="(none)"),
        ):
            exit_code, payload = self._invoke("rejected-run")

        self.assertNotEqual(0, exit_code)
        self.assertTrue(payload["paused"])
        names = self._names()
        self.assertIn(events.TURN_EXECUTION_FINISHED_EVENT, names)
        self.assertNotIn(events.TURN_FINISHED_EVENT, names)

        rejections = self._payloads(events.TURN_REJECTED_EVENT)
        self.assertEqual(1, len(rejections))
        self.assertEqual("workspace-write-guard", rejections[0]["guard"])
        self.assertEqual(payload["pause_reason"], rejections[0]["reason"])
        self.assertTrue(rejections[0]["reopened"])

        reopened = self._payloads(events.SESSION_REOPENED_EVENT)
        self.assertEqual(1, len(reopened))
        self.assertEqual("complete", reopened[0]["from_status"])
        self.assertEqual("pending", reopened[0]["to_status"])
        self.assertEqual("workspace-write-guard", reopened[0]["guard"])
        self.assertLess(
            names.index(events.TURN_REJECTED_EVENT),
            names.index(events.SESSION_REOPENED_EVENT),
        )
        self.assertEqual(
            "pending", plans.inspect_plan(self.plan_dir).sessions[0].status
        )

    def test_unverifiable_turn_is_rejected_and_reopened(self) -> None:
        with (
            mock.patch.object(
                runner.harnesses,
                "run_harness",
                side_effect=self._harness(
                    writes="SVC:README.md", declares="SVC:README.md"
                ),
            ),
            mock.patch.object(
                runner.verification,
                "run_verification",
                side_effect=OSError("verifier unavailable"),
            ),
        ):
            exit_code, payload = self._invoke("verify-run")

        self.assertNotEqual(0, exit_code)
        self.assertEqual("verify-failed", payload["pause_reason"])
        self.assertNotIn(events.TURN_FINISHED_EVENT, self._names())
        rejections = self._payloads(events.TURN_REJECTED_EVENT)
        self.assertEqual(1, len(rejections))
        self.assertEqual("session-verification", rejections[0]["guard"])
        self.assertEqual("verify-failed", rejections[0]["reason"])
        self.assertIn("verifier unavailable", rejections[0]["detail"]["error"])
        self.assertTrue(rejections[0]["reopened"])
        self.assertEqual(1, len(self._payloads(events.SESSION_REOPENED_EVENT)))

    def test_publication_drift_pauses_instead_of_crashing_the_loop(self) -> None:
        rejection = runner._TurnCommitRejected(
            reason="unexpected-status-change",
            message="session statuses moved",
            detail={"status_changes": []},
        )
        with (
            mock.patch.object(
                runner.harnesses,
                "run_harness",
                side_effect=self._harness(
                    writes="SVC:README.md", declares="SVC:README.md"
                ),
            ),
            mock.patch.object(runner, "_publication_snapshot", side_effect=rejection),
        ):
            exit_code, payload = self._invoke("drift-run")

        self.assertNotEqual(0, exit_code)
        self.assertEqual("unexpected-status-change", payload["pause_reason"])
        self.assertNotIn(events.TURN_FINISHED_EVENT, self._names())
        rejections = self._payloads(events.TURN_REJECTED_EVENT)
        self.assertEqual(1, len(rejections))
        self.assertEqual("ledger-sync", rejections[0]["guard"])
        self.assertEqual("unexpected-status-change", rejections[0]["reason"])

    def test_untouched_session_is_rejected_without_a_reopen(self) -> None:
        def idle(*_args: object, **_kwargs: object) -> harnesses.HarnessResult:
            return harnesses.HarnessResult(
                name="codex", argv=["fake"], returncode=0, stdout="", stderr=""
            )

        with mock.patch.object(runner.harnesses, "run_harness", side_effect=idle):
            exit_code, payload = self._invoke("idle-run")

        self.assertNotEqual(0, exit_code)
        self.assertEqual("no-status-change", payload["pause_reason"])
        self.assertNotIn(events.TURN_FINISHED_EVENT, self._names())
        rejections = self._payloads(events.TURN_REJECTED_EVENT)
        self.assertEqual(1, len(rejections))
        self.assertEqual("status-change", rejections[0]["guard"])
        self.assertEqual("no-status-change", rejections[0]["reason"])
        self.assertFalse(rejections[0]["reopened"])
        self.assertEqual([], self._payloads(events.SESSION_REOPENED_EVENT))


class CommitFailureProgressTests(unittest.TestCase):
    """What an operator watching the run is told when a commit is refused.

    The failure used to announce itself by ending the run. Now the progress
    journal is where it is announced, so these are the sentences that carry it.
    """

    def _emitted(self, call: Any) -> str:
        stream = io.StringIO()
        progress = runner._ProgressReporter(enabled=True)
        with redirect_stderr(stream):
            call(progress)
        return stream.getvalue()

    def test_a_retried_failure_says_the_run_continues(self) -> None:
        text = self._emitted(
            lambda progress: progress.commit_failed(
                {
                    "session_id": "2",
                    "failed_alias": "SVC",
                    "error": "spec commit refused",
                    "retrying": True,
                }
            )
        )

        self.assertIn("commit failed: session 2 (SVC): spec commit refused", text)
        self.assertIn("retrying this group at the next commit boundary", text)

    def test_an_abandoned_failure_says_the_changes_are_uncommitted(self) -> None:
        text = self._emitted(
            lambda progress: progress.commit_failed(
                {
                    "session_id": None,
                    "failed_alias": None,
                    "message": "final auto-commit failed",
                    "retrying": False,
                }
            )
        )

        self.assertIn("commit failed: end-of-run: final auto-commit failed", text)
        self.assertIn("leaving these changes uncommitted", text)

    def test_the_final_summary_warns_without_failing_the_run(self) -> None:
        text = self._emitted(
            lambda progress: progress.run_finished(
                {
                    "ok": True,
                    "reason": "all-complete",
                    "run_id": "run-1",
                    "duration_seconds": 1.0,
                    runner.UNRESOLVED_COMMIT_GROUPS_KEY: [
                        {"session_id": "1", "failed_alias": "SVC"},
                        {"session_id": None, "failed_alias": None},
                    ],
                }
            )
        )

        self.assertIn(
            "warning: 2 commit group(s) were never completed: "
            "session 1 (SVC), end-of-run",
            text,
        )
        # The warning is non-fatal: the run still reports that it finished.
        self.assertIn("run finished: run run-1, reason all-complete", text)


class CommitFailureSoftFailTests(unittest.TestCase):
    """A refused commit costs a run its commits, not its remaining sessions.

    Every assertion here is about a full two-session run, because the promise
    is about what happens *after* the failure: the next session still runs, the
    owed group is retried before it does, and a run that never recovers ends by
    saying so instead of by stopping.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.workspace_root = self.tmp_path / "workspace"
        self.repositories = init_multi_repo_workspace(self.workspace_root, SPECS)
        self.plan_dir = self.tmp_path / "plan"
        self.state_dir = self.tmp_path / "state"
        write_multi_repo_plan(
            self.plan_dir,
            ("pending", "pending"),
            specs=SPECS,
            contracts_by_id={
                session: _contract_block(
                    '{"type": "file_exists", "repo": "SVC", '
                    '"path": "README.md", "name": "svc-readme"}',
                    session=session,
                )
                for session in ("1", "2")
            },
        )
        registry = plans.read_repository_registry(self.plan_dir)
        assert registry is not None
        plans.save_workspace_plan_metadata(
            self.plan_dir, self.workspace_root, registry.digest
        )
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=plans.stable_readiness_sha256(self.plan_dir),
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        self.context = verification_module.VerificationContext.from_plan(
            self.plan_dir, self.workspace_root
        )
        self.commit_log = self.tmp_path / "commit-turns.jsonl"

    def _harness(self, *_args: object, **_kwargs: object) -> harnesses.HarnessResult:
        """Complete whichever session is pending with a change of its own."""
        pending = next(
            session
            for session in plans.inspect_plan(self.plan_dir).sessions
            if session.status == "pending"
        )
        relative = f"session-{pending.session_id}.txt"
        (self.repositories["SVC"] / relative).write_text(
            f"work for session {pending.session_id}\n", encoding="utf-8"
        )
        session_path = self.plan_dir / f"session-{pending.session_id}.md"
        lines = session_path.read_text(encoding="utf-8").splitlines(keepends=True)
        for index, line in enumerate(lines):
            if line.startswith(plans.STATUS_PREFIX):
                lines[index] = f"{plans.STATUS_PREFIX} complete\n"
            elif line.startswith(plans.HANDOFF_FILES_PREFIX):
                lines[index] = f"{plans.HANDOFF_FILES_PREFIX} SVC:{relative}\n"
        session_path.write_text("".join(lines), encoding="utf-8")
        return harnesses.HarnessResult(
            name="codex", argv=["fake"], returncode=0, stdout="ok\n", stderr=""
        )

    def _run(self, run_id: str, *, fail_limit: int | None = None) -> dict[str, Any]:
        env = fake_commit_harness_env(self.tmp_path, log_path=self.commit_log)
        env["FAKE_CODEX_COMMIT_MODE"] = "failure"
        if fail_limit is not None:
            env["FAKE_CODEX_COMMIT_FAIL_LIMIT"] = str(fail_limit)
        args = argparse.Namespace(
            plan_dir=str(self.plan_dir),
            cwd=str(self.workspace_root),
            harness="codex",
            state_dir=str(self.state_dir),
            json=True,
            dry_run=False,
            unsafe_bypass=True,
            timeout=None,
            max_sessions=None,
            commit_mode=runner.COMMIT_MODE_PER_SESSION,
            model=None,
            effort=None,
            note=None,
            allow_not_loop_ready=True,
        )
        lock_info = state.acquire_lock(self.plan_dir, self.state_dir)
        try:
            with (
                mock.patch.dict(os.environ, env),
                mock.patch.object(
                    runner.harnesses, "run_harness", side_effect=self._harness
                ),
            ):
                exit_code, payload = runner._run_locked(
                    args=args,
                    cwd=self.workspace_root,
                    timeout=None,
                    max_sessions=None,
                    commit_mode=runner.COMMIT_MODE_PER_SESSION,
                    lock_info=lock_info,
                    run_id=run_id,
                    verification_context=self.context,
                )
        finally:
            state.release_lock(self.plan_dir, self.state_dir)
        self.exit_code = exit_code
        return payload

    def _records(self) -> list[dict[str, Any]]:
        event_path = (
            state.plan_state_dir(self.plan_dir, self.state_dir)
            / events.EVENTS_FILE_NAME
        )
        return read_jsonl(event_path)

    def test_a_refused_commit_advances_to_the_next_session(self) -> None:
        payload = self._run("soft-fail-run")

        self.assertEqual(0, self.exit_code)
        self.assertFalse(payload["paused"])
        self.assertEqual("all-complete", payload["reason"])
        self.assertEqual([1, 2], payload["completed_sessions"])
        self.assertEqual(
            ["complete", "complete"],
            [session.status for session in plans.inspect_plan(self.plan_dir).sessions],
        )
        names = [record["name"] for record in self._records()]
        self.assertEqual(2, names.count(events.TURN_FINISHED_EVENT))
        self.assertNotIn("run_paused", names)
        # The turn executed, verified, and completed its session; only the
        # commit of its changes failed, so no guard rejected it.
        self.assertNotIn(events.TURN_REJECTED_EVENT, names)

    def test_the_failure_is_recorded_on_the_turn_and_on_the_run(self) -> None:
        payload = self._run("soft-fail-record-run")

        failures = payload["commit_failures"]
        self.assertEqual({"session"}, {record["scope"] for record in failures})
        self.assertEqual({"1", "2"}, {record["session_id"] for record in failures})
        first = failures[0]
        self.assertIn("auto-commit failed after session 1", first["message"])
        self.assertFalse(first["commit"]["success"])
        self.assertEqual(
            "session-commit", payload["turns"][0]["session_commit"]["guard"]
        )
        self.assertEqual(
            "commit-failed", payload["turns"][0]["session_commit"]["reason"]
        )

    def test_an_unresolved_group_is_reported_by_the_run_that_lost_it(self) -> None:
        payload = self._run("soft-fail-report-run")

        summaries = payload["unresolved_commit_groups"]
        self.assertEqual({"1", "2"}, {str(row["session_id"]) for row in summaries})
        self.assertEqual({"session"}, {row["scope"] for row in summaries})
        self.assertEqual({"SVC"}, {row["failed_alias"] for row in summaries})
        # The same record reaches the state file the supervisor audit reads.
        persisted = json.loads(
            Path(str(payload["state_path"])).read_text(encoding="utf-8")
        )
        self.assertEqual(summaries, persisted["unresolved_commit_groups"])
        self.assertEqual(
            "SVC",
            runner._unresolved_commit_groups(persisted)[0]["failed_alias"],
        )

    def test_a_transient_failure_heals_at_the_next_commit_boundary(self) -> None:
        payload = self._run("soft-fail-retry-run", fail_limit=1)

        self.assertEqual(0, self.exit_code)
        self.assertEqual([1, 2], payload["completed_sessions"])
        # One failure, retried before the next session ran, and nothing owed.
        self.assertEqual(1, len(payload["commit_failures"]))
        self.assertEqual([], payload["unresolved_commit_groups"])
        self.assertTrue(payload["commit_group_recovery"]["success"])
        self.assertEqual(
            "complete", payload["commit_group_recovery"]["group"]["status"]
        )
        records = self._records()
        recovered = [
            index
            for index, record in enumerate(records)
            if record["name"] == events.COMMIT_FINISHED_EVENT
            and record["payload"].get("success")
        ][0]
        session_2_started = [
            index
            for index, record in enumerate(records)
            if record["name"] == "turn_started"
            and str(record["payload"].get("session")) == "2"
        ][0]
        self.assertLess(recovered, session_2_started)
        self.assertEqual(
            "",
            runner._run_git(
                self.repositories["SVC"], ["status", "--porcelain"]
            ).stdout.strip(),
        )

    def test_a_run_stops_retrying_one_group_after_its_budget(self) -> None:
        payload = self._run("soft-fail-budget-run")

        # A repository that refuses every time must not turn the session loop
        # into a commit loop: each group is attempted its budget and no more.
        by_group: dict[str, int] = {}
        for record in payload["commit_failures"]:
            group_id = str(record["group_id"])
            by_group[group_id] = by_group.get(group_id, 0) + 1
        self.assertTrue(by_group)
        for attempts in by_group.values():
            self.assertLessEqual(attempts, runner.MAX_COMMIT_GROUP_RETRIES + 1)


class PublicationSnapshotTests(unittest.TestCase):
    """The snapshot a turn is published against is re-read, and must still agree."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.plan_dir = Path(self._tmp.name) / "plan"
        write_multi_repo_plan(self.plan_dir, ("pending",), specs=SPECS)
        self.snapshot = plans.inspect_plan(self.plan_dir)

    def publish(self) -> plans.PlanSnapshot:
        return runner._publication_snapshot(plans.PlanSnapshotCache(), self.snapshot)

    def test_readiness_bookkeeping_is_republished_without_moving_identity(self) -> None:
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="running",
                readiness_sha256=plans.stable_readiness_sha256(self.plan_dir),
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        published = self.publish()
        self.assertEqual(
            plans.plan_content_sha256(self.plan_dir), published.content_sha256
        )
        self.assertEqual(self.snapshot.content_sha256, published.content_sha256)

    def test_status_drift_refuses_the_turn(self) -> None:
        session_path = self.plan_dir / "session-1.md"
        session_path.write_text(
            session_path.read_text(encoding="utf-8").replace(
                "- **Status**: pending", "- **Status**: complete"
            ),
            encoding="utf-8",
        )
        with self.assertRaises(runner._TurnCommitRejected) as caught:
            self.publish()
        self.assertEqual("unexpected-status-change", caught.exception.reason)
        self.assertEqual(
            [{"number": 1, "before": "pending", "after": "complete"}],
            caught.exception.detail["status_changes"],
        )

    def test_unreadable_plan_refuses_the_turn_rather_than_raising_plan_error(
        self,
    ) -> None:
        (self.plan_dir / "session-1.md").write_text("no handoff\n", encoding="utf-8")
        with self.assertRaises(runner._TurnCommitRejected) as caught:
            self.publish()
        self.assertEqual("malformed-plan", caught.exception.reason)
        self.assertTrue(caught.exception.detail["error"])


class PlanMutationReinspectionTests(unittest.TestCase):
    """A runner-owned plan rewrite is re-read before it is reported."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.plan_dir = Path(self._tmp.name) / "plan"
        write_multi_repo_plan(self.plan_dir, ("pending",), specs=SPECS)
        self.snapshot = plans.inspect_plan(self.plan_dir)

    def test_reinspection_reports_the_rewritten_plan(self) -> None:
        session_path = self.plan_dir / "session-1.md"
        session_path.write_text(
            session_path.read_text(encoding="utf-8").replace(
                "- **Status**: pending", "- **Status**: complete"
            ),
            encoding="utf-8",
        )
        refreshed = runner._reinspect_after_plan_mutation(
            plans.PlanSnapshotCache(), self.snapshot
        )
        self.assertEqual("pending", self.snapshot.sessions[0].status)
        self.assertEqual("complete", refreshed.sessions[0].status)

    def test_unparseable_rewrite_keeps_the_pre_mutation_snapshot(self) -> None:
        (self.plan_dir / "session-1.md").write_text("no handoff\n", encoding="utf-8")
        refreshed = runner._reinspect_after_plan_mutation(
            plans.PlanSnapshotCache(), self.snapshot
        )
        self.assertIs(self.snapshot, refreshed)


class TurnAnomalyClassificationTests(unittest.TestCase):
    """A clean exit code is not on its own evidence that a turn succeeded."""

    def test_unreadable_handoff_is_anomalous(self) -> None:
        self.assertTrue(
            supervisor_audit._turn_shows_failure(
                {"returncode": 0, "plan_error": "session-1.md: no handoff"}
            )
        )

    def test_failure_status_result_is_anomalous(self) -> None:
        self.assertTrue(
            supervisor_audit._turn_shows_failure(
                {"returncode": 0, "status_result": "no-status-change"}
            )
        )

    def test_accepted_turn_is_not_anomalous(self) -> None:
        self.assertFalse(
            supervisor_audit._turn_shows_failure(
                {"returncode": 0, "status_result": "complete", "plan_error": None}
            )
        )

    def test_turn_without_a_status_result_is_judged_on_the_generic_signals(
        self,
    ) -> None:
        self.assertFalse(supervisor_audit._turn_shows_failure({"returncode": 0}))
        self.assertTrue(supervisor_audit._turn_shows_failure({"returncode": 1}))


if __name__ == "__main__":
    unittest.main()
