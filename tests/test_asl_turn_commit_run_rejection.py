"""Full runs where a post-execution guard refuses the turn instead of committing it."""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    events,
    harnesses,
    plans_content_hash,
    plans_snapshot,
    review_guard,
    turn_execution,
    verification,
)
from tests.asl_turn_commit_support import TurnCommitRunCase
from tests.support import (
    fake_commit_harness_env,
    read_jsonl,
)


class TurnCommitRejectionTests(TurnCommitRunCase):
    """Evidence recording and guard rejections around the commit."""

    def test_ignored_evidence_is_recorded_but_never_staged(self) -> None:
        self._mark_repair_session()
        repo = self.repositories["SVC"]
        # A `.gitignore` naming itself is invisible to the untracked scan, so
        # rewriting it leaves `worktree_ignore` as its only evidence; the same
        # goes for `.git/info/exclude`, which no worktree scan reaches at all.
        ignore_path = repo / ".gitignore"
        ignore_path.write_text(".gitignore\ncache/\n", encoding="utf-8")
        exclude_path = repo / ".git" / "info" / "exclude"
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        exclude_path.write_text("# baseline\n", encoding="utf-8")
        self.assertEqual(
            "", review_guard._run_git(repo, ["status", "--porcelain"]).stdout.strip()
        )
        commit_log = self.tmp_path / "commit-turns.jsonl"
        env = fake_commit_harness_env(self.tmp_path, log_path=commit_log)
        complete_session = self._harness(
            writes="SVC:repair.txt", declares="SVC:repair.txt"
        )

        def run(*args: object, **kwargs: object) -> harnesses.HarnessResult:
            ignore_path.write_text(".gitignore\ncache/\nbuild/\n", encoding="utf-8")
            exclude_path.write_text("# baseline\n*.tmp\n", encoding="utf-8")
            return complete_session(*args, **kwargs)

        with (
            mock.patch.dict(os.environ, env),
            mock.patch.object(harnesses, "run_harness", side_effect=run),
        ):
            exit_code, payload = self._invoke("ignored-evidence-commit")

        self.assertEqual(0, exit_code)
        self.assertEqual(1, len(payload["commits"]))
        commit = payload["commits"][0]
        # The guard still reports both ignore sources — dropping them from the
        # evidence would let a turn hide behind the rules it writes.
        self.assertEqual(
            ["SVC:repair.txt", "SVC:.gitignore", "SVC:.git/info/exclude"],
            commit["qualified_paths"],
        )
        self.assertEqual(["SVC:repair.txt"], commit["staging_paths"])
        self.assertEqual(["--pathspec", "repair.txt"], commit["argv"][-2:])
        self.assertEqual(
            ["repair.txt"],
            review_guard._run_git(
                repo, ["show", "--name-only", "--format=", "HEAD"]
            ).stdout.split(),
        )
        self.assertEqual("success", commit["status_result"])
        self.assertEqual(
            ".gitignore\ncache/\nbuild/\n", ignore_path.read_text(encoding="utf-8")
        )
        self.assertEqual(
            [str(self.repositories["SVC"].resolve())],
            [record["cwd"] for record in read_jsonl(commit_log)],
        )

    def test_turn_finished_digest_matches_stored_result_json(self) -> None:
        payload = self._run_accepted()
        self.assertTrue(payload["readiness_state_active"])
        finished = self._payloads(events.TURN_FINISHED_EVENT)[0]
        stored = json.loads(
            Path(str(finished["result_path"])).read_text(encoding="utf-8")
        )
        self.assertEqual(stored["after"], finished["after"])
        self.assertEqual(
            plans_content_hash.plan_content_sha256(self.plan_dir),
            finished["after"]["content_sha256"],
        )
        self.assertEqual(finished["after"], payload["final_snapshot"])

    def test_scope_violation_is_rejected_and_reopened_without_acceptance(self) -> None:
        with mock.patch.object(
            harnesses,
            "run_harness",
            side_effect=self._harness(writes="OAK:README.md", declares="(none)"),
        ):
            exit_code, payload = self._invoke("rejected-run")

        self.assertNotEqual(0, exit_code)
        self.assertTrue(payload["paused"])
        names = self._names()
        self.assertIn(events.TURN_EXECUTION_FINISHED_EVENT, names)
        self.assertNotIn(events.TURN_FINISHED_EVENT, names)

        rejections = self._payloads(events.TURN_REJECTED_EVENT)
        self.assertEqual(1, len(rejections))
        self.assertEqual("workspace-write-guard", rejections[0]["guard"])
        self.assertEqual(payload["pause_reason"], rejections[0]["reason"])
        self.assertTrue(rejections[0]["reopened"])

        reopened = self._payloads(events.SESSION_REOPENED_EVENT)
        self.assertEqual(1, len(reopened))
        self.assertEqual("complete", reopened[0]["from_status"])
        self.assertEqual("pending", reopened[0]["to_status"])
        self.assertEqual("workspace-write-guard", reopened[0]["guard"])
        self.assertLess(
            names.index(events.TURN_REJECTED_EVENT),
            names.index(events.SESSION_REOPENED_EVENT),
        )
        self.assertEqual(
            "pending", plans_snapshot.inspect_plan(self.plan_dir).sessions[0].status
        )

    def test_unverifiable_turn_is_rejected_and_reopened(self) -> None:
        with (
            mock.patch.object(
                harnesses,
                "run_harness",
                side_effect=self._harness(
                    writes="SVC:README.md", declares="SVC:README.md"
                ),
            ),
            mock.patch.object(
                verification,
                "run_verification",
                side_effect=OSError("verifier unavailable"),
            ),
        ):
            exit_code, payload = self._invoke("verify-run")

        self.assertNotEqual(0, exit_code)
        self.assertEqual("verify-failed", payload["pause_reason"])
        self.assertNotIn(events.TURN_FINISHED_EVENT, self._names())
        rejections = self._payloads(events.TURN_REJECTED_EVENT)
        self.assertEqual(1, len(rejections))
        self.assertEqual("session-verification", rejections[0]["guard"])
        self.assertEqual("verify-failed", rejections[0]["reason"])
        self.assertIn("verifier unavailable", rejections[0]["detail"]["error"])
        self.assertTrue(rejections[0]["reopened"])
        self.assertEqual(1, len(self._payloads(events.SESSION_REOPENED_EVENT)))

    def test_publication_drift_pauses_instead_of_crashing_the_loop(self) -> None:
        rejection = turn_execution.TurnCommitRejected(
            reason="unexpected-status-change",
            message="session statuses moved",
            detail={"status_changes": []},
        )
        with (
            mock.patch.object(
                harnesses,
                "run_harness",
                side_effect=self._harness(
                    writes="SVC:README.md", declares="SVC:README.md"
                ),
            ),
            mock.patch.object(
                turn_execution, "publication_snapshot", side_effect=rejection
            ),
        ):
            exit_code, payload = self._invoke("drift-run")

        self.assertNotEqual(0, exit_code)
        self.assertEqual("unexpected-status-change", payload["pause_reason"])
        self.assertNotIn(events.TURN_FINISHED_EVENT, self._names())
        rejections = self._payloads(events.TURN_REJECTED_EVENT)
        self.assertEqual(1, len(rejections))
        self.assertEqual("ledger-sync", rejections[0]["guard"])
        self.assertEqual("unexpected-status-change", rejections[0]["reason"])

    def test_untouched_session_is_rejected_without_a_reopen(self) -> None:
        def idle(*_args: object, **_kwargs: object) -> harnesses.HarnessResult:
            return harnesses.HarnessResult(
                name="codex", argv=["fake"], returncode=0, stdout="", stderr=""
            )

        with mock.patch.object(harnesses, "run_harness", side_effect=idle):
            exit_code, payload = self._invoke("idle-run")

        self.assertNotEqual(0, exit_code)
        self.assertEqual("no-status-change", payload["pause_reason"])
        self.assertNotIn(events.TURN_FINISHED_EVENT, self._names())
        rejections = self._payloads(events.TURN_REJECTED_EVENT)
        self.assertEqual(1, len(rejections))
        self.assertEqual("status-change", rejections[0]["guard"])
        self.assertEqual("no-status-change", rejections[0]["reason"])
        self.assertFalse(rejections[0]["reopened"])
        self.assertEqual([], self._payloads(events.SESSION_REOPENED_EVENT))


if __name__ == "__main__":
    unittest.main()
