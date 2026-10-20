"""Prior-cycle evidence and recovered reopen baselines."""

from __future__ import annotations

import unittest
from typing import Any

from agent_session_loop import (
    events,
    review,
    review_continuation,
    review_delta,
    review_session_body,
)

#: The symbols this module took from the runner. The program's no-alias rule
#: says the runner keeps none of them behind, so a re-export added later to
from tests.asl_review_delta_support import DeltaHarness
from tests.support import (
    read_jsonl,
)


class PriorCycleEvidenceTests(DeltaHarness):
    """What the boundary is willing to treat as the last repair."""

    def test_no_cycle_at_all_is_not_a_degraded_scope(self) -> None:
        # `None` rather than a full-scope delta: there is no prior review to
        # re-verify, so the caller writes an ordinary review, not a re-review.
        self.assertIsNone(
            review_delta._last_repairs_materialized_cycle(self.event_path)
        )
        self.assertEqual((None, "no-prior-repair-cycle"), self.compute())

    def test_a_cycle_that_did_not_materialize_repairs_is_skipped(self) -> None:
        self.record_repair_cycle(cycle=1, disposition="exhausted")
        self.assertIsNone(
            review_delta._last_repairs_materialized_cycle(self.event_path)
        )

    def test_the_newest_materialized_reopen_supersedes_its_own_findings(self) -> None:
        self.record_repair_cycle()
        events.append_event(
            self.event_path,
            events.Event(
                events.REVIEW_REPAIR_REOPENED_EVENT,
                {
                    "materialized": True,
                    "repo_head": self.head(),
                    "findings": ["stale"],
                    "current_findings": ["fresh"],
                    "repositories": ["stale"],
                    "current_repositories": ["fresh"],
                },
            ),
        )
        cycle = review_delta._last_repairs_materialized_cycle(self.event_path)
        assert cycle is not None
        self.assertEqual(["fresh"], cycle["findings"])
        self.assertEqual(["fresh"], cycle["repositories"])

    def test_findings_are_read_from_the_review_still_on_disk(self) -> None:
        self.assertEqual(
            [self.finding], review_delta._prior_review_findings(self.plan_dir)
        )
        (self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).unlink()
        self.assertEqual([], review_delta._prior_review_findings(self.plan_dir))

    def test_an_unreadable_artifact_digest_is_none_not_a_raised_error(self) -> None:
        # A `PlanError` here would abort the caller mid-comparison and let a
        # read failure look like a cycle result.
        self.assertNotIn(None, review_delta._review_artifact_digests(self.plan_dir))
        (self.plan_dir / review.FINAL_REVIEW_FILENAME).unlink()
        (self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).unlink()
        self.assertEqual(
            (None, None), review_delta._review_artifact_digests(self.plan_dir)
        )


class RecoveredReopenBaselineTests(DeltaHarness):
    """A reopen written before the baseline was recorded still names one.

    The newest materialized reopen supersedes the cycle record it descends
    from, and reopen records written before the writer carried the reviewed
    tree name no base at all — so every rereview that followed one of them
    re-read the whole repository. The evidence was never lost: it is still on
    the source cycle's own append-only record. What is asserted here is that it
    is read back only when it can be attributed beyond doubt, only into fields
    the reopen does not already fill, and only in memory.
    """

    def record_reopen(self, **overrides: Any) -> None:
        record: dict[str, Any] = {
            "materialized": True,
            "source_cycle": 1,
            "source_review_session_id": "2",
            "repair_session_ids": ["2.1"],
        }
        record.update(overrides)
        events.append_event(
            self.event_path,
            events.Event(events.REVIEW_REPAIR_REOPENED_EVENT, record),
        )

    def recovered(self) -> dict[str, Any]:
        cycle = review_delta._last_repairs_materialized_cycle(self.event_path)
        assert cycle is not None
        return cycle

    def test_a_baseless_reopen_recovers_the_tree_its_source_reviewed(self) -> None:
        base = self.head()
        self.record_repair_cycle(repo_head=base, worktree_dirty=True, dirty_paths=["x"])
        self.record_reopen()
        cycle = self.recovered()
        self.assertEqual(base, cycle["repo_head"])
        self.assertTrue(cycle["worktree_dirty"])
        self.assertEqual(["x"], cycle["dirty_paths"])

    def test_the_recovered_base_is_the_reviewed_tree_not_the_current_head(
        self,
    ) -> None:
        base = self.head()
        self.record_repair_cycle(repo_head=base)
        self.record_reopen()
        (self.repo_root / "repair.py").write_text("repaired\n", encoding="utf-8")
        self.git("add", "repair.py")
        self.git("commit", "-m", "repair")

        delta, fallback = self.compute()

        assert delta is not None
        self.assertIsNone(fallback)
        self.assertEqual(review_session_body.REVIEW_SCOPE_DELTA, delta["mode"])
        self.assertEqual(base, delta["base_sha"])
        # The repair committed after the review has to be inside the scope; a
        # base taken from `HEAD` would leave the changed-file list empty and the
        # rereview reading nothing at all.
        self.assertEqual(["repair.py"], delta["changed_files"])

    def test_recovery_never_overwrites_what_the_reopen_recorded(self) -> None:
        recorded = self.head()
        self.record_repair_cycle(repo_head="f" * 40, dirty_paths=["stale"])
        self.record_reopen(repo_head=recorded, worktree_dirty=False, dirty_paths=[])
        cycle = self.recovered()
        self.assertEqual(recorded, cycle["repo_head"])
        self.assertEqual([], cycle["dirty_paths"])

    def test_current_repository_evidence_survives_recovery(self) -> None:
        self.record_repair_cycle(repositories=["stale"])
        self.record_reopen(current_repositories=["fresh"], current_findings=["fresh"])
        cycle = self.recovered()
        self.assertEqual(["fresh"], cycle["repositories"])
        self.assertEqual(["fresh"], cycle["findings"])

    def test_a_registered_reopen_with_no_vector_recovers_the_recorded_one(
        self,
    ) -> None:
        bases = [{"alias": "SVC", "head": "b" * 40, "worktree_dirty": False}]
        self.record_repair_cycle(repo_head=None, repositories=bases)
        self.record_reopen()
        self.assertEqual(bases, self.recovered()["repositories"])

    def test_an_unattributable_reopen_keeps_the_full_scope_fallback(self) -> None:
        self.record_repair_cycle(repo_head=self.head())
        self.record_reopen(source_cycle=None, source_review_session_id=None)
        self.assertNotIn("repo_head", self.recovered())
        delta, fallback = self.compute()
        assert delta is not None
        self.assertEqual(review_session_body.REVIEW_SCOPE_FULL, delta["mode"])
        self.assertEqual("no-recorded-base", fallback)

    def test_a_reopen_naming_another_cycle_recovers_nothing(self) -> None:
        self.record_repair_cycle(cycle=1, review_session_id="2", repo_head=self.head())
        self.record_reopen(source_cycle=2, source_review_session_id="4")
        self.assertNotIn("repo_head", self.recovered())

    def test_cycles_that_disagree_about_the_tree_recover_nothing(self) -> None:
        self.record_repair_cycle(repo_head=self.head())
        self.record_repair_cycle(repo_head="f" * 40)
        self.record_reopen()
        self.assertNotIn("repo_head", self.recovered())

    def test_a_source_cycle_whose_digests_disagree_recovers_nothing(self) -> None:
        self.record_repair_cycle(repo_head=self.head(), review_artifact_sha256="c" * 64)
        self.record_reopen(source_review_artifact_sha256="d" * 64)
        self.assertNotIn("repo_head", self.recovered())

    def test_a_refused_reopen_is_still_not_the_newest_repair_evidence(self) -> None:
        # Recovery reads the source cycle, which is also what a refused reopen
        # leaves standing as the last materialization; the refusal must not
        # become the evidence just because it now knows a base.
        base = self.head()
        self.record_repair_cycle(repo_head=base)
        self.record_reopen(materialized=False, repo_head="f" * 40)
        cycle = self.recovered()
        self.assertEqual(base, cycle["repo_head"])
        self.assertEqual(
            review_continuation.REVIEW_CYCLE_DISPOSITION_REPAIRS,
            cycle["disposition"],
        )

    def test_recovery_leaves_the_historical_event_exactly_as_written(self) -> None:
        self.record_repair_cycle(repo_head=self.head())
        self.record_reopen()
        before = self.event_path.read_text(encoding="utf-8")

        self.assertIn("repo_head", self.recovered())

        self.assertEqual(before, self.event_path.read_text(encoding="utf-8"))
        reopened = [
            entry["payload"]
            for entry in read_jsonl(self.event_path)
            if entry.get("name") == events.REVIEW_REPAIR_REOPENED_EVENT
        ]
        self.assertEqual([None], [record.get("repo_head") for record in reopened])


if __name__ == "__main__":  # pragma: no cover - convenience entry point
    unittest.main()
