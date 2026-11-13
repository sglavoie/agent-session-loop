"""Repair-child commit preflight across real in-run transitions.

Both cases drive ``python -m agent_session_loop run`` end to end, because the
defects they pin lived in the seam between two loop passes rather than in any
one function:

* ASI-170: the plan directory is tracked inside the repository, so the review
  turn and the repair materialization leave plan artifacts dirty, and nothing
  commits after a review turn. The first repair child's preflight refused the
  very artifacts that created it.
* ASI-168: a repair child's first turn retains work and asks a runner
  question; the loop absorbs the answer and continues the same child, and the
  preflight that relaunch runs refused the child's own retained work.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import unittest
from typing import Any
from unittest import mock

import tests.reply_flow_support as rfs
from agent_session_loop import (
    control_queue,
    events,
    mailbox_vocabulary,
    plans_metadata,
    plans_snapshot,
    review_cycle,
    review_repair_validation,
    run_persistence,
    session_verification,
    state,
)
from tests.reply_flow_support import LOOP_RUN_TIMEOUT_SECONDS
from tests.review_cycle_support import RepairCycleFixtures
from tests.support import (
    _structured_review_finding,
    git_stdout,
    read_jsonl,
    write_completed_review_plan,
)

ASL_TEST_LANE = "integration"


class ReviewToRepairPlanArtifactTests(RepairCycleFixtures, unittest.TestCase):
    """ASI-170: plan artifacts a review cycle leaves do not block its children."""

    def setUp(self) -> None:
        super().setUp()
        # Tracked inside the repository the run commits in, which is what
        # turns every plan artifact the review cycle writes into repository
        # dirt the repair child's preflight inspects.
        self.plan_dir = self.repo_root / "docs" / "plan"
        self._write_plan()
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")
        plans_metadata.save_plan_metadata(self.plan_dir, self.repo_root)
        for argv in (["git", "add", "-A"], ["git", "commit", "-q", "-m", "seed"]):
            subprocess.run(argv, cwd=self.repo_root, capture_output=True, check=True)

    def test_first_repair_child_runs_on_the_plan_artifacts_its_review_left(
        self,
    ) -> None:
        completed, payload = self._run_cycles(
            review_outcomes="findings,all-clear",
            extra_args=["--commit-per-session"],
        )

        self.assertNotEqual(
            "commit-preflight", payload.get("pause_reason"), payload["pause_message"]
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("all-complete", payload["reason"])
        self.assertEqual(
            ["1", "2", "2.1", "3"],
            [turn["session_id"] for turn in payload["turns"]],
        )
        (preflight,) = payload["repair_commit_preflights"]
        sanctioned = preflight["provenance"]["sanctioned_dirty_paths"]
        # Exactly the review cycle's own plan artifacts, nothing outside the
        # plan directory, and every one of them still holding what the run
        # recorded.
        self.assertEqual([], preflight["provenance"]["content_drifted_dirty_paths"])
        self.assertTrue(sanctioned)
        self.assertTrue(all(path.startswith("docs/plan/") for path in sanctioned))
        for name in (
            "final-review.md",
            "final-review.json",
            "session-2.md",
            "session-2.1.md",
            "manifest.md",
        ):
            self.assertIn(f"docs/plan/{name}", sanctioned)
        self.assertEqual(
            sorted(sanctioned),
            sorted(payload["repair_provenance"]["review-cycle"]),
        )
        # The child's own commit took the artifacts with it, so the next
        # review started from a committed tree rather than inheriting them.
        log = git_stdout(self.repo_root, "log", "--format=%s")
        self.assertGreaterEqual(len(log.splitlines()), 3)


class RepairChildReplyContinuationTests(rfs.EndToEndReplyResumeCase):
    """ASI-168: a child continued after a reply keeps its own retained work."""

    MUTATION_PATH = "retained-by-child.txt"

    def setUp(self) -> None:
        super().setUp()
        finding = _structured_review_finding()
        write_completed_review_plan(self.plan_dir, [finding])
        self.event_path = (
            state.plan_state_dir(self.plan_dir, self.state_dir)
            / events.EVENTS_FILE_NAME
        )
        self.event_path.parent.mkdir(parents=True, exist_ok=True)
        with mock.patch.object(
            review_repair_validation,
            "_make_children_executable",
            return_value={
                "loop_ready": True,
                "blockers": [],
                "non_bypassable_blockers": [],
                "baseline_summary": None,
            },
        ):
            action, _after = review_cycle._advance_review_cycle(
                event_path=self.event_path,
                payload={"run_id": "seed", "run_dir": str(self.tmp_path / "seed")},
                persistence=run_persistence._RunPersistence(),
                args=argparse.Namespace(
                    state_dir=str(self.state_dir), allow_not_loop_ready=True
                ),
                cwd=self.repo_root,
                session_id="2",
                session_label="session 2",
                session_details={"session": "2"},
                snapshot=plans_snapshot.inspect_plan(self.plan_dir),
                turn_payload={
                    "review_verdict": {
                        "passed": True,
                        "outcome": "findings",
                        "total_findings": 1,
                        "findings": [finding],
                    }
                },
                max_review_fix_cycles=2,
                plan_cache=plans_snapshot.PlanSnapshotCache(),
                reinspect=session_verification._reinspect_after_plan_mutation,
            )
        self.assertEqual("continue", action)
        self.child = self.plan_dir / "session-2.1.md"
        self.assertIn("- **Status**: pending", self.child.read_text(encoding="utf-8"))

    def _base_env(self, mode: str) -> dict[str, str]:
        env = super()._base_env(mode)
        # Every turn of the child writes the same bytes to one repository
        # path, so the first turn leaves it dirty and the continuation finds
        # exactly what that turn left.
        env["FAKE_CODEX_MUTATION_PATH"] = self.MUTATION_PATH
        return env

    def _records(self) -> list[dict[str, Any]]:
        return read_jsonl(self.event_path)

    def test_reply_continuation_keeps_the_childs_clean_baseline(self) -> None:
        process = self._start_live_mailbox_run()
        question_id = self._wait_for_runner_question()
        self.assertTrue((self.repo_root / self.MUTATION_PATH).is_file())
        control_queue.append_command(
            self.plan_dir,
            control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT,
            state_dir=self.state_dir,
        )
        self._resolve_live_question(
            question_id,
            action=mailbox_vocabulary.RESOLUTION_ACTION_CONTINUE,
            note="carry on",
        )
        stdout, stderr = process.communicate(timeout=LOOP_RUN_TIMEOUT_SECONDS)
        payload = json.loads(stdout)

        self.assertNotEqual(
            "commit-preflight", payload.get("pause_reason"), payload["pause_message"]
        )
        self.assertEqual(0, process.returncode, stderr or stdout)
        self.assertEqual(
            ["2.1", "2.1"], [turn["session_id"] for turn in payload["turns"]]
        )
        self.assertIn("- **Status**: complete", self.child.read_text(encoding="utf-8"))
        first, second = payload["repair_commit_preflights"]
        self.assertTrue(first["repositories"][0]["clean"])
        self.assertNotIn("provenance", first)
        # The relaunch found the first turn's retained path, proven by content
        # rather than by name, and nothing else.
        self.assertEqual(
            [self.MUTATION_PATH], second["provenance"]["sanctioned_dirty_paths"]
        )
        self.assertEqual([], second["provenance"]["content_drifted_dirty_paths"])
        # The child's own commit settled the retained work.
        self.assertEqual(
            "",
            git_stdout(
                self.repo_root, "status", "--porcelain", "--untracked-files=all"
            ),
        )
        self.assertNotIn(
            "commit-preflight",
            [
                record["payload"].get("reason")
                for record in self._records()
                if record["name"] == events.RUN_PAUSED_EVENT
            ],
        )

    def test_an_edit_during_the_reply_wait_still_refuses_the_relaunch(self) -> None:
        process = self._start_live_mailbox_run()
        question_id = self._wait_for_runner_question()
        # Someone rewrites the retained path while the run waits for the
        # answer: same name, different bytes, so it is no longer the child's.
        (self.repo_root / self.MUTATION_PATH).write_text(
            "operator edit\n", encoding="utf-8"
        )
        self._resolve_live_question(
            question_id,
            action=mailbox_vocabulary.RESOLUTION_ACTION_CONTINUE,
            note="carry on",
        )
        stdout, _stderr = process.communicate(timeout=LOOP_RUN_TIMEOUT_SECONDS)
        payload = json.loads(stdout)

        self.assertEqual("commit-preflight", payload["pause_reason"])
        self.assertIn(
            "content changed after the run recorded it", payload["pause_message"]
        )
        self.assertEqual(
            [self.MUTATION_PATH],
            payload["pause_details"]["provenance"]["content_drifted_dirty_paths"],
        )
        self.assertEqual(["2.1"], [turn["session_id"] for turn in payload["turns"]])
        self.assertIn("- **Status**: pending", self.child.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
