"""Handoff correction and paused handoff status run tests."""

from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    events,
    handoff_correction,
    harnesses,
    plans,
    review,
    run_loop,
    state,
    state_locks,
    verification,
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


class HandoffCorrectionTests(unittest.TestCase):
    """A mechanically malformed handoff gets one bounded correction turn."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.workspace_root = self.tmp_path / "workspace"
        self.repositories = init_multi_repo_workspace(
            self.workspace_root,
            DEFAULT_MULTI_REPO_SPECS[:2],
        )
        self.plan_dir = self.tmp_path / "plan"
        self.state_dir = self.tmp_path / "state"
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
        registry = plans.read_repository_registry(self.plan_dir)
        assert registry is not None
        plans.save_workspace_plan_metadata(
            self.plan_dir,
            self.workspace_root,
            registry.digest,
        )
        self.context = verification.VerificationContext.from_plan(
            self.plan_dir,
            self.workspace_root,
        )

    def _args(self) -> argparse.Namespace:
        return argparse.Namespace(
            plan_dir=str(self.plan_dir),
            cwd=str(self.workspace_root),
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
        )

    def _invoke(self, run_id: str) -> tuple[int, dict[str, Any]]:
        lock_info = state_locks.acquire_lock(self.plan_dir, self.state_dir)
        try:
            return run_loop.run_locked(
                args=self._args(),
                cwd=self.workspace_root,
                timeout=None,
                max_sessions=None,
                commit_mode=None,
                lock_info=lock_info,
                run_id=run_id,
                verification_context=self.context,
            )
        finally:
            state_locks.release_lock(self.plan_dir, self.state_dir)

    def _records(self) -> list[dict[str, Any]]:
        event_path = (
            state.plan_state_dir(self.plan_dir, self.state_dir)
            / events.EVENTS_FILE_NAME
        )
        return read_jsonl(event_path)

    def _payloads(self, name: str) -> list[dict[str, Any]]:
        return [
            record["payload"] for record in self._records() if record["name"] == name
        ]

    def _complete_with(self, files_modified: str) -> None:
        """Mark the session complete with an arbitrary Files modified line."""
        (self.repositories["SVC"] / "README.md").write_text(
            f"changed by the harness: {files_modified}\n", encoding="utf-8"
        )
        session_path = self.plan_dir / "session-1.md"
        lines = session_path.read_text(encoding="utf-8").splitlines(keepends=True)
        for index, line in enumerate(lines):
            if line.startswith(plans.STATUS_PREFIX):
                lines[index] = f"{plans.STATUS_PREFIX} complete\n"
            elif line.startswith(plans.HANDOFF_FILES_PREFIX):
                lines[index] = f"{plans.HANDOFF_FILES_PREFIX} {files_modified}\n"
        session_path.write_text("".join(lines), encoding="utf-8")

    def _harness(self, *files_lines: str):
        """Return a ``run_harness`` stand-in writing one handoff per turn."""
        pending = list(files_lines)

        def run(*_args: object, **_kwargs: object) -> harnesses.HarnessResult:
            self._complete_with(pending.pop(0) if pending else "(none)")
            return harnesses.HarnessResult(
                name="codex",
                argv=["fake"],
                returncode=0,
                stdout="ok\n",
                stderr="",
            )

        return run

    def test_unqualified_entry_is_corrected_within_one_extra_turn(self) -> None:
        with mock.patch.object(
            harnesses,
            "run_harness",
            side_effect=self._harness("README.md", "SVC:README.md"),
        ):
            exit_code, payload = self._invoke("correction-run")

        self.assertEqual(0, exit_code)
        self.assertFalse(payload["paused"])
        self.assertEqual("all-complete", payload["reason"])
        corrections = self._payloads(events.TURN_HANDOFF_CORRECTION_EVENT)
        self.assertEqual(1, len(corrections))
        self.assertEqual(
            handoff_correction.HANDOFF_DEFECT_UNQUALIFIED_ENTRY,
            corrections[0]["defect"],
        )
        self.assertEqual(1, corrections[0]["attempt"])
        self.assertEqual(
            handoff_correction.MAX_HANDOFF_CORRECTION_ATTEMPTS,
            corrections[0]["max_attempts"],
        )
        self.assertFalse(corrections[0]["exhausted"])
        self.assertIn("README.md", corrections[0]["detail"])
        self.assertEqual([], self._payloads(events.RUN_PAUSED_EVENT))

    def test_execution_event_precedes_each_accepted_turn(self) -> None:
        with mock.patch.object(
            harnesses,
            "run_harness",
            side_effect=self._harness("README.md", "SVC:README.md"),
        ):
            self._invoke("ordering-run")

        names = [
            record["name"]
            for record in self._records()
            if record["name"]
            in (events.TURN_EXECUTION_FINISHED_EVENT, events.TURN_FINISHED_EVENT)
        ]
        # Both turns ran, but the first one's handoff was malformed and was
        # sent back for correction, so only the second was ever accepted.
        self.assertEqual(
            [
                events.TURN_EXECUTION_FINISHED_EVENT,
                events.TURN_EXECUTION_FINISHED_EVENT,
                events.TURN_FINISHED_EVENT,
            ],
            names,
        )
        executions = self._payloads(events.TURN_EXECUTION_FINISHED_EVENT)
        self.assertFalse(executions[0]["plan_readable"])
        self.assertIn("session-1.md", executions[0]["plan_error"])
        self.assertTrue(executions[1]["plan_readable"])
        for execution in executions:
            self.assertEqual(0, execution["returncode"])
            self.assertTrue(Path(execution["result_path"]).is_file())

    def test_repeated_defect_exhausts_the_correction_and_pauses(self) -> None:
        with mock.patch.object(
            harnesses,
            "run_harness",
            side_effect=self._harness("README.md", "README.md"),
        ):
            exit_code, payload = self._invoke("exhausted-run")

        self.assertNotEqual(0, exit_code)
        self.assertTrue(payload["paused"])
        self.assertEqual("malformed-plan", payload["pause_reason"])
        evidence = payload["pause_details"]["handoff_correction"]
        self.assertTrue(evidence["exhausted"])
        self.assertEqual(2, evidence["attempt"])
        self.assertEqual(
            handoff_correction.HANDOFF_DEFECT_UNQUALIFIED_ENTRY, evidence["defect"]
        )
        self.assertEqual(1, len(self._payloads(events.TURN_HANDOFF_CORRECTION_EVENT)))
        # The session is still reopened so the plan parses, but a defect the
        # bounded correction already failed on is not advertised as repairable.
        reopen = payload["pause_details"]["handoff_reopen"]
        self.assertTrue(reopen["reopened"])
        self.assertFalse(reopen["repairable"])

    def test_scope_violation_is_not_treated_as_a_correctable_defect(self) -> None:
        with mock.patch.object(
            harnesses,
            "run_harness",
            side_effect=self._harness("OAK:README.md"),
        ) as run_harness:
            exit_code, payload = self._invoke("scope-run")

        self.assertNotEqual(0, exit_code)
        self.assertTrue(payload["paused"])
        self.assertEqual("malformed-plan", payload["pause_reason"])
        self.assertNotIn("handoff_correction", payload["pause_details"])
        self.assertEqual([], self._payloads(events.TURN_HANDOFF_CORRECTION_EVENT))
        self.assertEqual(1, run_harness.call_count)

    def test_uninspectable_completed_handoff_is_reopened_as_repairable(self) -> None:
        with mock.patch.object(
            harnesses,
            "run_harness",
            side_effect=self._harness("OAK:README.md"),
        ):
            _, payload = self._invoke("reopen-run")

        self.assertEqual("malformed-plan", payload["pause_reason"])
        evidence = payload["pause_details"]["handoff_reopen"]
        self.assertTrue(evidence["reopened"])
        self.assertTrue(evidence["repairable"])
        self.assertIn("session-1.md", evidence["plan_error"])

        # The plan parses again, and it claims the work still has to be handed
        # off, so a relaunch has something to select.
        session_text = (self.plan_dir / "session-1.md").read_text(encoding="utf-8")
        self.assertIn(f"{plans.STATUS_PREFIX} pending", session_text)
        self.assertIn("The status was reset to pending", session_text)
        self.assertIn("OAK:README.md", session_text)
        snapshot = plans.inspect_plan(self.plan_dir)
        self.assertEqual(["pending"], [item.status for item in snapshot.sessions])
        self.assertEqual(
            ["pending"],
            [item["status"] for item in payload["final_snapshot"]["sessions"]],
        )

        rejections = self._payloads(events.TURN_REJECTED_EVENT)
        self.assertEqual(1, len(rejections))
        self.assertTrue(rejections[0]["reopened"])
        reopenings = self._payloads(events.SESSION_REOPENED_EVENT)
        self.assertEqual(1, len(reopenings))
        self.assertEqual("complete", reopenings[0]["from_status"])
        self.assertEqual("pending", reopenings[0]["to_status"])
        self.assertEqual("plan-inspection", reopenings[0]["guard"])
        # Replaying the stream lands on the projection the run reports, not on
        # a state the consumer has to re-read the plan to confirm.
        self.assertEqual(snapshot.content_sha256, reopenings[0]["content_sha256"])
        self.assertEqual(payload["final_snapshot"]["counts"], reopenings[0]["counts"])

    def test_correction_turn_preserves_review_artifacts(self) -> None:
        (self.plan_dir / review.FINAL_REVIEW_FILENAME).write_text(
            f"# Final review\n\n{review.REVIEW_STATUS_ALL_CLEAR}\n", encoding="utf-8"
        )
        (self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
            json.dumps(
                {
                    "version": review.REVIEW_ARTIFACT_VERSION,
                    "outcome": "all-clear",
                    "findings": [],
                }
            ),
            encoding="utf-8",
        )
        digest = review.final_review_artifact_sha256(self.plan_dir)

        with mock.patch.object(
            harnesses,
            "run_harness",
            side_effect=self._harness("README.md", "SVC:README.md"),
        ):
            self._invoke("preserved-run")

        corrections = self._payloads(events.TURN_HANDOFF_CORRECTION_EVENT)
        preserved = corrections[0]["preserved_review_artifacts"]
        self.assertEqual(digest, preserved["review_artifact_sha256"])
        self.assertTrue(preserved["review_verdict_ok"])
        self.assertEqual("all-clear", preserved["review_outcome"])
        self.assertEqual(digest, review.final_review_artifact_sha256(self.plan_dir))


class PausedHandoffStatusTests(unittest.TestCase):
    """A structured runner pause outranks the status value it was written with."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.repo_root = self.tmp_path / "repo"
        init_git_repo(self.repo_root)
        self.plan_dir = self.tmp_path / "plan"
        self.state_dir = self.tmp_path / "state"
        write_plan(self.plan_dir, ("pending",))

    def _args(self) -> argparse.Namespace:
        return argparse.Namespace(
            plan_dir=str(self.plan_dir),
            cwd=str(self.repo_root),
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
        )

    def _invoke(self, run_id: str) -> tuple[int, dict[str, Any]]:
        lock_info = state_locks.acquire_lock(self.plan_dir, self.state_dir)
        try:
            return run_loop.run_locked(
                args=self._args(),
                cwd=self.repo_root,
                timeout=None,
                max_sessions=None,
                commit_mode=None,
                lock_info=lock_info,
                run_id=run_id,
            )
        finally:
            state_locks.release_lock(self.plan_dir, self.state_dir)

    def _payloads(self, name: str) -> list[dict[str, Any]]:
        event_path = (
            state.plan_state_dir(self.plan_dir, self.state_dir)
            / events.EVENTS_FILE_NAME
        )
        return [
            record["payload"]
            for record in read_jsonl(event_path)
            if record["name"] == name
        ]

    def _harness(self, status: str, last_message: str):
        def run(*_args: object, **_kwargs: object) -> harnesses.HarnessResult:
            session_path = self.plan_dir / "session-1.md"
            lines = session_path.read_text(encoding="utf-8").splitlines(keepends=True)
            for index, line in enumerate(lines):
                if line.startswith(plans.STATUS_PREFIX):
                    lines[index] = f"{plans.STATUS_PREFIX} {status}\n"
            session_path.write_text("".join(lines), encoding="utf-8")
            return harnesses.HarnessResult(
                name="codex",
                argv=["fake"],
                returncode=0,
                stdout="ok\n",
                stderr="",
                last_message=last_message,
            )

        return run

    def test_structured_pause_outranks_a_paused_handoff_status(self) -> None:
        with mock.patch.object(
            harnesses,
            "run_harness",
            side_effect=self._harness(
                "paused",
                "Runner status: paused\nRUNNER_PAUSE: blocked - needs a decision\n",
            ),
        ):
            _, payload = self._invoke("paused-status-run")

        self.assertTrue(payload["paused"])
        self.assertEqual("blocked", payload["pause_reason"])
        repair = payload["pause_details"]["handoff_status_repair"]
        self.assertEqual("paused", repair["from_status"])
        self.assertEqual("pending", repair["to_status"])
        self.assertEqual("blocked", repair["pause_reason"])

        session_text = (self.plan_dir / "session-1.md").read_text(encoding="utf-8")
        self.assertIn(f"{plans.STATUS_PREFIX} pending", session_text)
        self.assertIn("there is no paused status value", session_text)
        self.assertEqual(
            "pending", plans.inspect_plan(self.plan_dir).sessions[0].status
        )
        self.assertEqual(
            ["pending"],
            [item["status"] for item in payload["final_snapshot"]["sessions"]],
        )

        reopenings = self._payloads(events.SESSION_REOPENED_EVENT)
        self.assertEqual(1, len(reopenings))
        self.assertEqual("paused", reopenings[0]["from_status"])
        self.assertEqual("pending", reopenings[0]["to_status"])
        self.assertEqual("plan-status-validity", reopenings[0]["guard"])
        self.assertEqual("blocked", reopenings[0]["reason"])
        self.assertEqual(
            plans.inspect_plan(self.plan_dir).content_sha256,
            reopenings[0]["content_sha256"],
        )
        self.assertEqual(payload["final_snapshot"]["counts"], reopenings[0]["counts"])

    def test_unstructured_pause_leaves_an_invalid_status_terminal(self) -> None:
        with mock.patch.object(
            harnesses,
            "run_harness",
            side_effect=self._harness(
                "paused",
                "RUNNER_PAUSE: wrangler auth expired\n",
            ),
        ):
            _, payload = self._invoke("unstructured-pause-run")

        self.assertTrue(payload["paused"])
        self.assertEqual("unknown-status", payload["pause_reason"])
        self.assertNotIn("handoff_status_repair", payload["pause_details"])
        self.assertIn(
            f"{plans.STATUS_PREFIX} paused",
            (self.plan_dir / "session-1.md").read_text(encoding="utf-8"),
        )
        self.assertEqual([], self._payloads(events.SESSION_REOPENED_EVENT))


if __name__ == "__main__":
    unittest.main()
