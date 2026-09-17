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


class ExternalRecoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.workspace = self.root / "workspace"
        specs = (
            ("notes", "notes", "git@github.com:example/notes.git"),
            ("pi", "pi", "git@github.com:example/pi.git"),
        )
        self.repos = init_multi_repo_workspace(self.workspace, specs)
        self.plan = self.root / "plan"
        write_multi_repo_plan(self.plan, ("pending", "pending"), specs=specs)
        self.session = self.plan / "session-1.md"
        self.session.write_text(
            self.session.read_text()
            .replace(
                "**Reads repositories**: notes, pi", "**Reads repositories**: notes"
            )
            .replace(
                "**Writes repositories**: notes, pi", "**Writes repositories**: notes"
            )
        )
        registry = plans.read_repository_registry(self.plan)
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
                    "run_id": "rejected",
                    "workspace_root": str(self.resolved.root),
                    "registry_digest": registry.digest,
                    "commit_mode": "per-session",
                    "repositories": [
                        dict(
                            alias=r.alias,
                            path=r.repository.path,
                            origin=r.repository.origin,
                            resolved_root=str(r.root),
                        )
                        for r in self.resolved.repositories
                    ],
                },
            ),
        )
        self.before = self.capture()
        (self.repos["notes"] / "README.md").write_text("retained work\n")
        git_stdout(self.repos["pi"], "commit", "--allow-empty", "-m", "independent")
        self.linked = self.root / "publication"
        git_stdout(
            self.repos["notes"],
            "worktree",
            "add",
            "-b",
            "publication",
            str(self.linked),
        )
        self.after = self.capture()
        self.guard = workspace_evidence._workspace_write_guard_evidence(
            workspace=self.resolved,
            scope=plans.SessionScope(reads=("notes",), writes=("notes",)),
            comparison=repository_evidence.compare_workspace_snapshots(
                self.before, self.after
            ),
            exclude=(),
            include_ignored=False,
        )
        self.guard.update(
            session_id="1", snapshot_before=self.before, snapshot_after=self.after
        )
        self.pause()
        (self.repos["pi"] / "independent.txt").write_text("independent dirt\n")

    def capture(self):
        return repository_evidence.capture_registered_snapshot(
            self.workspace, self.plan, ("notes", "pi")
        )

    def pause(self):
        events.append_event(
            self.event_path,
            events.Event(
                name=events.RUN_PAUSED_EVENT,
                payload={
                    "run_id": "rejected",
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
                    "acknowledge-external-changes",
                    str(self.plan),
                    "--session",
                    "1",
                    "--repository",
                    "pi",
                    "--state-dir",
                    str(self.state_dir),
                    "--json",
                    *args,
                ]
            )
        return code, json.loads(output.getvalue())

    def accept_args(self, preview):
        return (
            "--accept",
            preview["evidence_digest"],
            "--reason",
            "Independent Pi work authorized",
            "--checkpoint-retained",
        )

    def test_preview_and_accept_preserve_pi_and_original_turn(self):
        before = self.capture()
        session = self.session.read_bytes()
        history = self.event_path.read_bytes()
        code, preview = self.invoke()
        self.assertEqual(0, code, preview)
        self.assertEqual([], preview["remaining_blockers"])
        self.assertEqual(before, self.capture())
        self.assertEqual(history, self.event_path.read_bytes())
        code, result = self.invoke(*self.accept_args(preview))
        self.assertEqual(0, code, result)
        self.assertEqual(session, self.session.read_bytes())
        self.assertEqual(before["pi"], self.capture()["pi"])
        self.assertEqual("", git_stdout(self.repos["notes"], "status", "--porcelain"))
        self.assertTrue(self.linked.exists())
        self.assertEqual("1", plans.inspect_plan(self.plan).next_pending.id)
        self.assertIsNone(
            workspace_evidence._latest_workspace_scope_repair_requirement(
                self.event_path, "1"
            )
        )
        self.assertEqual(
            self.guard,
            json.loads(history.decode().splitlines()[-1])["payload"]["details"][
                "workspace_write_guard"
            ],
        )
        self.pause()
        self.assertIsNotNone(
            workspace_evidence._latest_workspace_scope_repair_requirement(
                self.event_path, "1"
            )
        )

    def test_stale_and_drift_refused(self):
        _, preview = self.invoke()
        (self.repos["pi"] / "independent.txt").write_text("new evidence\n")
        self.assertEqual(1, self.invoke(*self.accept_args(preview))[0])
        _, preview = self.invoke()
        (self.repos["notes"] / "README.md").write_text("content drift\n")
        _, fresh = self.invoke()
        self.assertTrue(fresh["remaining_blockers"])
        self.assertEqual(1, self.invoke(*self.accept_args(fresh))[0])

    def test_unrelated_staging_refused(self):
        (self.repos["notes"] / "foreign.txt").write_text("foreign\n")
        git_stdout(self.repos["notes"], "add", "foreign.txt")
        _, preview = self.invoke()
        self.assertTrue(preview["remaining_blockers"])
        self.assertEqual(1, self.invoke(*self.accept_args(preview))[0])
        self.assertIn(
            "A  foreign.txt", git_stdout(self.repos["notes"], "status", "--porcelain")
        )

    def test_reason_checkpoint_authorization_and_active_lock_required(self):
        _, preview = self.invoke()
        self.assertEqual(1, self.invoke("--accept", preview["evidence_digest"])[0])
        self.assertEqual(
            1,
            self.invoke("--accept", preview["evidence_digest"], "--reason", "approved")[
                0
            ],
        )
        state.acquire_lock(self.plan, self.state_dir)
        try:
            self.assertEqual(1, self.invoke(*self.accept_args(preview))[0])
        finally:
            state.release_lock(self.plan, self.state_dir)

    def test_writable_alias_and_changed_scope_binding_fail_closed(self):
        self.assertEqual(1, self.invoke("--repository", "notes")[0])
        self.session.write_text(
            self.session.read_text().replace(
                "**Reads repositories**: notes", "**Reads repositories**: notes, pi"
            )
        )
        self.assertEqual(1, self.invoke()[0])

    def test_needed_union_cleanliness_and_legacy_end_behavior(self):
        from agent_session_loop import preflight, review_guard, verification

        git_stdout(self.repos["notes"], "add", "README.md")
        git_stdout(self.repos["notes"], "commit", "-m", "manual checkpoint")
        context = verification.VerificationContext.resolved_workspace(self.resolved)
        snapshot = plans.inspect_plan(self.plan)
        self.assertEqual(
            (), preflight._check_registered_env_git(context, "per-session", snapshot)
        )
        needed = plans.needed_repository_aliases(snapshot, self.resolved.registry)
        review_guard._validate_commit_preflight(
            self.workspace, verification_context=context, needed_aliases=needed
        )
        linked_dirt = self.linked / "unowned.txt"
        linked_dirt.write_text("linked dirt\n")
        self.assertTrue(
            preflight._check_registered_env_git(context, "per-session", snapshot)
        )
        with self.assertRaises(review_guard.RunError):
            review_guard._validate_commit_preflight(
                self.workspace,
                verification_context=context,
                needed_aliases=needed,
            )
        linked_dirt.unlink()
        self.assertTrue(preflight._check_registered_env_git(context, "end", snapshot))
        self.session.write_text(
            self.session.read_text().replace(
                "**Reads repositories**: notes", "**Reads repositories**: notes, pi"
            )
        )
        snapshot = plans.inspect_plan(self.plan)
        self.assertTrue(
            preflight._check_registered_env_git(context, "per-session", snapshot)
        )
        with self.assertRaises(review_guard.RunError):
            review_guard._validate_commit_preflight(
                self.workspace,
                verification_context=context,
                needed_aliases=plans.needed_repository_aliases(
                    snapshot, self.resolved.registry
                ),
            )

    def test_interruption_after_commit_reconciles_without_duplicate(self):
        from agent_session_loop import run_commit

        _, preview = self.invoke()
        original = run_commit._run_asl_commit

        def interrupted(**kwargs):
            result = original(**kwargs)
            self.assertTrue(result["success"], result)
            raise OSError("simulated interruption")

        with mock.patch.object(run_commit, "_run_asl_commit", side_effect=interrupted):
            code, result = self.invoke(*self.accept_args(preview))
        self.assertEqual(1, code, result)
        head = git_stdout(self.repos["notes"], "rev-parse", "HEAD")
        code, result = self.invoke(*self.accept_args(preview))
        self.assertEqual(0, code, result)
        self.assertEqual(head, git_stdout(self.repos["notes"], "rev-parse", "HEAD"))
        self.assertEqual(head, result["checkpoints"]["notes"])

    def test_partial_multiple_checkout_failure_preserves_successful_sha(self):
        from agent_session_loop import run_commit

        (self.linked / "README.md").write_text("retained linked work\n")
        self.after = self.capture()
        self.guard = workspace_evidence._workspace_write_guard_evidence(
            workspace=self.resolved,
            scope=plans.SessionScope(reads=("notes",), writes=("notes",)),
            comparison=repository_evidence.compare_workspace_snapshots(
                self.before, self.after
            ),
            exclude=(),
            include_ignored=False,
        )
        self.guard.update(
            session_id="1", snapshot_before=self.before, snapshot_after=self.after
        )
        self.pause()
        _, preview = self.invoke()
        self.assertEqual(2, len(preview["checkpoint_targets"]))
        original = run_commit._run_asl_commit

        def fail_second(**kwargs):
            if kwargs["commit_index"] == 2:
                git_stdout(Path(kwargs["cwd"]), "add", "README.md")
                raise OSError("simulated checkpoint failure after staging")
            return original(**kwargs)

        with mock.patch.object(run_commit, "_run_asl_commit", side_effect=fail_second):
            code, result = self.invoke(*self.accept_args(preview))
        self.assertEqual(1, code, result)
        sha = git_stdout(self.repos["notes"], "rev-parse", "HEAD")
        self.assertEqual(sha, result["checkpoints"]["notes"])
        code, result = self.invoke(*self.accept_args(preview))
        self.assertEqual(0, code, result)
        self.assertEqual(sha, git_stdout(self.repos["notes"], "rev-parse", "HEAD"))
        self.assertEqual("", git_stdout(self.linked, "status", "--porcelain"))

    def test_interrupted_final_progress_write_uses_completed_acknowledgement(self):
        from agent_session_loop import durability

        _, preview = self.invoke()
        original = durability.write_json_file

        def fail_final(path, payload, **kwargs):
            if path.name == scope_recovery.RECOVERY_FILE_NAME and payload.get(
                "complete"
            ):
                raise OSError("simulated interruption after acknowledgement")
            return original(path, payload, **kwargs)

        with mock.patch.object(durability, "write_json_file", side_effect=fail_final):
            code, result = self.invoke(*self.accept_args(preview))
        self.assertEqual(1, code, result)
        sha = git_stdout(self.repos["notes"], "rev-parse", "HEAD")
        code, result = self.invoke(*self.accept_args(preview))
        self.assertEqual(0, code, result)
        self.assertEqual(sha, git_stdout(self.repos["notes"], "rev-parse", "HEAD"))

    def test_unavailable_and_changed_origin_evidence_are_refused(self):
        self.guard["snapshot_before"]["pi"]["git"] = False
        self.pause()
        self.assertEqual(1, self.invoke()[0])
        git_stdout(
            self.repos["pi"],
            "remote",
            "set-url",
            "origin",
            "git@github.com:example/other.git",
        )
        self.assertEqual(1, self.invoke()[0])
