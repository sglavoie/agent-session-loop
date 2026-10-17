"""Repair publication, regate and continuation tests."""

import json
import subprocess
import unittest
from unittest import mock

from agent_session_loop import (
    authoring_transaction,
    events,
    plans_errors,
    plans_progress,
    plans_snapshot,
    progress_repair,
    readiness_gate,
    review,
    review_continuation,
    review_repair,
    state_locks,
)
from tests.review_materialization_support import MaterializationFixtures
from tests.support import (
    _contract_block,
    _structured_review_finding,
    init_git_repo,
)


class ReviewRepairGateAndContinuationTests(MaterializationFixtures, unittest.TestCase):
    """Gate materialized repairs and project continuation boundaries."""

    def test_commit_failure_rolls_back_atomically(self) -> None:
        self._write_plan([self._finding("F1", "a"), self._finding("F2", "b")])
        manifest_before = (self.plan_dir / "manifest.md").read_text(encoding="utf-8")
        with mock.patch.object(
            authoring_transaction.PlanTransaction,
            "commit",
            side_effect=plans_errors.PlanError("boom"),
        ):
            result = self._materialize()

        self.assertFalse(result.ok)
        self.assertEqual(review_repair.REVIEW_REPAIR_REASON_MALFORMED, result.reason)
        self.assertEqual([], sorted(self.plan_dir.glob("session-2.*.md")))
        self.assertEqual(
            manifest_before,
            (self.plan_dir / "manifest.md").read_text(encoding="utf-8"),
        )

    def test_without_cwd_leaves_children_draft(self) -> None:
        self._write_plan([self._finding("F1", "a")])
        result = self._materialize()

        self.assertTrue(result.ok)
        self.assertFalse(result.loop_ready)
        manifest = (self.plan_dir / "manifest.md").read_text(encoding="utf-8")
        self.assertIn("**Readiness**: draft", manifest)

    def test_active_lock_pauses_malformed(self) -> None:
        self._write_plan([self._finding("F1", "a")])
        state_locks.acquire_lock(self.plan_dir, self.state_dir)
        self.addCleanup(state_locks.release_lock, self.plan_dir, self.state_dir)
        result = self._materialize()

        self.assertFalse(result.ok)
        self.assertEqual(review_repair.REVIEW_REPAIR_REASON_MALFORMED, result.reason)
        self.assertIn("lock", result.error)
        # Holding the lock externally, hold_lock=False still works.
        result_held = self._materialize(hold_lock=False)
        self.assertTrue(result_held.ok)

    def test_cwd_gate_marks_plan_loop_ready_with_absolute_repair_path(self) -> None:
        repo = self.tmp_path / "repo"
        init_git_repo(repo)
        (repo / "README.md").write_text("readme\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "-c",
                "user.email=a@b.c",
                "-c",
                "user.name=x",
                "commit",
                "-q",
                "-m",
                "readme",
            ],
            check=True,
        )
        contract_one = _contract_block(
            '{"type": "file_exists", "path": "README.md", "name": "readme-1"}',
            session="1",
        )
        finding = _structured_review_finding(
            finding_id="F1",
            repair_group="docs",
            evidence=[{"path": "README.md"}],
            done_criteria=["README stays present."],
            verification_checks=[
                {
                    "type": "file_exists",
                    "name": "readme-present",
                    "path": str(repo / "README.md"),
                }
            ],
        )
        self._write_plan([finding], session_one_contract=contract_one)
        progress_repair.repair_progress_journal(self.plan_dir)

        result = self._materialize(cwd=repo)

        self.assertTrue(result.ok, msg=str(result.blockers))
        self.assertTrue(result.loop_ready, msg=str(result.blockers))
        self.assertEqual(review_repair.REVIEW_REPAIR_REASON_OK, result.reason)
        manifest = (self.plan_dir / "manifest.md").read_text(encoding="utf-8")
        self.assertIn("**Readiness**: loop-ready", manifest)

    def test_child_escaping_bound_root_is_refused_before_the_plan_is_written(
        self,
    ) -> None:
        repo = self._committed_git_repo("repo")
        outside = self.tmp_path / "outside"
        init_git_repo(outside)
        (outside / "pkg").mkdir(parents=True, exist_ok=True)
        (outside / "pkg" / "target.py").write_text("x = 1\n", encoding="utf-8")
        finding = _structured_review_finding(
            finding_id="F1",
            repair_group="outside",
            evidence=[{"path": "README.md"}],
            done_criteria=["The outside module is fixed."],
            verification_checks=[
                {
                    "type": "file_exists",
                    "name": "outside-present",
                    "path": str(outside / "pkg" / "target.py"),
                }
            ],
        )
        self._write_plan([finding])
        before = self._plan_bytes()

        result = self._materialize(cwd=repo)

        self.assertEqual(
            review_repair.REVIEW_REPAIR_REASON_NOT_LOOP_READY, result.reason
        )
        self.assertEqual((), result.children)
        self.assertFalse(result.loop_ready)
        # Both pre-write rules refuse this child: its target escapes the bound
        # root and its check cannot resolve there either.
        self.assertEqual(
            ("repository-footprint", "repository-contract"),
            result.non_bypassable_blockers,
        )
        self.assertTrue(
            any("escapes the bound root" in blocker for blocker in result.blockers),
            msg=str(result.blockers),
        )
        self.assertEqual(before, self._plan_bytes())
        self.assertFalse((self.plan_dir / "session-2.1.md").exists())

    def test_stale_progress_journal_is_repaired_rather_than_blocking_the_gate(
        self,
    ) -> None:
        repo = self._committed_git_repo("repo")
        contract_one = _contract_block(
            '{"type": "file_exists", "path": "README.md", "name": "readme-1"}',
            session="1",
        )
        finding = _structured_review_finding(
            finding_id="F1",
            repair_group="docs",
            evidence=[{"path": "README.md"}],
            done_criteria=["README stays present."],
            verification_checks=[
                {
                    "type": "file_exists",
                    "name": "readme-present",
                    "path": str(repo / "README.md"),
                }
            ],
        )
        self._write_plan([finding], session_one_contract=contract_one)
        journal = plans_progress.progress_path(self.plan_dir)
        self.assertFalse(journal.exists())

        result = self._materialize(cwd=repo)

        self.assertTrue(result.loop_ready, msg=str(result.blockers))
        self.assertEqual(review_repair.REVIEW_REPAIR_REASON_OK, result.reason)
        self.assertTrue(journal.exists())
        self.assertIn(
            "**Readiness**: loop-ready",
            (self.plan_dir / "manifest.md").read_text(encoding="utf-8"),
        )

    def test_reopened_plan_is_regated_after_repairs_grow_the_session_inventory(
        self,
    ) -> None:
        repo = self._committed_git_repo("repo")
        contract_one = _contract_block(
            '{"type": "file_exists", "path": "README.md", "name": "readme-1"}',
            session="1",
        )
        findings = [
            _structured_review_finding(
                finding_id=finding_id,
                repair_group=repair_group,
                evidence=[{"path": "README.md"}],
                done_criteria=["README stays present."],
                verification_checks=[
                    {
                        "type": "file_exists",
                        "name": f"readme-present-{finding_id}",
                        "path": str(repo / "README.md"),
                    }
                ],
            )
            for finding_id, repair_group in (("F1", "docs"), ("F2", "runner"))
        ]
        self._write_plan(findings, session_one_contract=contract_one)

        authorized = readiness_gate._reopened_plan_gate_payload(
            plans_snapshot.inspect_plan(self.plan_dir),
            max_pending_sessions=1,
            repair_session_ids=[],
        )
        self.assertTrue(authorized["ok"])
        self.assertEqual(0, authorized["pending_sessions"])
        self.assertEqual(
            "all sessions complete; no turns required",
            authorized["findings"][0]["message"],
        )

        result = self._materialize(cwd=repo)
        self.assertTrue(result.ok, msg=str(result.blockers))
        repair_session_ids = [child.session_id.value for child in result.children]
        self.assertEqual(["2.1", "2.2"], repair_session_ids)

        regated = readiness_gate._reopened_plan_gate_payload(
            plans_snapshot.inspect_plan(self.plan_dir),
            max_pending_sessions=1,
            repair_session_ids=repair_session_ids,
        )
        self.assertFalse(regated["ok"])
        self.assertEqual(2, regated["pending_sessions"])
        self.assertEqual(4, regated["total_sessions"])
        self.assertEqual(repair_session_ids, regated["repair_session_ids"])
        self.assertEqual("env-turn-budget", regated["findings"][0]["category"])
        # Unstamped, the gate is the repairs one. The review-session append is
        # the other growth path and has to be distinguishable from this.
        self.assertEqual(events.REVIEW_REPAIR_REOPENED_EVENT, regated["trigger"])

    def test_the_two_growth_paths_stamp_distinguishable_triggers(self) -> None:
        # Same verdict shape from both routes, so the trigger is the only thing
        # that tells an audit which growth a gate authorized.
        self._write_plan([self._finding("F1", "runner")])
        snapshot = plans_snapshot.inspect_plan(self.plan_dir)

        repairs = readiness_gate._reopened_plan_gate_payload(
            snapshot, max_pending_sessions=1, repair_session_ids=["2.1"]
        )
        review_append = readiness_gate._reopened_plan_gate_payload(
            snapshot,
            max_pending_sessions=1,
            repair_session_ids=[],
            trigger=events.REVIEW_SESSION_CREATED_EVENT,
        )

        self.assertEqual(events.REVIEW_REPAIR_REOPENED_EVENT, repairs["trigger"])
        self.assertEqual(events.REVIEW_SESSION_CREATED_EVENT, review_append["trigger"])
        self.assertNotEqual(repairs["trigger"], review_append["trigger"])
        self.assertEqual(repairs["total_sessions"], review_append["total_sessions"])

    def test_continuation_projects_disabled_and_exhausted_cap_boundaries(
        self,
    ) -> None:
        finding = self._finding("F1", "runner")
        self._write_plan([finding])
        fingerprint = review.finding_fingerprint(finding)
        event_path = self._write_review_events(
            {
                "cycle": 1,
                "review_session_id": "2",
                "disposition": "cycles-disabled",
                "fingerprints": [fingerprint],
                "repair_session_ids": [],
                "max_review_fix_cycles": 0,
            }
        )

        disabled = review_continuation.evaluate_review_continuation(
            plans_snapshot.inspect_plan(self.plan_dir), event_path
        )

        self.assertEqual("materialize-repairs", disabled.action)
        self.assertFalse(disabled.eligible)
        self.assertEqual("higher-cap-required", disabled.blocker)
        self.assertEqual(1, disabled.minimum_required_cap)
        self.assertEqual((fingerprint,), disabled.source_fingerprints)
        self.assertIsNone(disabled.source_review_output_sha256)
        self.assertIsNotNone(disabled.current_review_output_sha256)
        self.assertIsNotNone(disabled.current_review_artifact_sha256)

        resumed = review_continuation.evaluate_review_continuation(
            plans_snapshot.inspect_plan(self.plan_dir),
            event_path,
            max_review_fix_cycles=1,
        )
        self.assertTrue(resumed.eligible)
        self.assertIsNone(resumed.blocker)

        event_path = self._write_review_events(
            {
                "cycle": 1,
                "review_session_id": "1",
                "disposition": "repairs-materialized",
                "fingerprints": ["finding:old"],
                "repair_session_ids": ["1.1"],
                "max_review_fix_cycles": 1,
            },
            {
                "cycle": 2,
                "review_session_id": "2",
                "disposition": "cycle-cap-exhausted",
                "fingerprints": [fingerprint],
                "repair_session_ids": [],
                "max_review_fix_cycles": 1,
            },
        )
        exhausted = review_continuation.evaluate_review_continuation(
            plans_snapshot.inspect_plan(self.plan_dir), event_path
        )
        self.assertEqual(1, exhausted.historical_materializations)
        self.assertEqual(2, exhausted.minimum_required_cap)
        self.assertEqual("higher-cap-required", exhausted.blocker)

    def test_continuation_rechecks_malformed_artifact_after_correction(
        self,
    ) -> None:
        findings = [self._finding(f"F{i}", f"g{i}") for i in range(9)]
        self._write_plan(findings)
        event_path = self._write_review_events(
            {
                "cycle": 1,
                "review_session_id": "2",
                "disposition": "malformed-review-repair",
                "fingerprints": [
                    review.finding_fingerprint(finding) for finding in findings
                ],
                "repair_session_ids": [],
                "max_review_fix_cycles": 1,
            }
        )

        malformed = review_continuation.evaluate_review_continuation(
            plans_snapshot.inspect_plan(self.plan_dir), event_path
        )
        self.assertFalse(malformed.eligible)
        self.assertEqual("artifact-invalid", malformed.blocker)
        self.assertFalse(malformed.artifact_valid)
        self.assertIn("exceed the maximum of 8", malformed.artifact_error or "")

        findings[-1]["repair_group"] = "g7"
        (self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
            json.dumps({"version": 2, "outcome": "findings", "findings": findings}),
            encoding="utf-8",
        )
        corrected = review_continuation.evaluate_review_continuation(
            plans_snapshot.inspect_plan(self.plan_dir), event_path
        )
        self.assertTrue(corrected.artifact_valid)
        self.assertTrue(corrected.eligible)
        self.assertEqual("materialize-repairs", corrected.action)
        self.assertIsNone(corrected.blocker)

    def test_continuation_marks_manually_changed_all_clear_invalid(
        self,
    ) -> None:
        self._write_plan([], outcome="all-clear")
        event_path = self._write_review_events(
            {
                "cycle": 1,
                "review_session_id": "2",
                "disposition": "cycles-disabled",
                "fingerprints": ["finding:original"],
                "repair_session_ids": [],
                "max_review_fix_cycles": 0,
            }
        )

        projected = review_continuation.evaluate_review_continuation(
            plans_snapshot.inspect_plan(self.plan_dir),
            event_path,
            max_review_fix_cycles=1,
        )

        self.assertEqual("materialize-repairs", projected.action)
        self.assertFalse(projected.eligible)
        self.assertEqual("artifact-invalid", projected.blocker)
        self.assertIs(projected.artifact_valid, False)
        self.assertEqual(
            "the completed findings review no longer has repair findings; "
            "a manually changed all-clear artifact cannot finalize it",
            projected.artifact_error,
        )
        self.assertIsNotNone(projected.current_review_output_sha256)
        self.assertIsNotNone(projected.current_review_artifact_sha256)


if __name__ == "__main__":
    unittest.main()
