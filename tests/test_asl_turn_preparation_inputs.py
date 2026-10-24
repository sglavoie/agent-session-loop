"""Asl turn preparation inputs regressions."""

from __future__ import annotations

import unittest
from typing import Any
from unittest import mock

from agent_session_loop import (
    events,
    plans_progress,
    review_guard,
    task_packet,
    turn_preparation,
)

from .support import patched_env
from .support_asl_turn_preparation import RUN_ID, FakeReviewResume, PreparationTestCase

ASL_TEST_PORTABLE = True


class PacketInputTests(PreparationTestCase):
    """The session-shaped facts every prompt shape is built from."""

    def collect(self, **overrides: Any) -> turn_preparation.PacketInputs:
        snapshot = overrides.pop("snapshot", None) or self.snapshot()
        kwargs: dict[str, Any] = {
            "classification": self.classification(),
            "args": self.args(),
            "session_id": "1",
            "mailbox_enabled": False,
        }
        kwargs.update(overrides)
        return turn_preparation.collect_packet_inputs(
            self.ctx, snapshot=snapshot, **kwargs
        )

    def test_the_worker_mode_is_read_from_the_environment(self) -> None:
        with patched_env(ASL_WORKER_MODE="skill"):
            self.assertEqual(task_packet.WORKER_MODE_SKILL, self.collect().worker_mode)

    def test_an_expansion_and_a_progress_file_are_detected(self) -> None:
        snapshot = self.snapshot()
        self.assertFalse(self.collect(snapshot=snapshot).has_expansion)
        self.assertFalse(self.collect(snapshot=snapshot).has_progress)

        (self.plan_dir / "session-1.expand.md").write_text("# x\n", encoding="utf-8")
        (self.plan_dir / plans_progress.PROGRESS_FILE_NAME).write_text(
            "# p\n", encoding="utf-8"
        )

        inputs = self.collect(snapshot=snapshot)
        self.assertTrue(inputs.has_expansion)
        self.assertTrue(inputs.has_progress)

    def test_verdict_references_are_added_only_for_a_review_session(self) -> None:
        with (
            mock.patch.object(
                turn_preparation.review_delta,
                "_verification_repair_state",
                return_value={"references": ("a",)},
            ),
            mock.patch.object(
                turn_preparation.review_transitions,
                "review_verdict_repair_references",
                return_value=("b",),
            ),
        ):
            implementation = self.collect()
            review = self.collect(classification=self.classification(review=True))

        self.assertEqual(("a",), implementation.repair_references)
        self.assertEqual(("a", "b"), review.repair_references)

    def test_guidance_is_not_read_when_the_mailbox_is_disabled(self) -> None:
        self.assertEqual((), self.collect().pending_guidance)


class RepairResumptionTests(PreparationTestCase):
    """A repair child's scope, eligibility and clean tree, settled up front."""

    def resume(self, **overrides: Any) -> turn_preparation.RepairResumption:
        snapshot = overrides.pop("snapshot", None) or self.snapshot()
        kwargs: dict[str, Any] = {
            "session": snapshot.sessions[0],
            "classification": self.classification(repair=True),
            "args": self.args(),
            "cwd": self.tmp_dir,
            "workspace": None,
            "session_scope": None,
            "session_label": "Session 1",
            "session_identity": {"session_id": "1"},
            "session_details": {"session_id": "1"},
            "retained_scope_repair": None,
            "max_review_fix_cycles": 1,
            "review_requested": False,
        }
        kwargs.update(overrides)
        return turn_preparation.resume_repair_session(
            self.ctx, snapshot=snapshot, **kwargs
        )

    def test_an_ordinary_session_is_left_alone(self) -> None:
        result = self.resume(classification=self.classification())

        self.assertIsNone(result.session_scope)
        self.assertIsNone(result.regenerated_write_scope)
        self.assertFalse(result.paused)
        self.assertEqual([], self.ctx.persistence.events)

    def test_an_ineligible_child_never_reaches_commit_preflight(self) -> None:
        with (
            mock.patch.object(
                turn_preparation.write_guards_repair,
                "_pending_repair_child_continuation",
                return_value=FakeReviewResume(
                    action="run-repairs", eligible=False, blocker="repair-scope-stale"
                ),
            ),
            mock.patch.object(
                turn_preparation.review_guard, "_repair_commit_preflight"
            ) as preflight,
        ):
            result = self.resume()

        self.assertTrue(result.paused)
        preflight.assert_not_called()
        (pause,) = self.ctx.pauses
        self.assertEqual(events.PAUSE_REASON_MALFORMED_REVIEW_REPAIR, pause["reason"])
        self.assertIn("repair-scope-stale", pause["message"])
        self.assertIn("review_resume", pause["details"])

    def test_a_dirty_writable_set_pauses_before_the_turn(self) -> None:
        error = review_guard.RepairCommitPreflightError("boom", {"dirty": ["a.py"]})
        with (
            mock.patch.object(
                turn_preparation.write_guards_repair,
                "_pending_repair_child_continuation",
                return_value=FakeReviewResume(
                    action="run-repairs", eligible=True, blocker=None
                ),
            ),
            mock.patch.object(
                turn_preparation.review_guard,
                "_repair_commit_preflight",
                side_effect=error,
            ),
        ):
            result = self.resume()

        self.assertTrue(result.paused)
        (pause,) = self.ctx.pauses
        self.assertEqual("commit-preflight", pause["reason"])
        self.assertEqual(["a.py"], pause["details"]["dirty"])
        self.assertEqual([], self.ctx.persistence.events)

    def test_a_clean_child_records_its_preflight_once(self) -> None:
        with (
            mock.patch.object(
                turn_preparation.write_guards_repair,
                "_pending_repair_child_continuation",
                return_value=FakeReviewResume(
                    action="run-repairs", eligible=True, blocker=None
                ),
            ),
            mock.patch.object(
                turn_preparation.review_guard,
                "_repair_commit_preflight",
                return_value={"clean": True},
            ),
        ):
            result = self.resume()

        self.assertFalse(result.paused)
        self.assertEqual([], self.ctx.pauses)
        self.assertEqual(
            [{"session_id": "1", "clean": True}],
            self.ctx.payload["repair_commit_preflights"],
        )
        ((name, payload),) = self.ctx.persistence.events
        self.assertEqual(events.REPAIR_COMMIT_PREFLIGHT_EVENT, name)
        self.assertEqual(RUN_ID, payload["run_id"])
        self.assertEqual(1, len(self.ctx.persistence.states))


if __name__ == "__main__":
    unittest.main()
