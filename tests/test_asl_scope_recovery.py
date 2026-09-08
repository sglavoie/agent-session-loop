"""Public CLI regressions for explicit, evidence-bound remote-ref recovery."""

from __future__ import annotations

import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    cli,
    events,
    plans,
    repository_evidence,
    scope_recovery,
    state,
    turn_preflight,
    workspace_evidence,
    workspaces,
)
from tests.support import git_stdout, init_multi_repo_workspace, write_multi_repo_plan


class RefRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.workspace = self.root / "workspace"
        specs = (("scripts", "scripts", "git@github.com:example/scripts.git"),)
        self.repo = init_multi_repo_workspace(self.workspace, specs)["scripts"]
        self.plan = self.root / "plan"
        write_multi_repo_plan(self.plan, ("pending",), specs=specs)
        self.session = self.plan / "session-1.md"
        self.session.write_text(
            self.session.read_text().replace(
                "**Writes repositories**: scripts", "**Writes repositories**: (none)"
            )
        )
        registry = plans.read_repository_registry(self.plan)
        assert registry is not None
        self.resolved = workspaces.resolve_workspace(self.workspace, registry)
        plans.save_workspace_plan_metadata(self.plan, self.workspace, registry.digest)
        self.state_dir = self.root / "state"
        self.event_path = (
            state.plan_state_dir(self.plan, self.state_dir) / events.EVENTS_FILE_NAME
        )
        events.append_event(
            self.event_path,
            events.Event(
                name=events.RUN_STARTED_EVENT,
                payload={
                    "run_id": "rejected-run",
                    "workspace_root": str(self.resolved.root),
                    "registry_digest": registry.digest,
                },
            ),
        )
        self.old = git_stdout(self.repo, "rev-parse", "HEAD")
        git_stdout(self.repo, "commit", "--allow-empty", "-m", "second")
        self.new = git_stdout(self.repo, "rev-parse", "HEAD")
        git_stdout(self.repo, "update-ref", "refs/remotes/origin/main", self.old)
        before = self.capture()
        git_stdout(self.repo, "update-ref", "refs/remotes/origin/main", self.new)
        after = self.capture()
        self.guard = workspace_evidence._workspace_write_guard_evidence(
            workspace=self.resolved,
            scope=plans.SessionScope(reads=("scripts",), writes=()),
            comparison=repository_evidence.compare_workspace_snapshots(before, after),
            exclude=(),
            include_ignored=False,
        )
        self.guard.update(session_id="1", snapshot_before=before, snapshot_after=after)
        self.pause()

    def capture(self):
        return repository_evidence.capture_registered_snapshot(
            self.workspace, self.plan, ("scripts",)
        )

    def pause(self) -> None:
        events.append_event(
            self.event_path,
            events.Event(
                name=events.RUN_PAUSED_EVENT,
                payload={
                    "run_id": "rejected-run",
                    "reason": events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
                    "details": {"session_id": "1", "workspace_write_guard": self.guard},
                },
            ),
        )

    def invoke(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cli.main(
                [
                    "accept-ref-updates",
                    str(self.plan),
                    "--session",
                    "1",
                    "--state-dir",
                    str(self.state_dir),
                    "--json",
                    *args,
                ]
            )
        return code, json.loads(output.getvalue())

    def retained(self):
        return turn_preflight.resolve_retained_scope_repair(
            event_path=self.event_path,
            session_id="1",
            workspace=self.resolved,
        )

    def test_accepts_exact_preview_and_preserves_pending_read_only_review(self):
        session_before = self.session.read_bytes()
        events_before = self.event_path.read_bytes()
        code, preview = self.invoke()
        self.assertEqual(0, code, preview)
        self.assertFalse(preview["accepted"])
        self.assertEqual(events_before, self.event_path.read_bytes())
        self.assertIsNotNone(self.retained())
        code, accepted = self.invoke(
            "--accept", preview["evidence_digest"], "--reason", "Operator approves push"
        )
        self.assertEqual(0, code, accepted)
        self.assertTrue(accepted["accepted"])
        self.assertIsNone(self.retained())
        self.assertEqual(session_before, self.session.read_bytes())
        self.assertEqual("1", plans.inspect_plan(self.plan).next_pending.id)
        self.assertEqual(
            self.new, git_stdout(self.repo, "rev-parse", "refs/remotes/origin/main")
        )
        self.assertEqual("", git_stdout(self.repo, "status", "--porcelain"))
        # A later violation is never covered by the previous acceptance.
        self.pause()
        self.assertIsNotNone(self.retained())

    def test_missing_reason_and_wrong_digest_never_clear_failure(self):
        _, preview = self.invoke()
        for args in (
            ("--accept", preview["evidence_digest"]),
            ("--accept", "stale", "--reason", "approved"),
        ):
            with self.subTest(args=args):
                code, result = self.invoke(*args)
                self.assertEqual(1, code, result)
                self.assertNotIn("evidence_digest", result)
                self.assertIsNotNone(self.retained())

    def test_changed_plan_or_current_commit_invalidates_preview(self):
        for change in (
            lambda: self.session.write_text(
                self.session.read_text() + "\nOperator note\n"
            ),
            lambda: git_stdout(
                self.repo, "commit", "--allow-empty", "-m", "operator repair"
            ),
        ):
            _, preview = self.invoke()
            change()
            code, result = self.invoke(
                "--accept", preview["evidence_digest"], "--reason", "approved"
            )
            self.assertEqual(1, code, result)
            self.assertIsNotNone(self.retained())
        # A newly reviewed clean operator commit is allowed; only the original
        # rejected turn must be ref-only. It is visible in current_heads.
        _, preview = self.invoke()
        self.assertEqual(
            git_stdout(self.repo, "rev-parse", "HEAD"),
            preview["current_heads"]["scripts"],
        )

    def test_rejects_further_ref_movement_and_current_dirty_tree(self):
        _, preview = self.invoke()
        git_stdout(self.repo, "update-ref", "refs/remotes/origin/main", self.old)
        code, result = self.invoke(
            "--accept", preview["evidence_digest"], "--reason", "approved"
        )
        self.assertEqual(1, code, result)
        git_stdout(self.repo, "update-ref", "refs/remotes/origin/main", self.new)
        (self.repo / "untracked.txt").write_text("operator work\n")
        self.assertEqual(1, self.invoke()[0])
        self.assertIsNotNone(self.retained())

    def test_rejects_original_source_head_local_ref_and_unknown_field_changes(self):
        original = copy.deepcopy(self.guard)
        for field, value in (
            ("tracked_changed", ["README.md"]),
            ("head", self.old),
            ("future_evidence", "unexpected"),
        ):
            with self.subTest(field=field):
                self.guard = copy.deepcopy(original)
                self.guard["snapshot_after"]["scripts"][field] = value
                self.pause()
                self.assertEqual(1, self.invoke()[0])
        self.guard = copy.deepcopy(original)
        self.guard["snapshot_before"]["scripts"]["refs"].append(
            ["refs/heads/other", self.old]
        )
        self.guard["snapshot_after"]["scripts"]["refs"].append(
            ["refs/heads/other", self.new]
        )
        self.pause()
        self.assertEqual(1, self.invoke()[0])

    def test_rejects_missing_topology_registry_and_unavailable_evidence(self):
        original = copy.deepcopy(self.guard)
        for mutate in (
            lambda g: g["snapshot_after"].clear(),
            lambda g: g["registered_repositories"].clear(),
            lambda g: g.update(unavailable={"scripts": {"reason": "unavailable"}}),
        ):
            self.guard = copy.deepcopy(original)
            mutate(self.guard)
            self.pause()
            self.assertEqual(1, self.invoke()[0])

    def test_active_runner_lock_is_preserved(self):
        state.acquire_lock(self.plan, self.state_dir)
        try:
            code, result = self.invoke()
            self.assertEqual(1, code, result)
            self.assertTrue(state.inspect_lock_info(self.plan, self.state_dir).active)
            self.assertIsNotNone(self.retained())
        finally:
            state.release_lock(self.plan, self.state_dir)

    def test_rebinding_origin_cannot_reuse_original_failure(self):
        manifest = self.plan / "manifest.md"
        manifest.write_text(
            manifest.read_text().replace("example/scripts.git", "example/other.git")
        )
        git_stdout(
            self.repo, "remote", "set-url", "origin", "git@github.com:example/other.git"
        )
        registry = plans.read_repository_registry(self.plan)
        assert registry is not None
        plans.save_workspace_plan_metadata(self.plan, self.workspace, registry.digest)
        code, result = self.invoke()
        self.assertEqual(1, code, result)
        self.assertIn("original run workspace binding", result["error"])
        self.assertIsNotNone(self.retained())

    def test_acceptance_for_other_evidence_does_not_clear_failure(self):
        for acceptance in (
            {},
            {"session_id": "2", "retained_guard_sha256": "other"},
            {"session_id": "1", "retained_guard_sha256": "other"},
        ):
            events.append_event(
                self.event_path,
                events.Event(
                    name=events.WORKSPACE_REF_UPDATES_ACCEPTED_EVENT,
                    payload={
                        "reason": "operator approval",
                        "details": {
                            "session_id": "1",
                            "acceptance": acceptance,
                        },
                    },
                ),
            )
            self.assertIsNotNone(self.retained())

    def test_rechecks_after_preparation(self):
        _, preview = self.invoke()
        real = scope_recovery._proposal
        calls = 0

        def moving_proposal(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                git_stdout(
                    self.repo, "update-ref", "refs/remotes/origin/main", self.old
                )
            return real(*args, **kwargs)

        with mock.patch.object(
            scope_recovery, "_proposal", side_effect=moving_proposal
        ):
            code, result = self.invoke(
                "--accept", preview["evidence_digest"], "--reason", "approved"
            )
        self.assertEqual(1, code, result)
        self.assertIsNotNone(self.retained())
