"""Tests for test asl overseer resume execution."""

from __future__ import annotations

import dataclasses
import subprocess
import unittest
from typing import Any
from unittest import mock

from agent_session_loop import (
    intervention,
    overseer_dispatch,
    supervision_liveness,
)
from tests import support_asl_overseer_resume as fixtures

ASL_TEST_PORTABLE = True


class ObservedProgress(fixtures._ResumeCase):
    """A spawned pid is not a resolution."""

    def test_resolution_is_reported_only_after_the_run_moves(self) -> None:
        self.moves_after = 4

        preparation, receipt = self.dispatch(self.adapter(timeout_seconds=30.0))

        self.assertTrue(preparation.ready)
        assert receipt is not None
        self.assertEqual("executed", receipt.outcome)
        # It waited rather than answering from the launch alone.
        self.assertTrue(self.slept)

    def test_a_child_that_changes_nothing_leaves_the_row_in_doubt(self) -> None:
        self.moves_after = None

        preparation, receipt = self.dispatch(self.adapter(timeout_seconds=5.0))

        self.assertTrue(preparation.ready)
        assert receipt is not None
        self.assertEqual("uncertain", receipt.outcome)
        self.assertRefused(receipt.detail, "progress_unobserved")
        self.assertEqual("pid 4242", receipt.locator)
        # The launch did happen; what is missing is the evidence it took hold.
        self.assertEqual(1, len(self.launches))

    def test_a_run_that_becomes_unreadable_mid_watch_is_not_progress(self) -> None:
        self.moves_after = None
        adapter = self.adapter(timeout_seconds=3.0)
        action = self.action()
        preparation = adapter.prepare(action)
        self.assertTrue(preparation.ready)

        original = self.inspect_run
        calls = {"n": 0}

        def flaky(plan_dir: Any, *, state_dir: Any = None) -> intervention.RunFacts:
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("the state directory went away for a moment")
            return original(plan_dir, state_dir=state_dir)

        adapter = dataclasses.replace(adapter, inspect_run=flaky)
        adapter.prepared.update(self.adapter_memo(action))
        receipt = adapter.execute(action)

        self.assertEqual("uncertain", receipt.outcome)
        self.assertRefused(receipt.detail, "progress_unobserved")

    def adapter_memo(self, action: overseer_dispatch.Action) -> dict[str, Any]:
        """Re-prepare on a fresh adapter, so a seam can be swapped in between."""
        adapter = self.adapter()
        self.assertTrue(adapter.prepare(action).ready)
        return dict(adapter.prepared)

    def test_a_launch_that_could_not_spawn_is_a_failure_not_a_wait(self) -> None:
        self.launch_error = OSError("no such executable")

        preparation, receipt = self.dispatch()

        self.assertTrue(preparation.ready)
        assert receipt is not None
        self.assertEqual("failed", receipt.outcome)
        self.assertRefused(receipt.detail, "launch_failed")
        self.assertEqual([], self.slept)

    def test_a_launcher_refusal_is_a_failure_too(self) -> None:
        self.launch_error = intervention.InterventionError(
            "invalid-profile", "the recorded cwd is not this plan's workspace"
        )

        _, receipt = self.dispatch()

        assert receipt is not None
        self.assertEqual("failed", receipt.outcome)
        self.assertRefused(receipt.detail, "launch_failed")

    def test_execution_without_preparation_launches_nothing(self) -> None:
        adapter = self.adapter()

        receipt = adapter.execute(self.action())

        self.assertEqual("failed", receipt.outcome)
        self.assertRefused(receipt.detail, "not_prepared")
        self.assertEqual([], self.launches)

    def test_a_preparation_is_consumed_once(self) -> None:
        adapter = self.adapter()
        action = self.action()
        self.assertTrue(adapter.prepare(action).ready)
        self.assertEqual("executed", adapter.execute(action).outcome)

        again = adapter.execute(action)

        self.assertEqual("failed", again.outcome)
        self.assertRefused(again.detail, "not_prepared")
        self.assertEqual(1, len(self.launches))


class ConcurrentManualResume(fixtures._ResumeCase):
    """Somebody else got there first, and they keep the run."""

    def test_a_run_that_moved_between_the_two_calls_is_refused(self) -> None:
        adapter = self.adapter()
        action = self.action()
        self.moves_after = None
        self.assertTrue(adapter.prepare(action).ready)

        # An operator ran ``asl run --resume`` by hand in the meantime.
        self.runner_state = self.paused_state("no-status-change")

        receipt = adapter.execute(action)

        self.assertEqual("failed", receipt.outcome)
        self.assertRefused(receipt.detail, "concurrent_resume")
        self.assertIn("already resumed it", receipt.detail)
        self.assertEqual([], self.launches)

    def test_an_owner_that_appeared_since_preparation_is_a_concurrent_resume(
        self,
    ) -> None:
        adapter = self.adapter()
        action = self.action()
        self.moves_after = None
        self.assertTrue(adapter.prepare(action).ready)

        # The manual resume has taken the lock but has not moved the run yet.
        self.live_lock()

        receipt = adapter.execute(action)

        self.assertEqual("failed", receipt.outcome)
        self.assertRefused(receipt.detail, "concurrent_resume")
        self.assertIn("was free when the resume was prepared", receipt.detail)
        self.assertEqual([], self.launches)

    def test_a_supervisor_that_appeared_since_preparation_is_the_same_thing(
        self,
    ) -> None:
        adapter = self.adapter()
        action = self.action()
        self.moves_after = None
        self.assertTrue(adapter.prepare(action).ready)

        self.write_helper("sup-late", verdict=supervision_liveness.ALIVE)

        receipt = adapter.execute(action)

        self.assertRefused(receipt.detail, "concurrent_resume")
        self.assertIn(self.helper_id("sup-late"), receipt.detail)

    def test_ownership_that_became_unknown_is_reported_as_unknown(self) -> None:
        # Unknown is not evidence somebody resumed; it is evidence of nothing,
        # and renaming it would tell an operator a story.
        adapter = self.adapter()
        action = self.action()
        self.moves_after = None
        self.assertTrue(adapter.prepare(action).ready)

        self.corrupt_helper("sup-broken")

        receipt = adapter.execute(action)

        self.assertRefused(receipt.detail, "ownership_unknown")
        self.assertEqual([], self.launches)

    def test_nothing_is_ever_killed_to_make_room(self) -> None:
        adapter = self.adapter()
        action = self.action()
        self.moves_after = None
        self.assertTrue(adapter.prepare(action).ready)
        self.live_lock(pid=5150)
        helper = self.write_helper("sup-1", verdict=supervision_liveness.DEAD)

        adapter.execute(action)

        self.assertTrue(helper.exists())
        self.assertTrue(self.ownership.lock_present)
        self.assertEqual(5150, self.ownership.pid)


class AuthorityGuards(fixtures._ResumeCase):
    """A resume is an action, so it is authorized like one."""

    def test_a_payload_that_is_not_a_resume_is_refused(self) -> None:
        action = self.action(payload=overseer_dispatch.Commit(message="checkpoint"))
        adapter = self.adapter()

        preparation = adapter.prepare(action)

        self.assertFalse(preparation.ready)
        self.assertRefused(preparation.detail, "payload_mismatch")
        self.assertEqual("failed", adapter.execute(action).outcome)

    def test_no_ruling_is_no_authority(self) -> None:
        preparation, receipt = self.dispatch(self.adapter(None))

        self.assertFalse(preparation.ready)
        self.assertRefused(preparation.detail, "authority_absent")
        self.assertIsNone(receipt)

    def test_a_hold_stops_the_resume(self) -> None:
        held = self.ruling(stance="hold")

        preparation, _ = self.dispatch(self.adapter(held))

        self.assertFalse(preparation.ready)
        self.assertRefused(preparation.detail, "authority_held")

    def test_a_ruling_about_another_operation_says_nothing_about_this_one(
        self,
    ) -> None:
        other = self.ruling(operation="commit")

        preparation, _ = self.dispatch(self.adapter(other))

        self.assertFalse(preparation.ready)
        self.assertRefused(preparation.detail, "authority_silent")
        self.assertIn("'commit'", preparation.detail)

    def test_a_decision_replaced_between_the_two_calls_is_superseded(self) -> None:
        adapter = self.adapter(self.ruling(), self.ruling(decision_id="d-2"))
        action = self.action()
        self.assertTrue(adapter.prepare(action).ready)

        receipt = adapter.execute(action)

        self.assertEqual("failed", receipt.outcome)
        self.assertRefused(receipt.detail, "authority_superseded")
        self.assertIn("d-1", receipt.detail)
        self.assertIn("d-2", receipt.detail)
        self.assertEqual([], self.launches)

    def test_a_decision_withdrawn_between_the_two_calls_stops_the_launch(self) -> None:
        adapter = self.adapter(self.ruling(), None)
        action = self.action()
        self.assertTrue(adapter.prepare(action).ready)

        receipt = adapter.execute(action)

        self.assertEqual("failed", receipt.outcome)
        self.assertRefused(receipt.detail, "authority_absent")
        self.assertEqual([], self.launches)


class Reconciliation(fixtures._ResumeCase):
    """A row a dead process left, answered from the row alone."""

    def test_a_run_that_moved_settles_as_executed(self) -> None:
        # The row was written while the run was in one situation; the run is in
        # another now. That comparison is the whole answer, and it needs
        # nothing the dispatching process remembered.
        action = self.action()
        self.moves_after = None
        record = self.attempted(action, progress_token="sha256:a-situation-since-left")

        receipt = self.adapter().reconcile(record)

        self.assertEqual("executed", receipt.outcome)
        self.assertIn("left the situation", receipt.detail)
        self.assertTrue(receipt.locator)

    def test_the_same_situation_with_nothing_holding_it_settles_as_failed(
        self,
    ) -> None:
        action = self.action()
        self.moves_after = None
        record = self.attempted(action)

        receipt = self.adapter().reconcile(record)

        self.assertEqual("failed", receipt.outcome)
        self.assertRefused(receipt.detail, "no_progress")

    def test_a_live_worker_leaves_the_row_in_doubt(self) -> None:
        self.moves_after = None
        record = self.attempted(self.action())
        self.live_lock()

        receipt = self.adapter().reconcile(record)

        self.assertEqual("uncertain", receipt.outcome)
        self.assertRefused(receipt.detail, "progress_unobserved")

    def test_a_live_supervisor_leaves_the_row_in_doubt(self) -> None:
        self.moves_after = None
        record = self.attempted(self.action())
        self.write_helper("sup-1", verdict=supervision_liveness.ALIVE)

        receipt = self.adapter().reconcile(record)

        self.assertEqual("uncertain", receipt.outcome)

    def test_an_unreadable_run_is_evidence_of_nothing(self) -> None:
        record = self.attempted(self.action())
        self.readable = False

        receipt = self.adapter().reconcile(record)

        self.assertEqual("uncertain", receipt.outcome)
        self.assertRefused(receipt.detail, "run_unreadable")
        self.assertEqual(record.progress_token, receipt.locator)

    def test_another_operations_row_is_not_this_adapters_to_settle(self) -> None:
        commit = self.ledger.attempt_action(
            grant=self.grant(),
            plan_dir=self.plan,
            operation="commit",
            target="checkpoint",
            incident_id=self.incident_id,
        )

        receipt = self.adapter().reconcile(commit)

        self.assertEqual("uncertain", receipt.outcome)
        self.assertRefused(receipt.detail, "payload_mismatch")

    def test_reconciliation_needs_nothing_the_executing_process_remembered(
        self,
    ) -> None:
        # The case this exists for: the process that dispatched is gone, so a
        # fresh adapter with an empty memo must still answer.
        self.moves_after = None
        record = self.attempted(self.action())
        fresh = self.adapter()

        self.assertEqual({}, fresh.prepared)
        self.assertEqual("failed", fresh.reconcile(record).outcome)


class Portability(fixtures._ResumeCase):
    """Nothing at this boundary shells out, which is the PORTABLE_MODULES claim."""

    def test_a_whole_resume_spawns_no_process_of_its_own(self) -> None:
        # The child this operation starts goes through the injected launch
        # seam. Everything else -- reading the run, probing the helpers,
        # watching for progress -- must reach no process at all, or the module
        # cannot honestly run on the portable lane.
        self.write_helper("sup-1", verdict=supervision_liveness.DEAD)

        def refuse(*args: object, **kwargs: object) -> None:
            raise AssertionError("this boundary must not spawn a process")

        with (
            mock.patch.object(subprocess, "Popen", refuse),
            mock.patch.object(subprocess, "run", refuse),
            mock.patch.object(subprocess, "call", refuse),
            mock.patch.object(subprocess, "check_call", refuse),
            mock.patch.object(subprocess, "check_output", refuse),
        ):
            preparation, receipt = self.dispatch()

        self.assertTrue(preparation.ready)
        assert receipt is not None
        self.assertEqual("executed", receipt.outcome)


if __name__ == "__main__":
    unittest.main()
