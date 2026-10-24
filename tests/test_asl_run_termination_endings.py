"""Readiness, honored-stop and terminal-verdict seams of the termination boundary."""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    plans_models,
    plans_payloads,
    plans_snapshot,
    run_termination,
)

#: The symbols this module took from the runner. The program's no-alias rule
from tests.asl_run_termination_support import _TerminationCase

ASL_TEST_PORTABLE = True


class ReadinessCloseTests(_TerminationCase):
    """The readiness writer enters as a seam and its outcome is recorded."""

    def test_a_successful_write_records_the_state_the_run_left(self) -> None:
        calls: list[tuple[Path, str]] = []

        def writer(
            plan_dir: Path,
            new_state: plans_models.ReadinessState,
            *,
            expected_sha256: str | None = None,
        ) -> tuple[bool, str | None]:
            calls.append((plan_dir, new_state))
            return True, None

        payload = dict(self.payload, readiness_state_active=True, ok=True)
        run_termination._finalize_readiness_lifecycle(
            payload, write_readiness_state=writer
        )

        self.assertEqual([(self.plan_dir, "loop-ready")], calls)
        self.assertEqual("loop-ready", payload["readiness_state_final"])
        self.assertEqual("loop-ready", payload["readiness_state_current"])

    def test_a_refused_write_records_the_error_and_no_final_state(self) -> None:
        def writer(
            plan_dir: Path,
            new_state: plans_models.ReadinessState,
            *,
            expected_sha256: str | None = None,
        ) -> tuple[bool, str | None]:
            return False, "readiness digest moved"

        payload = dict(self.payload, readiness_state_active=True, ok=True)
        run_termination._finalize_readiness_lifecycle(
            payload, write_readiness_state=writer
        )

        self.assertIsNone(payload["readiness_state_final"])
        self.assertEqual(
            "readiness digest moved", payload["readiness_state_write_error"]
        )

    def test_an_inactive_lifecycle_never_reaches_the_writer(self) -> None:
        def writer(
            plan_dir: Path,
            new_state: plans_models.ReadinessState,
            *,
            expected_sha256: str | None = None,
        ) -> tuple[bool, str | None]:
            raise AssertionError("the writer must not run for an inactive lifecycle")

        payload = dict(self.payload, ok=True)
        run_termination._finalize_readiness_lifecycle(
            payload, write_readiness_state=writer
        )

        self.assertNotIn("readiness_state_final", payload)

    def test_the_finalizer_factory_binds_the_writer_it_was_given(self) -> None:
        # `_finish_run` takes a one-argument callback, so the seam has to be
        # bound somewhere; this is the one place that binding is made.
        calls: list[str] = []

        def writer(
            plan_dir: Path,
            new_state: plans_models.ReadinessState,
            *,
            expected_sha256: str | None = None,
        ) -> tuple[bool, str | None]:
            calls.append(new_state)
            return True, None

        payload = dict(self.payload, readiness_state_active=True, ok=True)
        run_termination.readiness_finalizer(writer)(payload)

        self.assertEqual(["loop-ready"], calls)


class HonoredStopTests(_TerminationCase):
    """The stop and the abort a supervisor asks for, and their seams."""

    def test_the_stop_records_the_snapshot_it_was_handed(self) -> None:
        listener = self._listener()
        listener.state.request_stop_after_current()

        run_termination._honor_stop_after_current(
            listener,
            snapshot=self._snapshot(),
            payload=self.payload,
            event_path=self.event_path,
            control_payload={},
            persistence=self.persistence,
        )

        self.assertEqual("stopped", self.payload["reason"])
        self.assertEqual(
            plans_payloads.snapshot_payload(self._snapshot()),
            self.payload["final_snapshot"],
        )
        self.assertIsNone(listener.state.consume_stop_after_current())

    def test_the_abort_reports_the_snapshot_taken_after_the_revert(self) -> None:
        # The whole point of the revert is that the killed session no longer
        # holds a completion; reporting the pre-revert snapshot would say it
        # still does.
        reverted_path = self.plan_dir / "session-2.md"
        seen: dict[str, Any] = {}

        def revert(
            snapshot: plans_models.PlanSnapshot,
            session_id: str,
            *,
            failure_note: str,
        ) -> Path | None:
            seen["session_id"] = session_id
            seen["failure_note"] = failure_note
            return reverted_path

        after = self._snapshot()

        def reinspect(
            plan_dir: Path,
            fallback: plans_models.PlanSnapshot,
            path: Path | None,
            plan_cache: plans_snapshot.PlanSnapshotCache,
        ) -> plans_models.PlanSnapshot:
            seen["reverted_path"] = path
            return after

        run_termination._honor_abort_turn(
            self._listener(),
            snapshot=self._snapshot(),
            session_id="2",
            payload=self.payload,
            event_path=self.event_path,
            control_payload={},
            plan_cache=plans_snapshot.PlanSnapshotCache(),
            revert_session_status=revert,
            reinspect=reinspect,
            persistence=self.persistence,
        )

        self.assertEqual("2", seen["session_id"])
        self.assertIn("aborted by supervisor request", seen["failure_note"])
        self.assertEqual(reverted_path, seen["reverted_path"])
        self.assertEqual("stopped", self.payload["reason"])
        self.assertEqual(
            plans_payloads.snapshot_payload(after), self.payload["final_snapshot"]
        )

    def test_the_review_stop_records_its_verdict_before_the_stop(self) -> None:
        order: list[str] = []

        def record(payload: dict[str, Any], plan_dir: Path) -> None:
            order.append("verdict")
            payload["review_verdict"] = {"outcome": "all-clear"}

        original = self.persistence.write_state

        def write_state(payload: dict[str, Any]) -> None:
            order.append("stop")
            original(payload)

        self.persistence.write_state = write_state  # type: ignore[method-assign]
        try:
            run_termination._stop_before_review_work(
                self._listener(),
                snapshot=self._snapshot(),
                payload=self.payload,
                event_path=self.event_path,
                control_payload={},
                record_existing_review_verdict=record,
                persistence=self.persistence,
            )
        finally:
            self.persistence.write_state = original  # type: ignore[method-assign]

        self.assertEqual(["verdict", "stop"], order)
        self.assertEqual({"outcome": "all-clear"}, self.payload["review_verdict"])


class TerminalVerdictTests(_TerminationCase):
    """Adjudication, and the endings it is allowed to change."""

    def test_a_paused_run_keeps_the_blocker_it_arrived_with(self) -> None:
        payload = dict(self.payload, paused=True, ok=False, reason="blocked")
        adjudication = run_termination._adjudicate_terminal_run(
            event_path=self.event_path,
            payload=payload,
            persistence=self.persistence,
        )

        self.assertFalse(adjudication.blocked)
        self.assertEqual("blocked", adjudication.claimed_reason)
        self.assertFalse(payload["terminal_adjudication"]["blocked"])

    def test_a_dry_run_is_not_adjudicated_because_it_claimed_nothing(self) -> None:
        payload = dict(self.payload, ok=True, reason="dry-run")
        adjudication = run_termination._adjudicate_terminal_run(
            event_path=self.event_path,
            payload=payload,
            persistence=self.persistence,
        )

        self.assertFalse(adjudication.blocked)
        self.assertEqual("dry-run", run_termination._terminal_outcome(payload))
        self.assertIn("dry-run", run_termination._UNAUDITED_TERMINAL_OUTCOMES)


if __name__ == "__main__":
    unittest.main()
