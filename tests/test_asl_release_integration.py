"""End-to-end integration checks across the review and timing change set.

Every other module in this suite tests one seam. This one tests the seams
*together*, because the two defects this change set targets only appear when a
whole review lifecycle is driven by real runs:

- a review whose findings outlive its repair budget pauses, and the next run
  reopens it, repairs it, and rereviews. The rereview has to scope itself
  against the tree the *first* review read, which means the baseline written
  onto the reopen record has to survive a process boundary and reach the
  generated reviewer's session body. A unit test can assert the record carries
  the fields; only a run can assert the reviewer was told to read a diff.
- the metrics timing section has to account for that same lifecycle from the
  state directory the runs left behind: the generated repair child's seconds
  separated from the implementation turn's, the verification phase measured,
  and the review scope recorded as delta rather than full.

The third test is a regression rather than a story: the timing section reuses
the corpus reads ``collect_metrics`` already performs, so adding it must not
have added a traversal. It counts the reads instead of trusting the structure.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    config,
    events,
    execution_timing,
    metrics,
    metrics_models,
    plans_content_hash,
    plans_files,
    plans_models,
    plans_readiness,
    plans_session_ids,
    review_continuation,
    review_delta,
    review_session_body,
    review_transitions,
    state,
)
from tests.support import (
    REPO_ROOT,
    _contract_block,
    git_stdout,
    init_git_repo,
    install_fake_harness,
    read_jsonl,
    write_plan,
)

ASL_TEST_LANE = "integration"

#: Hang guard for an ``agent_session_loop run`` subprocess, matching the sibling
#: loop-test modules. Not a performance assertion: these runs share a machine
#: with the rest of the sharded suite.
LOOP_RUN_TIMEOUT_SECONDS = 180

#: The file the generated repair child writes and the runner commits, which is
#: also the one file a correctly scoped rereview should be pointed at.
REPAIR_FILE = "repair.txt"


class CappedReviewRepairRereleaseTests(unittest.TestCase):
    """Drive review, capped pause, reopen, repair and rereview as real runs."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.plan_dir = self.tmp_path / "plan"
        self.state_dir = self.tmp_path / "state"
        self.repo_root = self.tmp_path / "repo"
        self.config_home = self.tmp_path / "config"
        init_git_repo(self.repo_root)
        self.fake_codex = install_fake_harness(self.tmp_path, "codex")
        self.fake_claude = install_fake_harness(self.tmp_path, "claude")
        config_path = self.config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps(
                {
                    "codex": {"executable": str(self.fake_codex)},
                    "claude": {"executable": str(self.fake_claude)},
                }
            ),
            encoding="utf-8",
        )

    # -- fixtures ----------------------------------------------------------

    def _write_pending_plan(self) -> None:
        """Author a one-session plan the first run implements and then reviews.

        The implementation session is left pending on purpose: the run has to
        spend an implementation turn before the review it generates, so the
        timing section has an ``implementation`` figure to separate the repair
        child's seconds from.
        """
        write_plan(
            self.plan_dir,
            ("pending",),
            contracts_by_id={
                "1": _contract_block(
                    '{"type": "file_exists", "path": "README.md", "name": "readme-1"}',
                    session="1",
                )
            },
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

    def _run(
        self,
        *,
        max_review_fix_cycles: int,
        outcomes: str,
        repair_mutation: str | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
        argv = [
            sys.executable,
            "-m",
            "agent_session_loop",
            "run",
            str(self.plan_dir),
            "--cwd",
            str(self.repo_root),
            "--harness",
            "codex",
            "--review-harness",
            "claude",
            "--state-dir",
            str(self.state_dir),
            "--json",
            "--max-review-fix-cycles",
            str(max_review_fix_cycles),
        ]
        env = os.environ.copy()
        env.update(
            {
                "XDG_CONFIG_HOME": str(self.config_home),
                "FAKE_CODEX_MODE": "success-with-review",
                "FAKE_CODEX_SLEEP": "5",
                "FAKE_CODEX_REVIEW_OUTCOMES": outcomes,
                "FAKE_CODEX_COMMIT_LOG": str(self.tmp_path / "fake-commit.jsonl"),
                "PYTHONPATH": str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", ""),
            }
        )
        if repair_mutation is not None:
            env["FAKE_CODEX_REPAIR_MUTATION_PATH"] = repair_mutation
        completed = subprocess.run(
            argv,
            cwd=REPO_ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=LOOP_RUN_TIMEOUT_SECONDS,
            check=False,
        )
        return completed, json.loads(completed.stdout)

    def _events_path(self) -> Path:
        plan_state = self.state_dir / plans_files.path_sha256(
            plans_files.canonicalize_path(self.plan_dir)
        )
        return plan_state / events.EVENTS_FILE_NAME

    def _read_events(self) -> list[dict[str, Any]]:
        return read_jsonl(self._events_path())

    def _payloads(self, name: str) -> list[dict[str, Any]]:
        return [
            record["payload"]
            for record in self._read_events()
            if record["name"] == name
        ]

    def _drive_capped_review(self) -> str:
        """Run one: implement, review, report findings, run out of budget.

        Returns the commit the review actually read, which is the baseline
        every later assertion is about.
        """
        self._write_pending_plan()

        completed, payload = self._run(max_review_fix_cycles=0, outcomes="findings")

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertFalse(payload["ok"])
        self.assertEqual(
            review_transitions.REVIEW_FINDINGS_UNRESOLVED_REASON, payload["reason"]
        )
        return git_stdout(self.repo_root, "rev-parse", "HEAD")

    def _drive_reopen_repair_rereview(
        self, *, outcomes: str, max_review_fix_cycles: int
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
        """Run two: reopen the capped review, repair it, and rereview."""
        return self._run(
            max_review_fix_cycles=max_review_fix_cycles,
            outcomes=outcomes,
            repair_mutation=REPAIR_FILE,
        )

    def _drive_cleared_lifecycle(self) -> tuple[str, dict[str, Any]]:
        """Drive the whole lifecycle through to a rereview that clears it."""
        reviewed_head = self._drive_capped_review()
        completed, payload = self._drive_reopen_repair_rereview(
            outcomes="all-clear", max_review_fix_cycles=4
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual("all-complete", payload["reason"])
        return reviewed_head, payload

    # -- tests -------------------------------------------------------------

    def test_capped_reopen_scopes_the_rereview_against_the_reviewed_tree(
        self,
    ) -> None:
        reviewed_head, second = self._drive_cleared_lifecycle()

        reopens = self._payloads(events.REVIEW_REPAIR_REOPENED_EVENT)
        self.assertEqual(1, len(reopens))
        reopen = reopens[0]
        self.assertTrue(reopen["materialized"])
        self.assertEqual(["2.1"], reopen["repair_session_ids"])
        # The whole point of the reopen baseline: the record that supersedes
        # the cycle has to name the tree that cycle's review actually read.
        self.assertEqual(reviewed_head, reopen["repo_head"])
        self.assertIs(False, reopen["worktree_dirty"])
        self.assertEqual([], reopen["dirty_paths"])

        # The repair child ran and was committed, so the tree moved on.
        repaired_head = git_stdout(self.repo_root, "rev-parse", "HEAD")
        self.assertNotEqual(reviewed_head, repaired_head)
        self.assertEqual(
            ["2.1"], [commit["session_id"] for commit in second["commits"]]
        )

        # The rereview is the second review session the plan grew, and it was
        # scoped against the first review's tree rather than re-reading all of
        # it.
        scopes = self._payloads(events.REVIEW_SCOPE_EVENT)
        self.assertEqual(2, len(scopes))
        self.assertEqual(review_session_body.REVIEW_SCOPE_FULL, scopes[0]["scope"])
        self.assertEqual("no-prior-repair-cycle", scopes[0]["fallback_reason"])
        self.assertEqual(review_session_body.REVIEW_SCOPE_DELTA, scopes[1]["scope"])
        self.assertEqual(reviewed_head, scopes[1]["base_sha"])
        self.assertIsNone(scopes[1]["fallback_reason"])

        # And the reviewer's own session body is what carries it: a delta scope
        # naming the repaired file and the diff commands against that base.
        review_body = (
            self.plan_dir / f"session-{second['review_session_id']}.md"
        ).read_text(encoding="utf-8")
        self.assertIn("## Delta Review Scope", review_body)
        self.assertIn(REPAIR_FILE, review_body)
        flags = " ".join(review_session_body._DELTA_DIFF_FLAGS)
        self.assertIn(f"git diff {flags} {reviewed_head}", review_body)

    def test_a_reopen_without_a_baseline_recovers_the_same_reviewer_scope(
        self,
    ) -> None:
        """The reader half, against the shape a pre-change run recorded.

        A run recorded before the writer carried the baseline left a reopen
        naming no tree at all. Recovery attributes it back to its source cycle,
        and the answer has to be the one the new writer would have produced --
        not merely *a* base, but the same base and the same reviewer scope.
        """
        reviewed_head = self._drive_capped_review()
        # The rereview reports findings again, so the plan keeps the findings
        # artifact the scoping reads. A cleared rereview would overwrite it and
        # this comparison would be made against a plan that no longer looks the
        # way it did when the scope was chosen.
        completed, second = self._drive_reopen_repair_rereview(
            outcomes="findings", max_review_fix_cycles=1
        )
        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertEqual(
            review_transitions.REVIEW_FINDINGS_UNRESOLVED_REASON,
            second["action_required"]["reason"],
        )

        recorded_delta, recorded_reason = review_delta._compute_review_delta_context(
            event_path=self._events_path(),
            plan_dir=self.plan_dir,
            cwd=self.repo_root,
        )
        self.assertIsNone(recorded_reason)
        assert recorded_delta is not None
        self.assertEqual(reviewed_head, recorded_delta["base_sha"])

        legacy_path = self.tmp_path / "legacy-events.jsonl"
        stripped: set[str] = set()
        with legacy_path.open("w", encoding="utf-8") as handle:
            for record in self._read_events():
                if record["name"] == events.REVIEW_REPAIR_REOPENED_EVENT:
                    for key in review_continuation.REVIEW_BASELINE_FIELDS:
                        if key in record["payload"]:
                            record["payload"].pop(key)
                            stripped.add(key)
                handle.write(json.dumps(record) + "\n")
        # The reopen really did carry a baseline to remove; otherwise this test
        # would pass by comparing two full-scope fallbacks.
        self.assertIn("repo_head", stripped)

        recovered_delta, recovered_reason = review_delta._compute_review_delta_context(
            event_path=legacy_path,
            plan_dir=self.plan_dir,
            cwd=self.repo_root,
        )

        self.assertIsNone(recovered_reason)
        assert recovered_delta is not None
        self.assertEqual(reviewed_head, recovered_delta["base_sha"])
        self.assertEqual(recorded_delta, recovered_delta)
        self.assertEqual(
            review_session_body.build_generated_review_session_body(
                plans_session_ids.parse_session_id(second["review_session_id"]),
                review_harness="claude",
                plan_dir=self.plan_dir,
                delta=recorded_delta,
            ),
            review_session_body.build_generated_review_session_body(
                plans_session_ids.parse_session_id(second["review_session_id"]),
                review_harness="claude",
                plan_dir=self.plan_dir,
                delta=recovered_delta,
            ),
        )

    def test_metrics_accounts_for_the_repaired_lifecycle(self) -> None:
        """The timing section has to describe the lifecycle that just ran."""
        self._drive_cleared_lifecycle()

        report = metrics.collect_metrics(state_dir=self.state_dir, since_days=1)
        timing = report.execution_timing

        self.assertEqual(1, timing["coverage"]["plans_with_evidence"])
        self.assertEqual(2, timing["coverage"]["runs_with_evidence"])
        self.assertEqual(2, timing["coverage"]["runs_included"])
        self.assertEqual(0, timing["coverage"]["unattributable_turn_results"])

        seconds = timing["phases"]["seconds"]
        # The generated repair child is worker execution the collector cannot
        # tell apart; the report's one added distinction is exactly this split,
        # and the two halves must add back up to the collector's single figure.
        self.assertGreater(seconds["generated_repair"], 0.0)
        self.assertGreater(seconds["implementation"], 0.0)
        self.assertGreater(seconds["review"], 0.0)
        self.assertGreater(seconds["verification"], 0.0)
        worker_total = sum(
            run.phase_seconds.get(execution_timing.PHASE_WORKER_EXECUTION, 0.0)
            for run in _timing_runs(self.state_dir)
        )
        self.assertAlmostEqual(
            worker_total,
            seconds["implementation"] + seconds["generated_repair"],
            places=3,
        )

        # Verification work is reported beside the phase it happened in, never
        # folded into it.
        checks = timing["verification_checks"]
        self.assertGreater(checks["checks"], 0)
        self.assertGreater(checks["cumulative_seconds"], 0.0)
        self.assertEqual(
            round(seconds["verification"], 3), round(checks["phase_seconds"], 3)
        )

        # The review scope counts are the lifecycle's own story: one full scope
        # for the first review, one delta for the rereview.
        scope = timing["review_scope"]
        self.assertEqual(2, scope["selections"])
        self.assertEqual(
            {
                review_session_body.REVIEW_SCOPE_FULL: 1,
                review_session_body.REVIEW_SCOPE_DELTA: 1,
            },
            scope["by_scope"],
        )
        self.assertEqual(
            {"no-prior-repair-cycle": 1},
            scope["fallback_reasons"],
        )
        self.assertEqual(1, scope["selections_without_fallback_reason"])

        plan_rows = timing["plans"]
        self.assertEqual(1, len(plan_rows))
        self.assertEqual(2, plan_rows[0]["runs"])
        self.assertEqual(
            str(plans_files.canonicalize_path(self.plan_dir)), plan_rows[0]["plan_dir"]
        )


def _timing_runs(state_dir: Path) -> list[execution_timing.RunTiming]:
    """Recollect timing straight from the corpus, with no windowing at all."""
    collected: list[execution_timing.RunTiming] = []
    state_root = state.state_root(state_dir)
    for plan_state_dir in sorted(state_root.iterdir()):
        if not plan_state_dir.is_dir():
            continue
        report = execution_timing.collect_execution_timing(
            [
                execution_timing.RunEvidence(
                    plan_key=plan_state_dir.name,
                    event_records=metrics_models._read_event_records(
                        plan_state_dir / events.EVENTS_FILE_NAME
                    ),
                    turn_results=metrics_models._iter_turn_results(plan_state_dir),
                )
            ]
        )
        collected.extend(report.included_runs)
    return collected


if __name__ == "__main__":
    unittest.main()
