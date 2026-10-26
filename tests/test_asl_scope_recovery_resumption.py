# ruff: noqa: F403, F405
"""External recovery durability and interruption tests."""

from __future__ import annotations

# ruff: noqa: F403, F405
from tests.asl_scope_recovery_test_support import *


class ExternalRecoveryResumptionTests(ExternalRecoveryTestCase):
    def test_written_tree_cleanliness_and_legacy_end_behavior(self):
        from agent_session_loop import (
            preflight_environment,
            review_guard,
            verification_models,
        )

        git_stdout(self.repos["notes"], "add", "README.md")
        git_stdout(self.repos["notes"], "commit", "-m", "manual checkpoint")
        context = verification_models.VerificationContext.resolved_workspace(
            self.resolved
        )
        snapshot = plans_snapshot.inspect_plan(self.plan)
        self.assertEqual(
            (),
            preflight_environment._check_registered_env_git(
                context, "per-session", snapshot
            ),
        )
        needed = plans_session_scope.needed_repository_aliases(
            snapshot, self.resolved.registry
        )
        review_guard._validate_commit_preflight(
            self.workspace, verification_context=context, needed_aliases=needed
        )
        linked_dirt = self.linked / "unowned.txt"
        linked_dirt.write_text("linked dirt\n")
        self.assertTrue(
            preflight_environment._check_registered_env_git(
                context, "per-session", snapshot
            )
        )
        with self.assertRaises(review_guard.RunError):
            review_guard._validate_commit_preflight(
                self.workspace,
                verification_context=context,
                needed_aliases=needed,
            )
        linked_dirt.unlink()
        self.assertTrue(
            preflight_environment._check_registered_env_git(context, "end", snapshot)
        )
        self.session.write_text(
            self.session.read_text().replace(
                "**Reads repositories**: notes", "**Reads repositories**: notes, pi"
            )
        )
        snapshot = plans_snapshot.inspect_plan(self.plan)
        # A dirty tree the session only reads is inspected, reported green, and
        # never refused: its commit stages write aliases only.
        findings = preflight_environment._check_registered_env_git(
            context, "per-session", snapshot
        )
        self.assertEqual(["green"], [finding.severity for finding in findings])
        self.assertIn("read-only", findings[0].message)
        result = review_guard._validate_commit_preflight(
            self.workspace,
            verification_context=context,
            needed_aliases=plans_session_scope.needed_repository_aliases(
                snapshot, self.resolved.registry
            ),
            clean_aliases=plans_session_scope.commit_repository_aliases(
                snapshot, self.resolved.registry
            ),
        )
        by_alias = {row["alias"]: row for row in result["repositories"]}
        self.assertEqual(
            {"clean": False, "clean_required": False},
            {key: by_alias["pi"][key] for key in ("clean", "clean_required")},
        )
        self.session.write_text(
            self.session.read_text().replace(
                "**Writes repositories**: notes", "**Writes repositories**: notes, pi"
            )
        )
        snapshot = plans_snapshot.inspect_plan(self.plan)
        self.assertEqual(
            ["yellow"],
            [
                finding.severity
                for finding in preflight_environment._check_registered_env_git(
                    context, "per-session", snapshot
                )
            ],
        )
        with self.assertRaises(review_guard.RunError):
            review_guard._validate_commit_preflight(
                self.workspace,
                verification_context=context,
                needed_aliases=plans_session_scope.needed_repository_aliases(
                    snapshot, self.resolved.registry
                ),
                clean_aliases=plans_session_scope.commit_repository_aliases(
                    snapshot, self.resolved.registry
                ),
            )

    def test_dirty_read_only_tree_convicts_only_turn_writes(self):
        # The write guard judges the turn's own delta, so dirt a read-only tree
        # already carried at turn start is what the commit gate may now allow.
        scope = plans_session_scope.SessionScope(
            reads=("notes", "pi"), writes=("notes",)
        )

        def verdict(before, after):
            return workspace_evidence._workspace_write_guard_evidence(
                workspace=self.resolved,
                scope=scope,
                comparison=repository_evidence.compare_workspace_snapshots(
                    before, after
                ),
                exclude=(),
                include_ignored=False,
            )

        before = self.capture()
        (self.repos["notes"] / "README.md").write_text("session work\n")
        self.assertTrue(verdict(before, self.capture())["passed"])
        (self.repos["pi"] / "independent.txt").write_text("turn wrote here\n")
        self.assertFalse(verdict(before, self.capture())["passed"])

    def test_external_change_alias_is_recorded_not_convicted(self):
        from agent_session_loop import review_guard, verification_models

        before = self.capture()
        (self.repos["notes"] / "README.md").write_text("session work\n")
        (self.repos["pi"] / "independent.txt").write_text("operator edit\n")
        comparison = repository_evidence.compare_workspace_snapshots(
            before, self.capture()
        )

        def verdict(scope, aliases):
            return workspace_evidence._workspace_write_guard_evidence(
                workspace=self.resolved,
                scope=scope,
                comparison=comparison,
                exclude=(),
                include_ignored=False,
                external_change_aliases=aliases,
            )

        read_pi = plans_session_scope.SessionScope(
            reads=("notes", "pi"), writes=("notes",)
        )
        strict = verdict(read_pi, frozenset())
        self.assertFalse(strict["passed"])
        allowed = verdict(read_pi, frozenset({"pi"}))
        self.assertTrue(allowed["passed"], allowed["violations"])
        self.assertEqual(["notes"], allowed["authorized_aliases"])
        self.assertEqual({"pi": ["pi:independent.txt"]}, allowed["external_changes"])
        # A session that writes the alias owns its changes as before.
        write_pi = plans_session_scope.SessionScope(
            reads=("notes", "pi"), writes=("notes", "pi")
        )
        owned = verdict(write_pi, frozenset({"pi"}))
        self.assertTrue(owned["passed"])
        self.assertNotIn("external_changes", owned)
        self.assertEqual(["notes", "pi"], owned["authorized_aliases"])

        context = verification_models.VerificationContext.resolved_workspace(
            self.resolved
        )
        args = mock.Mock(allow_external_changes=["pi", "pi"])
        self.assertEqual(
            frozenset({"pi"}), review_guard._external_change_aliases(args, context)
        )
        args.allow_external_changes = ["nope"]
        with self.assertRaisesRegex(review_guard.RunError, "nope"):
            review_guard._external_change_aliases(args, context)
        with self.assertRaisesRegex(review_guard.RunError, "registered"):
            review_guard._external_change_aliases(
                mock.Mock(allow_external_changes=["pi"]),
                verification_models.VerificationContext.legacy(self.workspace),
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
            scope=plans_session_scope.SessionScope(reads=("notes",), writes=("notes",)),
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
        self.assertEqual("recovery-interrupted", result["reason"])
        self.assertIn("--accept", result["resume_argv"])
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
