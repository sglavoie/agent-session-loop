"""The restricted read-only audit launch and its workspace guards."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    assurance_ledger,
    assurance_models_artifacts,
    harness_claude,
    harness_failures,
    harnesses,
    supervisor_audit,
    supervisor_audit_vocabulary,
)
from agent_session_loop.assurance_models_summary import AuditRole
from tests.support import git_stdout, init_git_repo
from tests.support_supervisor_audit import RUN_ID

#: Every flag that widens a harness beyond its default sandbox. None of them may
#: appear in an audit invocation, whatever else the argv contains.
BYPASS_FLAGS = (
    "--dangerously-bypass-approvals-and-sandbox",
    "--allow-all-tools",
    "--allow-all-paths",
    "bypassPermissions",
    "workspace-write",
    "dontAsk",
)


class _RecordingHarness:
    """Stand-in for ``harnesses.run_harness`` that can mutate the workspace.

    A real read-only sandbox cannot be exercised offline, so the guard is tested
    the other way around: this fake plays a harness whose sandbox failed open,
    and the audit must catch it from evidence alone.
    """

    def __init__(
        self,
        *,
        writes: Path | None = None,
        action: Callable[[], None] | None = None,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        self.writes = writes
        self.action = action
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.calls: list[dict[str, Any]] = []

    def __call__(self, name: str, **kwargs: object) -> harnesses.HarnessResult:
        self.calls.append({"name": name, **kwargs})
        access = kwargs["access"]
        assert isinstance(access, harnesses.AccessProfile)
        argv = harnesses.get_adapter(name).build_argv(
            cwd=Path(str(kwargs["cwd"])),
            plan_dir=None,
            prompt=str(kwargs["prompt"]),
            last_message_path=Path(str(kwargs["last_message_path"])),
            access=access,
        )
        if self.writes is not None:
            self.writes.write_text("escaped\n", encoding="utf-8")
        if self.action is not None:
            self.action()
        return harnesses.HarnessResult(
            argv=argv,
            returncode=self.returncode,
            stdout=self.stdout,
            stderr=self.stderr,
            last_message="audit done",
            name=name,
        )


class RestrictedAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        # Every harness here is a recording fake, so whether its real CLI is on
        # this machine's PATH must not decide the verdict: without this, a host
        # missing one CLI reports that harness `harness-not-installed`.
        patcher = mock.patch.object(
            harnesses.HarnessAdapter,
            "resolve_executable",
            lambda adapter: f"/fake/bin/{adapter.executable}",
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _role(self, harness: str) -> AuditRole:
        return AuditRole(
            harness=harness,
            implementation_harness="codex" if harness != "codex" else "claude",
            source="explicit",
            timeout_seconds=30.0,
        )

    def _workspace(self, tmp: Path) -> tuple[Path, Path, Path, Path]:
        workspace = tmp / "workspace"
        init_git_repo(workspace / "app")
        plan_dir = tmp / "plan"
        plan_dir.mkdir()
        state_dir = tmp / "state"
        run_dir = state_dir / "runs" / RUN_ID
        run_dir.mkdir(parents=True)
        return workspace, plan_dir, state_dir, run_dir

    def _run(
        self,
        harness: str,
        tmp: Path,
        fake: _RecordingHarness,
        workspace: Path,
        plan_dir: Path,
        state_dir: Path,
        run_dir: Path,
        repositories: tuple[str, ...] = ("app",),
        registry_declared: bool = True,
    ) -> supervisor_audit.AuditAttemptResult:
        return supervisor_audit.run_restricted_audit(
            self._role(harness),
            run_dir=run_dir,
            run_id=RUN_ID,
            workspace_root=workspace,
            plan_dir=plan_dir,
            prompt="Audit this run.",
            repositories=repositories,
            registry_declared=registry_declared,
            state_dir=state_dir,
            _run_harness_fn=fake,
        )

    def test_audit_argv_never_carries_bypass_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            run_dir.mkdir()

            for harness in harnesses.HARNESS_NAMES:
                invocation = supervisor_audit.build_audit_invocation(
                    self._role(harness),
                    run_dir=run_dir,
                    prompt="Audit this run.",
                    last_message_path=run_dir / "last.txt",
                )

                for flag in BYPASS_FLAGS:
                    self.assertNotIn(flag, invocation.argv, harness)

    def test_the_audit_runs_from_a_directory_outside_the_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, _, _, run_dir = self._workspace(Path(tmp))

            workdir = supervisor_audit.audit_workdir(run_dir)
            roots = supervisor_audit.audit_read_roots(run_dir)

            self.assertTrue(workdir.is_dir())
            self.assertEqual(run_dir / supervisor_audit.AUDIT_WORKDIR_NAME, workdir)
            self.assertNotIn(workspace.resolve(), [root.resolve() for root in roots])
            for root in roots:
                self.assertFalse(root.resolve().is_relative_to(workspace.resolve()))

    def test_an_audit_that_mutates_the_workspace_is_unavailable(self) -> None:
        for harness in harnesses.HARNESS_NAMES:
            with self.subTest(harness=harness), tempfile.TemporaryDirectory() as tmp:
                workspace, plan_dir, state_dir, run_dir = self._workspace(Path(tmp))
                fake = _RecordingHarness(writes=workspace / "app" / "escaped.txt")

                result = self._run(
                    harness, Path(tmp), fake, workspace, plan_dir, state_dir, run_dir
                )

                self.assertEqual("workspace-changed", result.outcome)
                self.assertEqual("unavailable", result.state)
                self.assertEqual(
                    supervisor_audit.REASON_WORKSPACE_CHANGED, result.reason
                )
                self.assertIn("app", (result.workspace_change or {})["changed"])

    def test_a_failed_launch_carries_its_classification_and_redacted_streams(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._workspace(Path(tmp))
            fake = _RecordingHarness(
                returncode=1,
                stdout="chatter\n" * 4000,
                stderr=(
                    "cloning https://user:hunter2@example.com/app\n"
                    '{"error": {"message": "Selected model is at capacity.", '
                    '"codex_error_info": "server_overloaded"}}'
                ),
            )

            result = self._run(
                "claude", Path(tmp), fake, workspace, plan_dir, state_dir, run_dir
            )

            self.assertEqual("transport-error", result.outcome)
            assert result.classification is not None
            self.assertEqual(
                harness_failures.MODEL_CAPACITY_MARKER, result.classification.marker
            )
            self.assertTrue(result.classification.retryable)
            self.assertIn("at capacity", result.classification.evidence)
            # The provider's own words survive, the credential does not.
            self.assertIn("server_overloaded", result.stderr_tail)
            self.assertNotIn("hunter2", result.stderr_tail)
            self.assertLessEqual(
                len(result.stdout_tail.encode("utf-8")),
                supervisor_audit.MAX_HARNESS_TAIL_BYTES,
            )
            # The tail is the end of the stream, where the error is.
            self.assertTrue(result.stderr_tail.endswith('"server_overloaded"}}'))

    def test_a_write_to_an_unregistered_sibling_leaves_the_audit_available(
        self,
    ) -> None:
        """A concurrent run's worktree is outside the boundary this checks."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._workspace(Path(tmp))
            init_git_repo(workspace / "sibling")
            fake = _RecordingHarness(writes=workspace / "sibling" / "escaped.txt")

            result = self._run(
                "claude", Path(tmp), fake, workspace, plan_dir, state_dir, run_dir
            )

            self.assertEqual("ok", result.outcome)
            self.assertEqual("pending", result.state)
            self.assertIsNone(result.workspace_change)
            self.assertFalse(
                (
                    run_dir / supervisor_audit_vocabulary.WORKSPACE_CHANGE_ARTIFACT_NAME
                ).exists()
            )

    def test_a_symlinked_registered_checkout_is_not_audited_as_its_target(
        self,
    ) -> None:
        """An identical clone reached through a link is a topology change."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._workspace(Path(tmp))
            checkout = workspace / "app"
            twin = Path(tmp) / "twin"
            subprocess.run(
                ["git", "clone", str(checkout), str(twin)],
                text=True,
                capture_output=True,
                check=True,
            )

            def swap() -> None:
                shutil.rmtree(checkout)
                checkout.symlink_to(twin)

            fake = _RecordingHarness(action=swap)

            result = self._run(
                "claude", Path(tmp), fake, workspace, plan_dir, state_dir, run_dir
            )

            self.assertEqual("workspace-changed", result.outcome)
            self.assertEqual("unavailable", result.state)
            self.assertEqual(supervisor_audit.REASON_WORKSPACE_CHANGED, result.reason)
            self.assertEqual(["app"], (result.workspace_change or {})["removed"])
            self.assertEqual({}, (result.workspace_change or {})["changed"])

    def test_a_checkout_symlinked_before_the_audit_cannot_be_audited(self) -> None:
        """Both snapshots omit it, so the empty comparison must not read as ok."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._workspace(Path(tmp))
            checkout = workspace / "app"
            twin = Path(tmp) / "twin"
            subprocess.run(
                ["git", "clone", str(checkout), str(twin)],
                text=True,
                capture_output=True,
                check=True,
            )
            shutil.rmtree(checkout)
            checkout.symlink_to(twin)
            fake = _RecordingHarness()

            result = self._run(
                "claude", Path(tmp), fake, workspace, plan_dir, state_dir, run_dir
            )

            self.assertEqual("unavailable", result.outcome)
            self.assertEqual("unavailable", result.state)
            self.assertEqual(
                supervisor_audit.REASON_WORKSPACE_EVIDENCE_UNAVAILABLE, result.reason
            )
            self.assertIsNone(result.workspace_change)
            self.assertEqual([], fake.calls)

    def test_a_registered_repository_that_stops_being_a_worktree_is_unavailable(
        self,
    ) -> None:
        """Fail-closed probe evidence after the turn is silence, not an all-clear."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._workspace(Path(tmp))
            checkout = workspace / "app"
            fake = _RecordingHarness(action=lambda: shutil.rmtree(checkout / ".git"))

            result = self._run(
                "claude", Path(tmp), fake, workspace, plan_dir, state_dir, run_dir
            )

            self.assertEqual("unavailable", result.outcome)
            self.assertEqual("unavailable", result.state)
            self.assertEqual(
                supervisor_audit.REASON_WORKSPACE_EVIDENCE_UNAVAILABLE, result.reason
            )
            self.assertIsNone(result.workspace_change)
            self.assertEqual(1, len(fake.calls))

    def test_a_registered_repository_unfingerprintable_throughout_is_refused(
        self,
    ) -> None:
        """Unavailable on both sides compares as unchanged, so it cannot be graded."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._workspace(Path(tmp))
            checkout = workspace / "app"
            shutil.rmtree(checkout / ".git")
            escaped = checkout / "escaped.txt"
            fake = _RecordingHarness(writes=escaped)

            result = self._run(
                "claude", Path(tmp), fake, workspace, plan_dir, state_dir, run_dir
            )

            self.assertEqual("unavailable", result.outcome)
            self.assertEqual("unavailable", result.state)
            self.assertEqual(
                supervisor_audit.REASON_WORKSPACE_EVIDENCE_UNAVAILABLE, result.reason
            )
            self.assertIsNone(result.workspace_change)
            self.assertEqual([], fake.calls)
            self.assertFalse(escaped.exists())

    def test_a_synthesized_run_root_that_is_not_a_worktree_is_still_audited(
        self,
    ) -> None:
        """A plan that registered nothing keeps the audit its run root allows."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._workspace(Path(tmp))
            fake = _RecordingHarness()

            result = self._run(
                "claude",
                Path(tmp),
                fake,
                workspace,
                plan_dir,
                state_dir,
                run_dir,
                repositories=(".",),
                registry_declared=False,
            )

            self.assertEqual("ok", result.outcome)
            self.assertEqual("pending", result.state)
            self.assertEqual(1, len(fake.calls))

    def test_the_same_root_declared_by_a_registry_is_refused(self) -> None:
        """The legacy tolerance follows the caller's claim, not the path."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._workspace(Path(tmp))
            fake = _RecordingHarness()

            result = self._run(
                "claude",
                Path(tmp),
                fake,
                workspace,
                plan_dir,
                state_dir,
                run_dir,
                repositories=(".",),
            )

            self.assertEqual("unavailable", result.outcome)
            self.assertEqual(
                supervisor_audit.REASON_WORKSPACE_EVIDENCE_UNAVAILABLE, result.reason
            )
            self.assertEqual([], fake.calls)

    def test_workspace_activity_is_recorded_without_claiming_a_writer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._workspace(Path(tmp))
            fake = _RecordingHarness(writes=workspace / "app" / "escaped.txt")

            result = self._run(
                "claude", Path(tmp), fake, workspace, plan_dir, state_dir, run_dir
            )

            artifact = (
                run_dir / supervisor_audit_vocabulary.WORKSPACE_CHANGE_ARTIFACT_NAME
            )
            recorded = json.loads(artifact.read_text(encoding="utf-8"))
            self.assertEqual("unattributed", recorded["attribution"]["status"])
            self.assertEqual(
                "snapshot-comparison-has-no-writer-identity",
                recorded["attribution"]["reason"],
            )
            self.assertEqual(result.workspace_change, recorded["comparison"])
            self.assertEqual(
                {"root": str(workspace)}, recorded["comparison"]["workspace"]
            )
            self.assertFalse((state_dir / assurance_ledger.LEDGER_FILE_NAME).exists())
            self.assertFalse(
                (run_dir / supervisor_audit_vocabulary.MUTATION_ARTIFACT_NAME).exists()
            )

    def test_operator_process_activity_invalidates_without_attribution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._workspace(Path(tmp))
            path = workspace / "app" / "operator.txt"
            fake = _RecordingHarness(
                action=lambda: subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        "from pathlib import Path; import sys; "
                        "Path(sys.argv[1]).write_text('operator edit')",
                        str(path),
                    ],
                    capture_output=True,
                    check=True,
                )
            )
            result = self._run(
                "codex", Path(tmp), fake, workspace, plan_dir, state_dir, run_dir
            )
            self.assertEqual("unavailable", result.state)
            self.assertEqual("workspace-changed", result.outcome)
            self.assertFalse((state_dir / assurance_ledger.LEDGER_FILE_NAME).exists())

    def test_losing_the_change_artifact_does_not_accept_the_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._workspace(Path(tmp))
            fake = _RecordingHarness(writes=workspace / "app" / "escaped.txt")
            with mock.patch.object(
                assurance_models_artifacts,
                "write_audit_json",
                side_effect=OSError("disk-full"),
            ):
                result = self._run(
                    "codex", Path(tmp), fake, workspace, plan_dir, state_dir, run_dir
                )

            self.assertEqual("unavailable", result.state)
            self.assertEqual("workspace-changed", result.outcome)
            self.assertIsNotNone(result.workspace_change)

    def test_ref_changes_preserve_the_remote_tracking_exception(self) -> None:
        for ref, expected in (
            ("refs/remotes/origin/main", "ok"),
            ("refs/heads/sneaky", "workspace-changed"),
            ("refs/tags/sneaky", "workspace-changed"),
        ):
            with self.subTest(ref=ref), tempfile.TemporaryDirectory() as tmp:
                workspace, plan_dir, state_dir, run_dir = self._workspace(Path(tmp))
                app = workspace / "app"
                head = git_stdout(app, "rev-parse", "HEAD").strip()
                fake = _RecordingHarness(
                    action=lambda ref=ref, head=head, app=app: subprocess.run(
                        ["git", "update-ref", ref, head],
                        cwd=app,
                        capture_output=True,
                        check=True,
                    )
                )
                result = self._run(
                    "claude", Path(tmp), fake, workspace, plan_dir, state_dir, run_dir
                )
                self.assertEqual(expected, result.outcome)
                if expected == "workspace-changed":
                    refs = (result.workspace_change or {})["changed"]["app"][
                        "refs_added"
                    ]
                    self.assertEqual([ref], [entry["ref"] for entry in refs])

    def test_an_audit_that_leaves_the_workspace_alone_completes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._workspace(Path(tmp))
            fake = _RecordingHarness()

            result = self._run(
                "copilot", Path(tmp), fake, workspace, plan_dir, state_dir, run_dir
            )

            self.assertEqual("ok", result.outcome)
            self.assertEqual("pending", result.state)
            self.assertIsNone(result.workspace_change)
            self.assertFalse(
                (
                    run_dir / supervisor_audit_vocabulary.WORKSPACE_CHANGE_ARTIFACT_NAME
                ).exists()
            )

    def test_the_auditor_is_launched_read_only_without_the_plan_directory(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._workspace(Path(tmp))
            fake = _RecordingHarness()

            self._run("codex", Path(tmp), fake, workspace, plan_dir, state_dir, run_dir)

            call = fake.calls[0]
            self.assertIsNone(call["plan_dir"])
            self.assertFalse(call["unsafe_bypass"])
            self.assertEqual("audit-read-only", call["access"].name)
            self.assertEqual(run_dir / supervisor_audit.AUDIT_WORKDIR_NAME, call["cwd"])

    def test_a_harness_that_cannot_enforce_the_profile_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._workspace(Path(tmp))
            fake = _RecordingHarness()
            role = AuditRole(
                harness="claude",
                implementation_harness="codex",
                source="explicit",
                timeout_seconds=30.0,
            )
            unenforceable = harness_claude.ClaudeAdapter
            self.addCleanup(setattr, unenforceable, "enforces_audit_read_only", True)
            unenforceable.enforces_audit_read_only = False

            result = supervisor_audit.run_restricted_audit(
                role,
                run_dir=run_dir,
                run_id=RUN_ID,
                workspace_root=workspace,
                plan_dir=plan_dir,
                prompt="Audit this run.",
                repositories=("app",),
                state_dir=state_dir,
                _run_harness_fn=fake,
            )

            self.assertEqual("unavailable", result.outcome)
            self.assertEqual("profile-unenforceable", result.reason)
            self.assertEqual([], fake.calls)


if __name__ == "__main__":
    unittest.main()
