"""How ``turn_postflight`` reacts to guard verdicts and what it records.

The sibling ``test_asl_turn_postflight`` covers the boundary's shape and the
guard order. This module covers the behavior after a guard answers:

* a finalization guard that pauses still reaches the protected-path step, in
  adjudicate-only mode, because a turn that breached both has to report both;
* "no baseline" and "proven clean" stay distinct answers to the protected-path
  guard;
* the terminating disposition is named once and gates the plan artifact guard;
* the review-base projection is the one durable record the boundary writes.
"""

from __future__ import annotations

import json
import unittest
from typing import Any

from agent_session_loop import events, plans_payloads
from agent_session_loop.run_errors import RunError

from .asl_turn_postflight_support import (
    PLAN_ARTIFACT,
    PROTECTED_PATH,
    REVIEW_MUTATION,
    WORKSPACE_WRITE,
    PostflightTestCase,
)

ASL_TEST_PORTABLE = True


class FinalizationPauseTests(PostflightTestCase):
    """A guard that pauses still has to reach the guards after it."""

    def test_a_paused_write_guard_still_adjudicates_the_protected_paths(self) -> None:
        self.recorder.verdicts[WORKSPACE_WRITE] = True

        outcome = self.workspace_turn()

        self.assertTrue(outcome.stopped)
        self.assertIn(PROTECTED_PATH, self.recorder.order)
        self.assertTrue(self.recorder.calls[PROTECTED_PATH]["adjudicate_only"])

    def test_a_paused_write_guard_still_adjudicates_the_plan_artifacts(self) -> None:
        # Staying in scope and leaving the other packets alone are separate
        # questions, so a turn that failed the first still owes an answer to
        # the second: the packets it changed would otherwise stay in the plan
        # with no verdict against them at all.
        self.recorder.verdicts[WORKSPACE_WRITE] = True

        outcome = self.workspace_turn()

        self.assertTrue(outcome.stopped)
        self.assertIn(PLAN_ARTIFACT, self.recorder.order)
        self.assertTrue(self.recorder.calls[PLAN_ARTIFACT]["adjudicate_only"])

    def test_a_paused_mutation_guard_still_adjudicates_the_protected_paths(
        self,
    ) -> None:
        self.recorder.verdicts[REVIEW_MUTATION] = True

        outcome = self.legacy_review_turn()

        self.assertTrue(outcome.stopped)
        self.assertTrue(self.recorder.calls[PROTECTED_PATH]["adjudicate_only"])

    def test_a_paused_write_guard_skips_the_mutation_guard(self) -> None:
        # One refusal per turn: the mutation guard would otherwise record a
        # second verdict against a comparison the write guard already refused.
        self.recorder.verdicts[WORKSPACE_WRITE] = True

        self.workspace_turn(is_review_session=True, review_fingerprint_before={})

        self.assertNotIn(REVIEW_MUTATION, self.recorder.order)

    def test_a_clean_turn_adjudicates_nothing(self) -> None:
        self.workspace_turn()

        self.assertFalse(self.recorder.calls[PROTECTED_PATH]["adjudicate_only"])


class ProtectedPathEvidenceTests(PostflightTestCase):
    """ "No baseline" and "proven clean" stay distinct answers."""

    def test_a_write_guard_comparison_supplies_the_changed_paths(self) -> None:
        self.recorder.changed_paths = ("app:src/main.py",)

        self.workspace_turn(turn_payload={"workspace_write_guard": {"passed": True}})

        self.assertEqual(
            ("app:src/main.py",),
            self.recorder.calls[PROTECTED_PATH]["changed_paths"],
        )

    def test_a_passing_legacy_review_turn_clears_every_path(self) -> None:
        # No write-guard comparison exists, but an unchanged worktree is a
        # stronger clearance than an empty change list -- not a missing one.
        self.legacy_review_turn()

        self.assertEqual((), self.recorder.calls[PROTECTED_PATH]["changed_paths"])

    def test_a_paused_legacy_review_turn_clears_nothing(self) -> None:
        self.recorder.verdicts[REVIEW_MUTATION] = True

        self.legacy_review_turn()

        self.assertIsNone(self.recorder.calls[PROTECTED_PATH]["changed_paths"])

    def test_a_turn_with_neither_evidence_reports_no_baseline(self) -> None:
        self.workspace_turn()

        self.assertIsNone(self.recorder.calls[PROTECTED_PATH]["changed_paths"])

    def test_a_convicted_protected_path_stops_the_turn(self) -> None:
        self.recorder.verdicts[PROTECTED_PATH] = True

        outcome = self.workspace_turn()

        self.assertTrue(outcome.stopped)
        self.assertIsNone(outcome.terminating_disposition)

    def test_a_convicted_protected_path_still_adjudicates_the_plan_artifacts(
        self,
    ) -> None:
        self.recorder.verdicts[PROTECTED_PATH] = True

        self.workspace_turn()

        self.assertTrue(self.recorder.calls[PLAN_ARTIFACT]["adjudicate_only"])


class TerminatingDispositionTests(PostflightTestCase):
    """The artifact guard is gated on the turn having a branch to take."""

    def test_a_turn_that_reaches_the_status_guards_skips_the_artifact_guard(
        self,
    ) -> None:
        self.recorder.disposition = None

        outcome = self.workspace_turn()

        self.assertFalse(outcome.stopped)
        self.assertIsNone(outcome.terminating_disposition)
        self.assertNotIn(PLAN_ARTIFACT, self.recorder.order)

    def test_a_terminating_turn_runs_the_artifact_guard_with_its_own_name(self) -> None:
        self.recorder.disposition = "timeout"

        outcome = self.workspace_turn()

        self.assertEqual("timeout", outcome.terminating_disposition)
        self.assertEqual(
            "timeout", self.recorder.calls[PLAN_ARTIFACT]["result_disposition"]
        )

    def test_a_refused_artifact_guard_stops_the_turn(self) -> None:
        self.recorder.verdicts[PLAN_ARTIFACT] = True

        outcome = self.workspace_turn()

        self.assertTrue(outcome.stopped)
        self.assertIsNone(outcome.terminating_disposition)


class ReviewBaseProjectionTests(PostflightTestCase):
    """The one durable record this boundary writes on its own."""

    def test_a_passing_workspace_review_turn_rewrites_its_result_artifact(
        self,
    ) -> None:
        turn_payload: dict[str, Any] = {"turn_index": 1}

        self.workspace_turn(is_review_session=True, turn_payload=turn_payload)

        self.assertEqual([{"alias": "app"}], turn_payload["review_repository_bases"])
        written = json.loads(
            self.paths()["result"].read_text(encoding="utf-8"),
        )
        self.assertEqual([{"alias": "app"}], written["review_repository_bases"])

    def test_a_non_review_turn_projects_no_bases(self) -> None:
        turn_payload: dict[str, Any] = {"turn_index": 1}

        self.workspace_turn(turn_payload=turn_payload)

        self.assertNotIn("review_repository_bases", turn_payload)
        self.assertFalse(self.paths()["result"].exists())

    def test_a_paused_write_guard_projects_no_bases(self) -> None:
        self.recorder.verdicts[WORKSPACE_WRITE] = True
        turn_payload: dict[str, Any] = {"turn_index": 1}

        self.workspace_turn(is_review_session=True, turn_payload=turn_payload)

        self.assertNotIn("review_repository_bases", turn_payload)

    def test_bases_that_cannot_be_projected_pause_the_run(self) -> None:
        self.recorder.review_bases = RunError("no base for app")

        outcome = self.workspace_turn(is_review_session=True, after=self.before)

        self.assertTrue(outcome.stopped)
        (pause,) = self.ctx.pauses
        self.assertEqual(events.PAUSE_REASON_REVIEW_GUARD_UNAVAILABLE, pause["reason"])
        self.assertEqual("no base for app", pause["details"]["error"])
        self.assertIn("final_snapshot", self.ctx.payload)
        # The refusal still reaches the protected-path step, like every other
        # finalization pause.
        self.assertTrue(self.recorder.calls[PROTECTED_PATH]["adjudicate_only"])

    def test_the_final_snapshot_falls_back_to_the_before_image(self) -> None:
        self.recorder.review_bases = RunError("no base for app")

        self.workspace_turn(is_review_session=True, after=None)

        self.assertEqual(
            plans_payloads.snapshot_payload(self.before),
            self.ctx.payload["final_snapshot"],
        )


if __name__ == "__main__":
    unittest.main()
