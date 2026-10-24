"""Test asl turn preflight baselines."""

from __future__ import annotations

import unittest
from typing import Any
from unittest import mock

from agent_session_loop import (
    events,
    handoff_guards,
    plans_session_scope,
    repository_evidence,
    review_guard,
    turn_baselines,
    workspace_evidence,
)

from .asl_turn_preflight_support import SESSION_ID, PreflightTestCase, fake_workspace

ASL_TEST_PORTABLE = True


class LegacyBaselineTests(PreflightTestCase):
    def test_explicit_external_roots_have_pre_turn_digests(self) -> None:
        snapshot = self.snapshot()
        approved = self.cwd.parent / "trial-data"
        approved.mkdir()
        record = approved / "trial.json"
        record.write_text("{}")
        with (
            mock.patch.object(
                handoff_guards,
                "_capture_legacy_implementation_snapshot",
                return_value=repository_evidence.WorkspaceSnapshot(),
            ),
            mock.patch.object(
                workspace_evidence, "_workspace_snapshot_unavailable", return_value={}
            ),
        ):
            baselines = self.baselines(
                snapshot,
                classification=self.classification(),
                authorized_external_roots=(approved.resolve(),),
            )
        self.assertIn(str(approved.resolve()), baselines.external_digests)
        self.assertIn(str(record.resolve()), baselines.external_digests)

    """The single-repository mode: one snapshot, or one fingerprint."""

    def test_an_implementation_turn_captures_the_legacy_snapshot(self) -> None:
        self.patch_digests()
        snapshot = self.snapshot()
        captured = repository_evidence.WorkspaceSnapshot()
        with (
            mock.patch.object(
                handoff_guards,
                "_capture_legacy_implementation_snapshot",
                return_value=captured,
            ),
            mock.patch.object(
                workspace_evidence, "_workspace_snapshot_unavailable", return_value={}
            ),
        ):
            baselines = self.baselines(snapshot, classification=self.classification())
        self.assertFalse(baselines.paused)
        self.assertIs(captured, baselines.legacy_snapshot)
        self.assertIsNone(baselines.review_fingerprint)
        self.assertTrue(baselines.protected_path_baseline)
        assert baselines.pending_exception_guard is not None
        self.assertEqual("legacy", baselines.pending_exception_guard["mode"])

    def test_an_unusable_legacy_snapshot_refuses_the_turn(self) -> None:
        self.patch_digests()
        snapshot = self.snapshot()
        with (
            mock.patch.object(
                handoff_guards,
                "_capture_legacy_implementation_snapshot",
                return_value=repository_evidence.WorkspaceSnapshot(),
            ),
            mock.patch.object(
                workspace_evidence,
                "_workspace_snapshot_unavailable",
                return_value={".": {"reason": "not-a-repository"}},
            ),
        ):
            baselines = self.baselines(snapshot, classification=self.classification())
        self.assertTrue(baselines.paused)
        self.assertIsNone(baselines.pending_exception_guard)
        self.assertFalse(baselines.protected_path_baseline)
        self.assertEqual(
            [events.PAUSE_REASON_WORKSPACE_WRITE_GUARD_UNAVAILABLE],
            [pause["reason"] for pause in self.ctx.pauses],
        )
        self.assertEqual([snapshot], self.ctx.final_snapshots)

    def test_a_legacy_review_turn_is_armed_from_its_fingerprint(self) -> None:
        # Without this branch the turn enters the harness with no exception
        # context and a crash skips protected-path adjudication entirely.
        self.patch_digests()
        snapshot = self.snapshot()
        fingerprint = {"head": "abc", "dirty": False}
        with (
            mock.patch.object(
                review_guard,
                "_capture_review_repo_fingerprint",
                return_value=fingerprint,
            ),
            mock.patch.object(
                review_guard, "_review_guard_unavailable_detail", return_value=None
            ),
            mock.patch.object(
                handoff_guards, "_capture_legacy_implementation_snapshot"
            ) as legacy,
        ):
            baselines = self.baselines(
                snapshot, classification=self.classification(review=True)
            )
        legacy.assert_not_called()
        self.assertFalse(baselines.paused)
        self.assertIsNone(baselines.legacy_snapshot)
        self.assertIs(fingerprint, baselines.review_fingerprint)
        assert baselines.pending_exception_guard is not None
        self.assertEqual("review", baselines.pending_exception_guard["mode"])
        self.assertIs(
            fingerprint,
            baselines.pending_exception_guard["review_fingerprint_before"],
        )

    def test_a_review_fingerprint_counts_as_a_protected_path_baseline(self) -> None:
        # It proves the whole worktree unchanged, which clears every path rule
        # at once rather than leaving them unproven.
        self.patch_digests()
        snapshot = self.snapshot()
        with (
            mock.patch.object(
                review_guard,
                "_capture_review_repo_fingerprint",
                return_value={"head": "abc"},
            ),
            mock.patch.object(
                review_guard, "_review_guard_unavailable_detail", return_value=None
            ),
        ):
            baselines = self.baselines(
                snapshot, classification=self.classification(review=True)
            )
        self.assertIsNone(baselines.workspace_snapshot)
        self.assertIsNone(baselines.legacy_snapshot)
        self.assertTrue(baselines.protected_path_baseline)

    def test_an_unusable_review_fingerprint_refuses_the_turn(self) -> None:
        self.patch_digests()
        snapshot = self.snapshot()
        with (
            mock.patch.object(
                review_guard,
                "_capture_review_repo_fingerprint",
                return_value={"head": None},
            ),
            mock.patch.object(
                review_guard,
                "_review_guard_unavailable_detail",
                return_value={"reason": "git-unavailable"},
            ),
        ):
            baselines = self.baselines(
                snapshot, classification=self.classification(review=True)
            )
        self.assertTrue(baselines.paused)
        self.assertIsNone(baselines.pending_exception_guard)
        self.assertEqual(
            [events.PAUSE_REASON_REVIEW_GUARD_UNAVAILABLE],
            [pause["reason"] for pause in self.ctx.pauses],
        )

    def test_the_control_plane_is_digested_for_the_legacy_mode_too(self) -> None:
        # The repository snapshot is silent about the plan directory, the state
        # root and the run directory, so the digest is not made conditional.
        digests = self.patch_digests({"plan": {"digest": "x"}})
        snapshot = self.snapshot()
        with (
            mock.patch.object(
                handoff_guards,
                "_capture_legacy_implementation_snapshot",
                return_value=repository_evidence.WorkspaceSnapshot(),
            ),
            mock.patch.object(
                workspace_evidence, "_workspace_snapshot_unavailable", return_value={}
            ),
        ):
            baselines = self.baselines(snapshot, classification=self.classification())
        digests.assert_called_once()
        self.assertEqual({"plan": {"digest": "x"}}, baselines.external_digests)
        assert baselines.pending_exception_guard is not None
        self.assertEqual(
            {"plan": {"digest": "x"}},
            baselines.pending_exception_guard["external_digests_before"],
        )
        self.assertGreater(baselines.turn_started_ns, 0)


class WorkspaceBaselineTests(PreflightTestCase):
    """The registered-workspace mode: one snapshot, and the repair it clears."""

    def setUp(self) -> None:
        super().setUp()
        self.workspace = fake_workspace("app", "docs")
        self.scope = plans_session_scope.SessionScope(
            reads=("app", "docs"), writes=("app",)
        )

    def capture(self, **overrides: Any) -> turn_baselines.TurnBaselines:
        snapshot = overrides.pop("snapshot", None) or self.snapshot()
        return self.baselines(
            snapshot,
            classification=overrides.pop(
                "classification", self.classification(repair=True)
            ),
            workspace=self.workspace,
            session_scope=self.scope,
            **overrides,
        )

    def test_a_prepared_review_base_is_reused_rather_than_recaptured(self) -> None:
        # The guard must measure against the base the session file declares,
        # not a second capture taken moments later.
        self.patch_digests()
        base = repository_evidence.WorkspaceSnapshot()
        with (
            mock.patch.object(
                handoff_guards, "_capture_workspace_write_guard_snapshot"
            ) as recapture,
            mock.patch.object(
                workspace_evidence, "_workspace_snapshot_unavailable", return_value={}
            ),
        ):
            baselines = self.capture(
                classification=self.classification(review=True), review_base=base
            )
        recapture.assert_not_called()
        self.assertIs(base, baselines.workspace_snapshot)
        assert baselines.pending_exception_guard is not None
        self.assertEqual("workspace", baselines.pending_exception_guard["mode"])

    def test_an_inventory_failure_refuses_the_turn(self) -> None:
        self.patch_digests()
        with mock.patch.object(
            handoff_guards,
            "_capture_workspace_write_guard_snapshot",
            side_effect=repository_evidence.RepositoryEvidenceError("git is gone"),
        ):
            baselines = self.capture()
        self.assertTrue(baselines.paused)
        self.assertIsNone(baselines.workspace_snapshot)
        self.assertIsNone(baselines.pending_exception_guard)
        (pause,) = self.ctx.pauses
        self.assertEqual(
            events.PAUSE_REASON_WORKSPACE_WRITE_GUARD_UNAVAILABLE, pause["reason"]
        )
        guard = pause["details"]["workspace_write_guard"]
        self.assertEqual(
            "workspace-inventory-failed", guard["unavailable"]["."]["reason"]
        )
        self.assertEqual(["app", "docs"], guard["read_aliases"])
        self.assertEqual(["app"], guard["write_aliases"])

    def test_a_retained_repair_that_cleared_its_scope_is_recorded(self) -> None:
        self.patch_digests()
        evidence = {"passed": True, "violations": []}
        with (
            mock.patch.object(
                handoff_guards,
                "_capture_workspace_write_guard_snapshot",
                return_value=repository_evidence.WorkspaceSnapshot(),
            ),
            mock.patch.object(
                workspace_evidence, "_workspace_snapshot_unavailable", return_value={}
            ),
            mock.patch.object(
                repository_evidence, "compare_workspace_snapshots", return_value={}
            ),
            mock.patch.object(
                workspace_evidence,
                "_workspace_scope_repair_comparison",
                return_value=({}, ["docs"]),
            ),
            mock.patch.object(
                workspace_evidence,
                "_workspace_write_guard_evidence",
                return_value=dict(evidence),
            ),
            mock.patch.object(
                workspace_evidence,
                "_session_expansion_evidence",
                return_value={"digest": "now"},
            ),
            mock.patch.object(
                workspace_evidence,
                "_workspace_scope_repair_is_fresh",
                return_value=True,
            ),
        ):
            baselines = self.capture(
                retained_scope_repair={
                    "session_id": SESSION_ID,
                    "snapshot_before": {},
                    "expansion_before_repair": {"digest": "then"},
                },
                regenerated_write_scope={"widened": ["docs"]},
            )
        self.assertFalse(baselines.paused)
        (name, payload) = self.ctx.persistence.events[0]
        self.assertEqual(events.WORKSPACE_WRITE_SCOPE_REPAIR_EVENT, name)
        recorded = payload["details"]["workspace_write_guard"]
        self.assertEqual(["docs"], recorded["repair_registry_delta"])
        self.assertEqual({"widened": ["docs"]}, recorded["write_scope_regeneration"])
        self.assertEqual({"digest": "now"}, payload["details"]["expansion"])

    def test_a_retained_repair_without_a_fresh_expansion_refuses_the_turn(self) -> None:
        # A scope that is clean only because nobody re-ran expansion is not
        # proof, so the clean comparison alone must not clear the repair.
        self.patch_digests()
        with (
            mock.patch.object(
                handoff_guards,
                "_capture_workspace_write_guard_snapshot",
                return_value=repository_evidence.WorkspaceSnapshot(),
            ),
            mock.patch.object(
                workspace_evidence, "_workspace_snapshot_unavailable", return_value={}
            ),
            mock.patch.object(
                repository_evidence, "compare_workspace_snapshots", return_value={}
            ),
            mock.patch.object(
                workspace_evidence,
                "_workspace_scope_repair_comparison",
                return_value=({}, []),
            ),
            mock.patch.object(
                workspace_evidence,
                "_workspace_write_guard_evidence",
                return_value={"passed": True, "violations": []},
            ),
            mock.patch.object(
                workspace_evidence,
                "_session_expansion_evidence",
                return_value={"digest": "now"},
            ),
            mock.patch.object(
                workspace_evidence,
                "_workspace_scope_repair_is_fresh",
                return_value=False,
            ),
        ):
            baselines = self.capture(
                retained_scope_repair={
                    "session_id": SESSION_ID,
                    "snapshot_before": {},
                    "expansion_before_repair": {"digest": "then"},
                }
            )
        self.assertTrue(baselines.paused)
        self.assertIsNone(baselines.pending_exception_guard)
        self.assertEqual([], self.ctx.persistence.events)
        (pause,) = self.ctx.pauses
        self.assertEqual(
            events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION, pause["reason"]
        )
        self.assertFalse(
            pause["details"]["workspace_write_guard"]["expansion_fresh"],
        )

    def test_a_retained_repair_with_no_baseline_cannot_clear_itself(self) -> None:
        self.patch_digests()
        with (
            mock.patch.object(
                handoff_guards,
                "_capture_workspace_write_guard_snapshot",
                return_value=repository_evidence.WorkspaceSnapshot(),
            ),
            mock.patch.object(
                workspace_evidence, "_workspace_snapshot_unavailable", return_value={}
            ),
            mock.patch.object(
                repository_evidence, "compare_workspace_snapshots"
            ) as compare,
            mock.patch.object(
                workspace_evidence,
                "_session_expansion_evidence",
                return_value={"digest": "now"},
            ),
        ):
            baselines = self.capture(
                retained_scope_repair={
                    "session_id": SESSION_ID,
                    "snapshot_before": None,
                }
            )
        compare.assert_not_called()
        self.assertTrue(baselines.paused)
        (pause,) = self.ctx.pauses
        guard = pause["details"]["workspace_write_guard"]["current_scope_evidence"]
        self.assertEqual("repair-baseline-missing", guard["unavailable"]["."]["reason"])


if __name__ == "__main__":
    unittest.main()
