"""Handoff-correction tests: correction."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    events,
    handoff_correction,
    plans_errors,
    plans_handoff,
    plans_models,
    plans_snapshot,
    review,
    run_persistence,
)
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    read_jsonl,
    write_multi_repo_plan,
)

from .asl_handoff_correction_support import (
    resolved_workspace,
    set_handoff,
)


class CorrectionTurnTests(unittest.TestCase):
    """One malformed handoff, driven through a real bounded correction turn."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name).resolve()
        self.plan_dir = root / "plan"
        write_multi_repo_plan(self.plan_dir, ("pending", "pending"))
        # Review artifacts with their own validator, written beside the plan.
        # They are what the correction turn must leave alone.
        (self.plan_dir / review.FINAL_REVIEW_FILENAME).write_text(
            f"# Final review\n\n{review.REVIEW_STATUS_ALL_CLEAR}\n", encoding="utf-8"
        )
        (self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
            json.dumps({"version": 2, "outcome": "all-clear", "findings": []}),
            encoding="utf-8",
        )
        self.session_path = self.plan_dir / "session-1.md"
        self.before = plans_snapshot.inspect_plan(self.plan_dir)
        self.event_path = root / "events.jsonl"
        self.result_path = root / "result.json"
        self.persistence = run_persistence._RunPersistence(write_state=False)
        self.plan_cache = plans_snapshot.PlanSnapshotCache()
        self.workspace = resolved_workspace(root)
        self.payload: dict[str, Any] = {"run_id": "run-1"}
        self.turn_payload: dict[str, Any] = {}

    def correct(self) -> tuple[plans_models.PlanSnapshot | None, dict[str, Any] | None]:
        return handoff_correction._correct_malformed_handoff(
            event_path=self.event_path,
            payload=self.payload,
            persistence=self.persistence,
            plan_cache=self.plan_cache,
            before=self.before,
            workspace=self.workspace,
            run_id="run-1",
            turn_index=3,
            session_id="1",
            plan_error=f"{self.session_path.name}: unqualified path 'README.md'",
            turn_payload=self.turn_payload,
            paths={"result": self.result_path},
        )

    def malform(self) -> None:
        set_handoff(self.session_path, status="complete", files_modified="README.md")

    def artifact_digests(self) -> tuple[str, str]:
        return (
            review.final_review_output_sha256(self.plan_dir),
            review.final_review_artifact_sha256(self.plan_dir),
        )

    def test_the_correction_turn_preserves_the_review_artifacts(self) -> None:
        self.malform()
        before_digests = self.artifact_digests()

        corrected, evidence = self.correct()

        assert corrected is not None
        assert evidence is not None
        self.assertFalse(evidence["exhausted"])
        preserved = evidence["preserved_review_artifacts"]
        # The digests were taken before the session was reverted, and both
        # artifacts are byte-for-byte what they were: the handoff is what
        # failed to parse, and the artifacts have their own validator.
        self.assertEqual(before_digests[0], preserved["review_output_sha256"])
        self.assertEqual(before_digests[1], preserved["review_artifact_sha256"])
        self.assertEqual(before_digests, self.artifact_digests())
        self.assertTrue(preserved["review_verdict_ok"])
        self.assertEqual("all-clear", preserved["review_outcome"])

    def test_the_correction_note_tells_the_next_turn_to_leave_them_alone(
        self,
    ) -> None:
        self.malform()

        self.correct()

        note = self.session_path.read_text(encoding="utf-8")
        self.assertIn("- **Status**: pending", note)
        self.assertIn("handoff correction required", note)
        self.assertIn("leave them exactly as they are", note)
        # The claim is withdrawn; the work is not touched.
        self.assertIn("the session's work is already done", note)

    def test_the_correction_records_its_evidence_and_arms_one_turn(self) -> None:
        self.malform()

        corrected, evidence = self.correct()

        assert corrected is not None
        assert evidence is not None
        self.assertEqual(1, evidence["attempt"])
        self.assertEqual(
            handoff_correction.MAX_HANDOFF_CORRECTION_ATTEMPTS,
            evidence["max_attempts"],
        )
        self.assertEqual(
            handoff_correction.HANDOFF_DEFECT_UNQUALIFIED_ENTRY, evidence["defect"]
        )
        self.assertEqual(str(self.session_path), evidence["session_path"])
        self.assertEqual(
            evidence,
            json.loads(self.result_path.read_text(encoding="utf-8"))[
                "handoff_correction"
            ],
        )
        recorded = [
            record
            for record in read_jsonl(self.event_path)
            if record["name"] == events.TURN_HANDOFF_CORRECTION_EVENT
        ]
        self.assertEqual(1, len(recorded))
        self.assertEqual(evidence, recorded[0]["payload"])
        # The re-read plan is what the loop publishes, and it parses again.
        self.assertIn("final_snapshot", self.payload)
        self.assertEqual(
            "pending",
            next(item.status for item in corrected.sessions if item.id == "1"),
        )

    def test_plan_artifact_correction_preserves_repository_validation(self) -> None:
        session_text = self.session_path.read_text(encoding="utf-8")
        aliases = ", ".join(alias for alias, _path, _origin in DEFAULT_MULTI_REPO_SPECS)
        for repository_entry in (None, "SVC:README.md", "OAK:README.md"):
            with self.subTest(repository_entry=repository_entry):
                writes = "SVC" if repository_entry else "(none)"
                self.session_path.write_text(
                    session_text.replace(
                        f"**Writes repositories**: {aliases}",
                        f"**Writes repositories**: {writes}",
                    ),
                    encoding="utf-8",
                )
                self.before = plans_snapshot.inspect_plan(self.plan_dir)
                # Authored review sessions do not have the generated-review
                # marker that projects these artifact names away on read.
                inline = "final-review.md, final-review.json"
                if repository_entry:
                    inline += f", {repository_entry}"
                set_handoff(self.session_path, status="complete", files_modified=inline)
                if self.event_path.exists():
                    self.event_path.unlink()
                before_digests = self.artifact_digests()
                with self.assertRaises(plans_errors.PlanError):
                    plans_snapshot.inspect_plan(self.plan_dir)

                corrected, evidence = self.correct()

                self.assertIsNotNone(corrected)
                assert evidence is not None
                self.assertFalse(evidence["exhausted"])
                note = self.session_path.read_text(encoding="utf-8")
                self.assertIn("move its provenance to Notes", note)
                self.assertIn("Files modified: (none)", note)
                self.assertIn("retain every such file as an Alias:path entry", note)

                # Apply the documented representation: only the repository
                # entry remains, with artifact provenance in the handoff notes.
                set_handoff(
                    self.session_path,
                    status="complete",
                    files_modified=repository_entry or "(none)",
                )
                self.session_path.write_text(
                    self.session_path.read_text(encoding="utf-8").replace(
                        "- **Notes**:",
                        "- **Notes**: Plan-local artifacts: "
                        "final-review.md, final-review.json.",
                        1,
                    ),
                    encoding="utf-8",
                )
                if repository_entry == "OAK:README.md":
                    with self.assertRaisesRegex(
                        plans_errors.PlanError, "outside Writes repositories"
                    ):
                        plans_snapshot.inspect_plan(self.plan_dir)
                else:
                    snapshot = plans_snapshot.inspect_plan(self.plan_dir)
                    handoff = plans_handoff.read_session_handoff(
                        snapshot.sessions[0].session
                    )
                    self.assertEqual("complete", handoff.status)
                    self.assertEqual(
                        (repository_entry or "(none)",), handoff.files_modified
                    )
                    self.assertIn("Plan-local artifacts:", handoff.notes or "")
                self.assertEqual(before_digests, self.artifact_digests())

    def test_a_second_correction_is_refused_but_still_reports(self) -> None:
        self.malform()
        self.persistence.append_event(
            self.event_path,
            events.TURN_HANDOFF_CORRECTION_EVENT,
            {"session_id": "1", "run_id": "run-1"},
        )
        before_digests = self.artifact_digests()

        corrected, evidence = self.correct()

        self.assertIsNone(corrected)
        assert evidence is not None
        self.assertTrue(evidence["exhausted"])
        self.assertEqual(2, evidence["attempt"])
        # The exhausted case still carries the preserved-artifact evidence, so
        # the pause that follows says what was tried and what was left intact.
        self.assertIn("preserved_review_artifacts", evidence)
        self.assertEqual(before_digests, self.artifact_digests())
        self.assertIn("- **Status**: complete", self.session_path.read_text("utf-8"))

    def test_a_plan_error_naming_another_file_corrects_nothing(self) -> None:
        self.malform()

        corrected, evidence = handoff_correction._correct_malformed_handoff(
            event_path=self.event_path,
            payload=self.payload,
            persistence=self.persistence,
            plan_cache=self.plan_cache,
            before=self.before,
            workspace=self.workspace,
            run_id="run-1",
            turn_index=3,
            session_id="1",
            plan_error="manifest.md: registry row is malformed",
            turn_payload=self.turn_payload,
            paths={"result": self.result_path},
        )

        self.assertIsNone(corrected)
        self.assertIsNone(evidence)
        self.assertIn("- **Status**: complete", self.session_path.read_text("utf-8"))

    def test_a_legacy_run_without_a_workspace_corrects_nothing(self) -> None:
        self.malform()
        self.workspace = None  # type: ignore[assignment]

        corrected, evidence = self.correct()

        self.assertIsNone(corrected)
        self.assertIsNone(evidence)

    def test_the_correction_turn_spawns_no_process(self) -> None:
        # `PORTABLE_MODULES` membership is a claim about this boundary, made
        # here on evidence rather than by reading the imports.
        self.malform()
        boom = mock.Mock(side_effect=AssertionError("spawned a process"))
        with (
            mock.patch.object(subprocess, "Popen", boom),
            mock.patch.object(subprocess, "run", boom),
            mock.patch.object(subprocess, "check_output", boom),
            mock.patch.object(subprocess, "check_call", boom),
        ):
            corrected, evidence = self.correct()

        self.assertIsNotNone(corrected)
        self.assertIsNotNone(evidence)


class UninspectableReopenTests(unittest.TestCase):
    """The lane that covers everything the bounded correction does not."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name).resolve()
        self.plan_dir = root / "plan"
        write_multi_repo_plan(self.plan_dir, ("pending", "pending"))
        self.session_path = self.plan_dir / "session-1.md"
        self.before = plans_snapshot.inspect_plan(self.plan_dir)
        self.plan_cache = plans_snapshot.PlanSnapshotCache()

    def reopen(self, *, exhausted: bool) -> tuple[Any, dict[str, Any] | None]:
        return handoff_correction._reopen_uninspectable_handoff(
            plan_cache=self.plan_cache,
            before=self.before,
            session_id="1",
            plan_error=f"{self.session_path.name}: handoff is unreadable",
            correction_exhausted=exhausted,
        )

    def test_a_completed_handoff_is_returned_to_pending_with_the_reason(self) -> None:
        set_handoff(self.session_path, status="complete", files_modified="README.md")

        reinspected, evidence = self.reopen(exhausted=False)

        assert reinspected is not None
        assert evidence is not None
        self.assertTrue(evidence["reopened"])
        self.assertTrue(evidence["repairable"])
        text = self.session_path.read_text(encoding="utf-8")
        self.assertIn("- **Status**: pending", text)
        self.assertIn("the plan could not be inspected", text)

    def test_an_exhausted_correction_reopens_but_promises_nothing(self) -> None:
        set_handoff(self.session_path, status="complete", files_modified="README.md")

        _reinspected, evidence = self.reopen(exhausted=True)

        assert evidence is not None
        self.assertTrue(evidence["reopened"])
        self.assertFalse(evidence["repairable"])

    def test_a_session_that_never_claimed_complete_is_left_alone(self) -> None:
        original = self.session_path.read_text(encoding="utf-8")

        reinspected, evidence = self.reopen(exhausted=False)

        self.assertIsNone(reinspected)
        self.assertIsNone(evidence)
        self.assertEqual(original, self.session_path.read_text(encoding="utf-8"))
