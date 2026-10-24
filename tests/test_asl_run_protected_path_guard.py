"""Protected path guard run tests."""

from __future__ import annotations

import argparse
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    control_queue,
    events,
    harnesses,
    intervention,
    plans_metadata,
    plans_registry,
    plans_session_ids,
    repository_evidence,
    review_guard,
    review_session_body,
    review_session_contract,
    run_loop,
    state,
    state_locks,
    verification_models,
)
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    _contract_block,
    init_git_repo,
    init_multi_repo_workspace,
    read_jsonl,
    write_multi_repo_plan,
    write_plan,
)

ASL_TEST_LANE = "integration"


class ProtectedPathGuardRunTests(unittest.TestCase):
    """End-to-end: a frozen path decides whether a finished turn is published.

    The guard is enqueued before the run rather than mid-turn because the
    external control queue is drained once before any turn starts, which makes
    "the guard is active for this turn" deterministic here. The mid-turn
    activation semantics -- a guard added late still compares against the
    turn-start snapshot -- are pinned by the controller and enforcement unit
    tests in tests/test_asl_guards.py.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.repo_root = self.tmp_path / "repo"
        self.plan_dir = self.tmp_path / "plan"
        self.state_dir = self.tmp_path / "state"
        init_git_repo(self.repo_root)
        self.protected = self.repo_root / "notes.md"
        self.protected.write_text("turn-start contents\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "notes.md"],
            cwd=self.repo_root,
            text=True,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "notes"],
            cwd=self.repo_root,
            text=True,
            capture_output=True,
            check=True,
        )
        write_plan(
            self.plan_dir,
            ("pending",),
            contracts_by_id={
                "1": _contract_block(
                    '{"type": "file_exists", "path": "README.md", "name": "readme"}',
                    session="1",
                )
            },
        )
        self.context = verification_models.VerificationContext.legacy(self.repo_root)
        self.cwd = self.repo_root
        self.review_harness: str | None = None

    def _args(self) -> argparse.Namespace:
        return argparse.Namespace(
            plan_dir=str(self.plan_dir),
            cwd=str(self.cwd),
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
            # Non-None only for the legacy-review fixture: a run that names a
            # review harness also requests a generated review of its own.
            review_harness=self.review_harness,
        )

    def _use_legacy_review_fixture(self) -> None:
        """Turn the pending session into a generated review of this repository.

        A legacy review turn is the one shape that captures neither write-guard
        snapshot, so it is the only way to reach the crash path with the review
        fingerprint as the turn's sole turn-start comparison.
        """
        self.review_harness = "claude"
        session_path = self.plan_dir / "session-1.md"
        session_path.write_text(
            review_session_body.build_generated_review_session_body(
                plans_session_ids.parse_session_id("1"),
                review_harness="claude",
                plan_dir=self.plan_dir,
            ),
            encoding="utf-8",
        )
        self.assertTrue(
            review_session_contract.is_generated_review_session_file(session_path)
        )

    def _use_workspace_fixture(self) -> None:
        """Re-point this fixture at a registered two-repository workspace.

        The class default is a legacy single repository, which declares no
        write aliases and therefore has no way to reach a write-scope
        violation. Only the composition tests need one, so the workspace is
        built on demand rather than in ``setUp``.
        """
        self.workspace_root = self.tmp_path / "workspace"
        self.repositories = init_multi_repo_workspace(
            self.workspace_root,
            DEFAULT_MULTI_REPO_SPECS[:2],
        )
        self.plan_dir = self.tmp_path / "workspace-plan"
        write_multi_repo_plan(
            self.plan_dir,
            ("pending",),
            specs=DEFAULT_MULTI_REPO_SPECS[:2],
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
        registry = plans_registry.read_repository_registry(self.plan_dir)
        assert registry is not None
        plans_metadata.save_workspace_plan_metadata(
            self.plan_dir,
            self.workspace_root,
            registry.digest,
        )
        self.context = verification_models.VerificationContext.from_plan(
            self.plan_dir,
            self.workspace_root,
        )
        self.cwd = self.workspace_root
        # The frozen file lives in the alias the session *may* write, so the
        # scope violation and the protected-path violation are independent
        # breaches rather than two names for one write.
        self.protected = self.repositories["SVC"] / "notes.md"
        self.protected.write_text("turn-start contents\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "notes.md"],
            cwd=self.repositories["SVC"],
            text=True,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "notes"],
            cwd=self.repositories["SVC"],
            text=True,
            capture_output=True,
            check=True,
        )

    def _turn_rejections(self, guard: str) -> list[dict[str, Any]]:
        event_path = (
            state.plan_state_dir(self.plan_dir, self.state_dir)
            / events.EVENTS_FILE_NAME
        )
        return [
            record["payload"]
            for record in read_jsonl(event_path)
            if record["name"] == events.TURN_REJECTED_EVENT
            and record["payload"].get("guard") == guard
        ]

    def _enqueue_guard(self, run_id: str, *protect_paths: str) -> None:
        dispatch = intervention.dispatch_control_command(
            self.plan_dir,
            control_queue.COMMAND_ADD_GUARD,
            state_dir=self.state_dir,
            protect_paths=list(protect_paths),
            reason="operator froze this file",
            expected_run_id=run_id,
        )
        self.assertEqual(
            tuple(protect_paths),
            tuple(
                f"{path.qualifier}:{path.pattern}"
                for path in dispatch.command.protect_paths
            ),
        )

    def _invoke(self, run_id: str) -> tuple[int, dict[str, Any]]:
        lock_info = state_locks.acquire_lock(self.plan_dir, self.state_dir)
        try:
            return run_loop.run_locked(
                args=self._args(),
                cwd=self.cwd,
                timeout=None,
                max_sessions=None,
                commit_mode=None,
                lock_info=lock_info,
                run_id=run_id,
                verification_context=self.context,
            )
        finally:
            state_locks.release_lock(self.plan_dir, self.state_dir)

    def _mark_complete(self) -> harnesses.HarnessResult:
        session_path = self.plan_dir / "session-1.md"
        session_path.write_text(
            session_path.read_text(encoding="utf-8").replace(
                "- **Status**: pending", "- **Status**: complete", 1
            ),
            encoding="utf-8",
        )
        return harnesses.HarnessResult(
            name="codex",
            argv=["fake"],
            returncode=0,
            stdout="ok\n",
            stderr="",
        )

    def _run_with(self, run_id: str, turn: Any) -> tuple[int, dict[str, Any]]:
        with mock.patch.object(harnesses, "run_harness", side_effect=turn):
            return self._invoke(run_id)

    def test_a_changed_protected_path_rejects_the_turn_and_keeps_the_change(
        self,
    ) -> None:
        run_id = "guard-protected-changed"
        self._enqueue_guard(run_id, ".:notes.md")

        def edit_protected(*_args: object, **_kwargs: object) -> Any:
            self.protected.write_text("worker rewrote this\n", encoding="utf-8")
            return self._mark_complete()

        exit_code, payload = self._run_with(run_id, edit_protected)

        self.assertEqual(1, exit_code)
        self.assertEqual(
            events.PAUSE_REASON_GUARD_PROTECTED_PATH_CHANGED,
            payload["pause_reason"],
        )
        guard = payload["turns"][0]["protected_path_guard"]
        self.assertFalse(guard["passed"])
        self.assertEqual([".:notes.md"], sorted(guard["matched_paths"].values())[0])
        self.assertEqual(guard["guard_ids"], list(guard["matched_paths"]))
        self.assertTrue(guard["evidence"])
        self.assertIn("command-gate", guard["coverage"][guard["guard_ids"][0]])
        # The completion the turn claimed is withdrawn...
        self.assertTrue(guard["reverted_to_pending"])
        # ...but the worker's edit is left exactly where the worker put it. A
        # guard decides what gets published, not what gets destroyed.
        self.assertTrue(guard["retained"])
        self.assertEqual(
            "worker rewrote this\n", self.protected.read_text(encoding="utf-8")
        )

    def test_a_protected_path_restored_before_the_end_passes(self) -> None:
        run_id = "guard-protected-restored"
        self._enqueue_guard(run_id, ".:notes.md")
        original = self.protected.read_text(encoding="utf-8")

        def edit_then_restore(*_args: object, **_kwargs: object) -> Any:
            self.protected.write_text("scratch\n", encoding="utf-8")
            self.protected.write_text(original, encoding="utf-8")
            return self._mark_complete()

        exit_code, payload = self._run_with(run_id, edit_then_restore)

        self.assertEqual(0, exit_code)
        self.assertIsNone(payload.get("pause_reason"))
        guard = payload["turns"][0]["protected_path_guard"]
        self.assertTrue(guard["passed"])
        self.assertNotIn(".:notes.md", guard["observed_paths"])

    def test_a_dirty_protected_path_at_turn_start_is_the_accepted_baseline(
        self,
    ) -> None:
        run_id = "guard-protected-dirty-baseline"
        self._enqueue_guard(run_id, ".:notes.md")
        # The operator's own uncommitted edit, made before the run. The guard
        # freezes the file as the turn found it, not as HEAD has it.
        self.protected.write_text("operator edit\n", encoding="utf-8")

        def touch_something_else(*_args: object, **_kwargs: object) -> Any:
            (self.repo_root / "README.md").write_text("worker\n", encoding="utf-8")
            return self._mark_complete()

        exit_code, payload = self._run_with(run_id, touch_something_else)

        self.assertEqual(0, exit_code)
        self.assertTrue(payload["turns"][0]["protected_path_guard"]["passed"])
        self.assertEqual("operator edit\n", self.protected.read_text(encoding="utf-8"))

    def test_a_write_outside_the_pattern_does_not_trip_the_guard(self) -> None:
        run_id = "guard-protected-unmatched"
        self._enqueue_guard(run_id, ".:docs/**/*.md")

        def edit_protected(*_args: object, **_kwargs: object) -> Any:
            self.protected.write_text("not under docs/\n", encoding="utf-8")
            return self._mark_complete()

        exit_code, payload = self._run_with(run_id, edit_protected)

        self.assertEqual(0, exit_code)
        guard = payload["turns"][0]["protected_path_guard"]
        self.assertTrue(guard["passed"])
        self.assertIn(".:notes.md", guard["observed_paths"])

    def test_scope_violation_still_adjudicates_protected_paths(self) -> None:
        """Two breaches in one turn are both reported, not just the first.

        The write guard pauses first, so its reason is the one the operator
        acts on. But a turn that also moved a frozen path has to say so: an
        operator who repairs only the scope and reruns would otherwise never
        learn the guard was breached at all.
        """
        self._use_workspace_fixture()
        run_id = "guard-protected-with-scope-violation"
        self._enqueue_guard(run_id, "SVC:notes.md")
        out_of_scope = self.repositories["OAK"] / "README.md"

        def breach_both(*_args: object, **_kwargs: object) -> Any:
            out_of_scope.write_text("outside the write scope\n", encoding="utf-8")
            self.protected.write_text("worker rewrote this\n", encoding="utf-8")
            return self._mark_complete()

        exit_code, payload = self._run_with(run_id, breach_both)

        self.assertEqual(1, exit_code)
        # The primary pause is untouched: same reason, same remediation.
        self.assertEqual(
            events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
            payload["pause_reason"],
        )
        self.assertEqual(
            ["OAK:README.md"],
            payload["turns"][0]["workspace_write_guard"]["violations"][0][
                "qualified_paths"
            ],
        )

        guard = payload["turns"][0]["protected_path_guard"]
        self.assertFalse(guard["passed"])
        self.assertTrue(guard["adjudicated_only"])
        self.assertEqual(
            events.PAUSE_REASON_GUARD_PROTECTED_PATH_CHANGED, guard["reason"]
        )
        self.assertEqual(
            events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
            guard["deferred_to_pause_reason"],
        )
        self.assertEqual(guard["guard_ids"], list(guard["matched_paths"]))
        self.assertEqual(["SVC:notes.md"], sorted(guard["matched_paths"].values())[0])
        # The matched repository's before/after vectors survive the
        # composition, which is what "restore or accept" needs.
        self.assertTrue(guard["evidence"])
        self.assertTrue(guard["retained"])
        # The write guard already withdrew the session; this guard does not
        # withdraw it a second time.
        self.assertFalse(guard["reverted_to_pending"])
        self.assertIn(
            "- **Status**: pending",
            (self.plan_dir / "session-1.md").read_text(encoding="utf-8"),
        )

        # The pause an operator reads carries both halves.
        self.assertEqual(guard, payload["pause_details"]["protected_path_guard"])
        rejections = self._turn_rejections("protected-path-guard")
        self.assertEqual(1, len(rejections))
        self.assertFalse(rejections[0]["reopened"])
        self.assertEqual(guard["guard_ids"], rejections[0]["detail"]["guard_ids"])
        self.assertEqual(
            "worker rewrote this\n", self.protected.read_text(encoding="utf-8")
        )

    def test_harness_exception_still_adjudicates_protected_paths(self) -> None:
        """A crashing turn still answers the operator's standing instruction.

        The exception cleanup is the last place the run's guards are still
        alive, so the verdict is reached there or never. The exception stays
        the outcome -- it is re-raised unchanged -- and the verdict rides
        beside it in the event stream.
        """
        run_id = "guard-protected-exception"
        self._enqueue_guard(run_id, ".:notes.md")

        def edit_then_raise(*_args: object, **_kwargs: object) -> Any:
            self.protected.write_text("worker rewrote this\n", encoding="utf-8")
            raise RuntimeError("synthetic harness crash")

        with (
            mock.patch.object(harnesses, "run_harness", side_effect=edit_then_raise),
            self.assertRaisesRegex(RuntimeError, "synthetic harness crash"),
        ):
            self._invoke(run_id)

        rejections = self._turn_rejections("protected-path-guard")
        self.assertEqual(1, len(rejections))
        rejection = rejections[0]
        self.assertEqual(
            events.PAUSE_REASON_GUARD_PROTECTED_PATH_CHANGED, rejection["reason"]
        )
        self.assertFalse(rejection["reopened"])
        self.assertTrue(rejection["detail"]["adjudicated_only"])
        self.assertEqual(
            [".:notes.md"],
            sorted(rejection["detail"]["matched_paths"].values())[0],
        )
        self.assertEqual(
            rejection["detail"]["guard_ids"],
            list(rejection["detail"]["matched_paths"]),
        )
        self.assertEqual(
            "worker rewrote this\n", self.protected.read_text(encoding="utf-8")
        )

    def test_legacy_review_exception_still_adjudicates_protected_paths(self) -> None:
        """A crashing legacy review is adjudicated like any other crashing turn.

        It captures no write-guard snapshot, so before this it entered the
        harness with no exception context at all and the crash path skipped the
        protected-path guard entirely -- the guard then expired with the
        operator's standing instruction never answered. Its turn-start review
        fingerprint is the comparison that answers it.
        """
        self._use_legacy_review_fixture()
        run_id = "guard-protected-legacy-review-exception"
        self._enqueue_guard(run_id, ".:notes.md")

        def edit_then_raise(*_args: object, **_kwargs: object) -> Any:
            self.protected.write_text("review rewrote this\n", encoding="utf-8")
            raise RuntimeError("synthetic review crash")

        with (
            mock.patch.object(harnesses, "run_harness", side_effect=edit_then_raise),
            self.assertRaisesRegex(RuntimeError, "synthetic review crash"),
        ):
            self._invoke(run_id)

        rejections = self._turn_rejections("protected-path-guard")
        self.assertEqual(1, len(rejections))
        rejection = rejections[0]
        self.assertEqual(
            events.PAUSE_REASON_GUARD_PROTECTED_PATH_CHANGED, rejection["reason"]
        )
        self.assertTrue(rejection["detail"]["adjudicated_only"])
        self.assertFalse(rejection["reopened"])
        self.assertEqual(
            [".:notes.md"],
            sorted(rejection["detail"]["matched_paths"].values())[0],
        )
        self.assertEqual(
            rejection["detail"]["guard_ids"],
            list(rejection["detail"]["matched_paths"]),
        )
        # The review's edit is retained, exactly as it is for an implementation
        # turn: the guard decides what gets published, not what survives.
        self.assertEqual(
            "review rewrote this\n", self.protected.read_text(encoding="utf-8")
        )

    def test_legacy_review_exception_without_evidence_records_unavailable(
        self,
    ) -> None:
        """Unreadable post-crash evidence is a verdict, not a silent clearance.

        The recapture is what names the changed paths here, so a recapture that
        cannot answer must leave the guard explicitly unavailable rather than
        clear -- the failure mode the finding calls out is a frozen path with
        neither a violation nor an unavailable verdict.
        """
        self._use_legacy_review_fixture()
        run_id = "guard-protected-legacy-review-blind"
        self._enqueue_guard(run_id, ".:notes.md")

        def edit_then_raise(*_args: object, **_kwargs: object) -> Any:
            self.protected.write_text("review rewrote this\n", encoding="utf-8")
            raise RuntimeError("synthetic review crash")

        real_capture = review_guard._capture_review_repo_fingerprint
        captures: list[object] = []

        def capture_then_fail(*args: object, **kwargs: object) -> Any:
            # Only the post-crash recapture fails; the turn-start capture has
            # to succeed or the turn never reaches the harness at all.
            captures.append(args)
            if len(captures) == 1:
                return real_capture(*args, **kwargs)
            raise repository_evidence.RepositoryEvidenceError("git is gone")

        with (
            mock.patch.object(harnesses, "run_harness", side_effect=edit_then_raise),
            mock.patch.object(
                review_guard,
                "_capture_review_repo_fingerprint",
                side_effect=capture_then_fail,
            ),
            self.assertRaisesRegex(RuntimeError, "synthetic review crash"),
        ):
            self._invoke(run_id)

        self.assertEqual(2, len(captures))

        rejections = self._turn_rejections("protected-path-guard")
        self.assertEqual(1, len(rejections))
        self.assertEqual(
            events.PAUSE_REASON_GUARD_PROTECTED_PATH_UNAVAILABLE,
            rejections[0]["reason"],
        )
        self.assertEqual(
            "review rewrote this\n", self.protected.read_text(encoding="utf-8")
        )


if __name__ == "__main__":
    unittest.main()
