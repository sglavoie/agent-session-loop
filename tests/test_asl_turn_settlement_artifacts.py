"""Asl turn settlement artifacts regressions."""

from __future__ import annotations

import dataclasses
import unittest
from typing import Any

from agent_session_loop import (
    handoff_correction,
    handoff_guards,
    plans_content_hash,
    plans_payloads,
    plans_snapshot,
    run_termination,
    turn_settlement,
)

from .support import write_plan
from .support_asl_turn_settlement import _REAL_PLAN_ARTIFACT_GUARD, SettlementTestCase

ASL_TEST_PORTABLE = True


class StatusRefusalArtifactAdjudicationTests(SettlementTestCase):
    """A status refusal still answers the packet question (ASI-119).

    The three status refusals pause before the plan-artifact guard's ordinary
    slot, so without an adjudicate-only pass a turn that also rewrote another
    session's packet left that packet in the plan with no verdict at all. The
    real guard runs here; only the pause is faked, and it writes the same pause
    block the run's own writer does so the guard can defer to it.
    """

    def _install_collaborators(self) -> None:
        super()._install_collaborators()
        # The real guard, not the recorder the base installs.
        self.patch(
            handoff_guards,
            "_enforce_plan_artifact_guard",
            _REAL_PLAN_ARTIFACT_GUARD,
        )
        self.appended_notes: list[Any] = []
        self.patch(
            handoff_correction,
            "_append_handoff_note",
            lambda *args, **kwargs: self.appended_notes.append((args, kwargs)),
        )
        pauses = self.ctx.pauses
        payload = self.ctx.payload

        def _pause(
            *, reason: str, message: str, details: dict[str, Any] | None
        ) -> None:
            pauses.append({"reason": reason, "message": message, "details": details})
            run_termination.set_pause(
                payload, reason=reason, message=message, details=details
            )

        self.ctx.pause = _pause  # type: ignore[method-assign]

    def digests(self, exclude: str = "1") -> dict[str, str]:
        return plans_content_hash.session_artifact_digests(
            self.plan_dir, exclude_session_id=exclude
        )

    def rewrite(self, session_number: str) -> None:
        path = self.plan_dir / f"session-{session_number}.md"
        path.write_text(
            path.read_text(encoding="utf-8") + "\n- **Note**: rewritten\n",
            encoding="utf-8",
        )

    def artifact_rejections(self) -> list[dict[str, Any]]:
        return [
            payload
            for name, payload in self.ctx.persistence.events
            if name == "turn_rejected" and payload.get("guard") == "plan-artifact-guard"
        ]

    def assert_passed_adjudicated_only(self, reason: str) -> None:
        self.assertEqual([reason], [p["reason"] for p in self.ctx.pauses])
        guard = self.turn_payload["plan_artifact_guard"]
        self.assertTrue(guard["passed"])
        self.assertTrue(guard["adjudicated_only"])
        self.assertEqual([], self.artifact_rejections())

    def assert_deferred_conviction(self, reason: str, changed: list[str]) -> None:
        self.assertEqual([reason], [p["reason"] for p in self.ctx.pauses])
        self.assertEqual(reason, self.ctx.payload["pause_reason"])
        guard = self.turn_payload["plan_artifact_guard"]
        self.assertFalse(guard["passed"])
        self.assertTrue(guard["adjudicated_only"])
        self.assertEqual(changed, guard["changed_artifacts"])
        self.assertEqual(reason, guard["deferred_to_pause_reason"])
        self.assertEqual(
            guard, self.ctx.payload["pause_details"]["plan_artifact_guard"]
        )
        # Reverted at most once: the adjudication never reverts or appends a
        # failure note, and the turn is rejected under this guard exactly once.
        self.assertFalse(guard["reverted_to_pending"])
        self.assertEqual([], self.appended_notes)
        rejections = self.artifact_rejections()
        self.assertEqual(1, len(rejections))
        self.assertFalse(rejections[0]["reopened"])

    def test_own_packet_write_on_no_status_change_passes(self) -> None:
        digests_before = self.digests()
        self.rewrite("1")

        settlement = self.settle(
            after=self.before, plan_artifacts_before=digests_before
        )

        self.assertEqual(turn_settlement.ACTION_STOP, settlement.action)
        self.assert_passed_adjudicated_only("no-status-change")

    def test_foreign_packet_write_on_no_status_change_is_convicted(self) -> None:
        digests_before = self.digests()
        self.rewrite("2")

        self.settle(after=self.before, plan_artifacts_before=digests_before)

        self.assert_deferred_conviction("no-status-change", ["session-2.md"])
        self.assertEqual(
            "complete",
            plans_payloads.status_map(plans_snapshot.inspect_plan(self.plan_dir))["1"],
        )

    def test_own_packet_write_on_multiple_status_changes_passes(self) -> None:
        digests_before = self.digests()
        self.rewrite("1")
        after = plans_snapshot.inspect_plan(self.plan_dir)
        # Two statuses moved in the snapshot, but no foreign packet on disk.
        multiple = dataclasses.replace(
            after,
            sessions=tuple(
                dataclasses.replace(session, status="complete")
                for session in after.sessions
            ),
        )

        self.settle(after=multiple, plan_artifacts_before=digests_before)

        self.assert_passed_adjudicated_only("multiple-status-changes")

    def test_foreign_packet_write_on_multiple_status_changes_is_convicted(
        self,
    ) -> None:
        digests_before = self.digests()
        write_plan(self.plan_dir, ("complete", "complete"))

        self.settle(
            after=plans_snapshot.inspect_plan(self.plan_dir),
            plan_artifacts_before=digests_before,
        )

        self.assert_deferred_conviction("multiple-status-changes", ["session-2.md"])
        self.assertEqual(
            {"1": "complete", "2": "complete"},
            dict(plans_payloads.status_map(plans_snapshot.inspect_plan(self.plan_dir))),
        )

    def test_own_packet_write_on_unexpected_status_change_passes(self) -> None:
        digests_before = self.digests(exclude="2")
        self.rewrite("2")

        self.settle(session_id="2", plan_artifacts_before=digests_before)

        self.assert_passed_adjudicated_only("unexpected-status-change")

    def test_foreign_packet_write_on_unexpected_status_change_is_convicted(
        self,
    ) -> None:
        # Session 2 was selected, but session 1's packet moved under it.
        write_plan(self.plan_dir, ("pending", "pending"))
        digests_before = self.digests(exclude="2")
        write_plan(self.plan_dir, ("complete", "pending"))

        self.settle(session_id="2", plan_artifacts_before=digests_before)

        self.assert_deferred_conviction("unexpected-status-change", ["session-1.md"])


if __name__ == "__main__":
    unittest.main()
