"""Commit failure reporting, soft failure and recovery."""

from __future__ import annotations

import argparse
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    commit_groups,
    commit_settlement,
    events,
    harnesses,
    plans_content_hash,
    plans_handoff,
    plans_metadata,
    plans_models,
    plans_readiness,
    plans_registry,
    plans_session_scope,
    plans_snapshot,
    review_guard,
    run_loop,
    run_reporting,
    run_selection,
    state,
    state_locks,
)
from agent_session_loop import verification_models as verification_module
from tests.asl_turn_commit_support import SPECS
from tests.support import (
    _contract_block,
    fake_commit_harness_env,
    init_multi_repo_workspace,
    read_jsonl,
    write_multi_repo_plan,
)


class CommitFailureProgressTests(unittest.TestCase):
    """What an operator watching the run is told when a commit is refused.

    The failure used to announce itself by ending the run. Now the progress
    journal is where it is announced, so these are the sentences that carry it.
    """

    def _emitted(self, call: Any) -> str:
        stream = io.StringIO()
        progress = run_reporting._ProgressReporter(enabled=True)
        with redirect_stderr(stream):
            call(progress)
        return stream.getvalue()

    def _emitted_by_the_run(self, call: Any) -> str:
        """Emit through the labeller ``run_loop`` actually injects.

        The reporter's own default is a second implementation of the same
        sentence, pinned equal in ``tests/test_asl_run_reporting.py``. A test
        about which group a line names has to read the one the run installs,
        or it would pass on the copy while the run printed the other.
        """
        stream = io.StringIO()
        progress = run_reporting._ProgressReporter(
            enabled=True,
            commit_group_label=commit_settlement._unresolved_commit_group_label,
            unresolved_groups_key=commit_settlement.UNRESOLVED_COMMIT_GROUPS_KEY,
        )
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
                    commit_settlement.UNRESOLVED_COMMIT_GROUPS_KEY: [
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

    def test_unresolved_paused_group_label_names_paused_scope(self) -> None:
        # All three group scopes in one summary, because the point is that the
        # operator can tell them apart: the commit a session's verification
        # earned, the commit a recoverable pause settled for the work that
        # session's failed turn retained, and the end-of-run sweep. Naming the
        # middle one "session 2" hid whether the lost commit covered verified
        # work or work no verification ever passed.
        text = self._emitted_by_the_run(
            lambda progress: progress.run_finished(
                {
                    "ok": False,
                    "pause_reason": "verify-failed",
                    "run_id": "run-2",
                    "duration_seconds": 1.0,
                    commit_settlement.UNRESOLVED_COMMIT_GROUPS_KEY: [
                        {
                            "scope": commit_groups.GROUP_SCOPE_SESSION,
                            "session_id": "2",
                            "failed_alias": "SVC",
                        },
                        {
                            "scope": commit_groups.GROUP_SCOPE_SESSION_PAUSED,
                            "session_id": "2",
                            "failed_alias": "SVC",
                        },
                        {
                            "scope": commit_groups.GROUP_SCOPE_END,
                            "session_id": None,
                            "failed_alias": None,
                        },
                    ],
                }
            )
        )

        self.assertIn(
            "warning: 3 commit group(s) were never completed: "
            "session 2 (SVC), session 2 retained work (SVC), end-of-run",
            text,
        )

    def test_a_paused_group_failure_names_the_retained_work(self) -> None:
        # The same distinction on the failure line, which is where an operator
        # meets the paused-work group first: a refused settlement says it was
        # the retained work that stayed uncommitted.
        text = self._emitted_by_the_run(
            lambda progress: progress.commit_failed(
                commit_settlement._commit_failure_record(
                    scope=commit_groups.GROUP_SCOPE_SESSION_PAUSED,
                    session_id="2",
                    group=None,
                    commit=None,
                    error="paused-work commit refused",
                    message="paused-work commit failed",
                )
            )
        )

        self.assertIn(
            "commit failed: session 2 retained work: paused-work commit refused",
            text,
        )


class CommitFailureSoftFailTests(unittest.TestCase):
    """Commit recovery precedes the required-tree check for the next worker.

    A transient failure recovers and continues. A persistent failure leaving a
    needed tree dirty pauses before another worker can launch.
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
        registry = plans_registry.read_repository_registry(self.plan_dir)
        assert registry is not None
        plans_metadata.save_workspace_plan_metadata(
            self.plan_dir, self.workspace_root, registry.digest
        )
        plans_readiness.write_readiness_metadata(
            self.plan_dir,
            plans_models.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=plans_content_hash.stable_readiness_sha256(
                    self.plan_dir
                ),
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
            for session in plans_snapshot.inspect_plan(self.plan_dir).sessions
            if session.status == "pending"
        )
        relative = f"session-{pending.session_id}.txt"
        (self.repositories["SVC"] / relative).write_text(
            f"work for session {pending.session_id}\n", encoding="utf-8"
        )
        session_path = self.plan_dir / f"session-{pending.session_id}.md"
        lines = session_path.read_text(encoding="utf-8").splitlines(keepends=True)
        for index, line in enumerate(lines):
            if line.startswith(plans_handoff.STATUS_PREFIX):
                lines[index] = f"{plans_handoff.STATUS_PREFIX} complete\n"
            elif line.startswith(plans_handoff.HANDOFF_FILES_PREFIX):
                lines[index] = f"{plans_handoff.HANDOFF_FILES_PREFIX} SVC:{relative}\n"
        session_path.write_text("".join(lines), encoding="utf-8")
        return harnesses.HarnessResult(
            name="codex", argv=["fake"], returncode=0, stdout="ok\n", stderr=""
        )

    def _run(
        self,
        run_id: str,
        *,
        fail_limit: int | None = None,
        max_sessions: int | None = None,
    ) -> dict[str, Any]:
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
            max_sessions=max_sessions,
            commit_mode=run_selection.COMMIT_MODE_PER_SESSION,
            model=None,
            effort=None,
            note=None,
            max_pending_sessions=30,
            allow_not_loop_ready=True,
        )
        lock_info = state_locks.acquire_lock(self.plan_dir, self.state_dir)
        try:
            with (
                mock.patch.dict(os.environ, env),
                mock.patch.object(harnesses, "run_harness", side_effect=self._harness),
            ):
                exit_code, payload = run_loop.run_locked(
                    args=args,
                    cwd=self.workspace_root,
                    timeout=None,
                    max_sessions=max_sessions,
                    commit_mode=run_selection.COMMIT_MODE_PER_SESSION,
                    lock_info=lock_info,
                    run_id=run_id,
                    verification_context=self.context,
                )
        finally:
            state_locks.release_lock(self.plan_dir, self.state_dir)
        self.exit_code = exit_code
        return payload

    def _records(self) -> list[dict[str, Any]]:
        event_path = (
            state.plan_state_dir(self.plan_dir, self.state_dir)
            / events.EVENTS_FILE_NAME
        )
        return read_jsonl(event_path)

    def test_a_refused_commit_blocks_the_next_needed_dirty_session(self) -> None:
        """The run stops, and it stops as the commit failure it actually was.

        This repository refuses every commit, so the group spends its whole
        retry budget and the run ends before a second worker launches. The
        reason is ``commit-failed``: the dirt the next session's tree check
        would object to is the run's own uncommitted work, and naming the tree
        instead would report a bounded commit failure as an environment for a
        human to clean up. Both reasons are environment-blocked, so what this
        pins is which of the two the supervisor is told about.
        """
        payload = self._run("soft-fail-run")

        self.assertEqual(1, self.exit_code)
        self.assertTrue(payload["paused"])
        self.assertEqual("commit-failed", payload["reason"])
        # Stopping with a retry unspent is the failure mode this guards: the
        # tree check the next session brings sees the failed group's dirt, so it
        # used to refuse before the last attempt was ever made.
        self.assertEqual(
            commit_settlement.MAX_COMMIT_GROUP_RETRIES + 1,
            len(payload["commit_failures"]),
        )
        self.assertEqual([1], payload["completed_sessions"])
        self.assertEqual(
            ["complete", "pending"],
            [
                session.status
                for session in plans_snapshot.inspect_plan(self.plan_dir).sessions
            ],
        )
        names = [record["name"] for record in self._records()]
        self.assertEqual(1, names.count(events.TURN_FINISHED_EVENT))
        self.assertIn("run_paused", names)
        # The turn executed, verified, and completed its session; only the
        # commit of its changes failed, so no guard rejected it.
        self.assertNotIn(events.TURN_REJECTED_EVENT, names)

    def test_the_failure_is_recorded_on_the_turn_and_on_the_run(self) -> None:
        payload = self._run("soft-fail-record-run")

        failures = payload["commit_failures"]
        self.assertEqual({"session"}, {record["scope"] for record in failures})
        self.assertEqual({"1"}, {record["session_id"] for record in failures})
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
        self.assertEqual({"1"}, {str(row["session_id"]) for row in summaries})
        self.assertEqual({"session"}, {row["scope"] for row in summaries})
        self.assertEqual({"SVC"}, {row["failed_alias"] for row in summaries})
        # The same record reaches the state file the supervisor audit reads.
        persisted = json.loads(
            Path(str(payload["state_path"])).read_text(encoding="utf-8")
        )
        self.assertEqual(summaries, persisted["unresolved_commit_groups"])
        self.assertEqual(
            "SVC",
            commit_settlement._unresolved_commit_groups(persisted)[0]["failed_alias"],
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
            review_guard._run_git(
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
            self.assertLessEqual(
                attempts, commit_settlement.MAX_COMMIT_GROUP_RETRIES + 1
            )

    def test_unused_dirty_repository_does_not_block_worker_launch(self):
        unused = next(alias for alias in self.repositories if alias != "SVC")
        for session in plans_snapshot.inspect_plan(self.plan_dir).sessions:
            text = session.path.read_text()
            registry = plans_registry.read_repository_registry(self.plan_dir)
            scope = plans_session_scope.parse_session_scope(text, registry.aliases)
            text = text.replace(
                plans_session_scope.render_session_scope(scope).strip(),
                plans_session_scope.render_session_scope(
                    plans_session_scope.SessionScope(reads=("SVC",), writes=("SVC",))
                ).strip(),
            )
            session.path.write_text(text)
        path = self.repositories[unused] / "independent.txt"
        path.write_text("pre-existing independent work\n")
        payload = self._run("unused-dirt", fail_limit=0)
        self.assertEqual(0, self.exit_code, payload.get("message"))
        self.assertEqual([1, 2], payload["completed_sessions"])
        self.assertEqual("pre-existing independent work\n", path.read_text())

    def test_seventeen_session_budget_stops_before_session_twenty_one(self):
        write_multi_repo_plan(
            self.plan_dir, ("complete",) * 3 + ("pending",) * 18, specs=SPECS
        )
        payload = self._run("seventeen-session-budget", fail_limit=0, max_sessions=17)
        self.assertEqual(0, self.exit_code, payload.get("message"))
        self.assertEqual("max-sessions", payload["reason"])
        self.assertEqual(list(range(4, 21)), payload["completed_sessions"])
        self.assertEqual(
            "21", plans_snapshot.inspect_plan(self.plan_dir).next_pending.id
        )

    def test_later_scope_blocks_preexisting_dirty_repository(self):
        unused = next(alias for alias in self.repositories if alias != "SVC")
        registry = plans_registry.read_repository_registry(self.plan_dir)
        first = self.plan_dir / "session-1.md"
        text = first.read_text()
        scope = plans_session_scope.parse_session_scope(text, registry.aliases)
        first.write_text(
            text.replace(
                plans_session_scope.render_session_scope(scope).strip(),
                plans_session_scope.render_session_scope(
                    plans_session_scope.SessionScope(reads=("SVC",), writes=("SVC",))
                ).strip(),
            )
        )
        (self.repositories[unused] / "independent.txt").write_text("existing dirt\n")
        payload = self._run("later-needed-dirt", fail_limit=0)
        self.assertEqual(1, self.exit_code)
        self.assertEqual("commit-preflight", payload["reason"])
        self.assertEqual([1], payload["completed_sessions"])
        self.assertEqual(
            "2", plans_snapshot.inspect_plan(self.plan_dir).next_pending.id
        )

    def test_new_unused_repository_mutation_still_rejects_worker(self):
        unused = next(alias for alias in self.repositories if alias != "SVC")
        registry = plans_registry.read_repository_registry(self.plan_dir)
        first = self.plan_dir / "session-1.md"
        text = first.read_text()
        scope = plans_session_scope.parse_session_scope(text, registry.aliases)
        first.write_text(
            text.replace(
                plans_session_scope.render_session_scope(scope).strip(),
                plans_session_scope.render_session_scope(
                    plans_session_scope.SessionScope(reads=("SVC",), writes=("SVC",))
                ).strip(),
            )
        )
        original = self._harness

        def mutating(*args, **kwargs):
            result = original(*args, **kwargs)
            (self.repositories[unused] / "new-dirt.txt").write_text("new mutation\n")
            return result

        with mock.patch.object(self, "_harness", side_effect=mutating):
            payload = self._run("new-unused-mutation", fail_limit=0)
        self.assertEqual(1, self.exit_code)
        self.assertEqual("workspace-write-scope-violation", payload["reason"])
        self.assertEqual([], payload["completed_sessions"])
        self.assertEqual(
            "1", plans_snapshot.inspect_plan(self.plan_dir).next_pending.id
        )
        self.assertTrue((self.repositories[unused] / "new-dirt.txt").exists())


if __name__ == "__main__":
    unittest.main()
