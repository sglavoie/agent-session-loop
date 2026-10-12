#!/usr/bin/env python3
"""Tests for supervise_run honoring stop and abort controls."""

from __future__ import annotations

import unittest

from asl_autopilot_test_bases import SuperviseRunInjectedBase
from asl_autopilot_test_payloads import (
    OLDER_RUN_STARTED_AT,
    _attached_run_status,
    _control_block,
    _finished_run_status,
    _leftover_state_attach_status,
    _review_resume,
    _startup_attach_status,
    _status_payload,
)
from asl_autopilot_test_support import (  # noqa: F401
    _never_run,
    _RunScript,
    _StatusScript,
    autopilot,
    setUpModule,
    tearDownModule,
)


class SuperviseRunStopControlTest(SuperviseRunInjectedBase):
    def test_honored_stop_after_current_stops_cleanly(self) -> None:
        # The runner honored an out-of-band `asl control stop-after-current`:
        # it exits ok/non-paused with `reason: "stopped"` after finishing the
        # current session (pending 3 -> 2). The supervisor must report that as a
        # clean early stop instead of relaunching the remaining sessions.
        status = _StatusScript([_status_payload(3), _status_payload(2)])
        run = _RunScript([{"ok": True, "paused": False, "reason": "stopped"}])
        result = self._supervise(status, run)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "stopped")
        self.assertEqual(result["chunks"], 1)
        self.assertEqual(result["completed"], 1)
        self.assertEqual(result["pending"], 2)
        self.assertEqual(result["stop_kind"], autopilot.STOP_KIND_AFTER_CURRENT)
        # No further chunk relaunched.
        self.assertEqual(run.chunks, [1])

    def test_honored_abort_stops_cleanly_as_an_abort(self) -> None:
        # `asl control abort` ends the turn in flight and reverts its session to
        # pending, so the run exits with the same clean `reason: "stopped"` but
        # *no* session completed (pending stays 3). That is a clean early stop,
        # not an anomaly: same `ok: true`, no relaunch, and `stop_kind` names the
        # abort so the zero progress is not read as a stall.
        status = _StatusScript([_status_payload(3), _status_payload(3)])
        run = _RunScript(
            [
                {
                    "ok": True,
                    "paused": False,
                    "reason": "stopped",
                    "control": _control_block("abort-turn-honored"),
                }
            ]
        )
        result = self._supervise(status, run)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "stopped")
        self.assertEqual(result["stop_kind"], autopilot.STOP_KIND_ABORT)
        self.assertEqual(result["chunks"], 1)
        self.assertEqual(result["completed"], 0)
        self.assertEqual(result["pending"], 3)
        self.assertEqual(run.chunks, [1])

    def test_abort_satisfied_by_the_worker_is_also_an_abort_stop(self) -> None:
        # The worker wound the turn down inside the grace window, so nothing was
        # killed — but the request that ended the run was still the abort.
        status = _StatusScript([_status_payload(3), _status_payload(3)])
        run = _RunScript(
            [
                {
                    "ok": True,
                    "paused": False,
                    "reason": "stopped",
                    "control": _control_block("abort-turn-satisfied-by-worker"),
                }
            ]
        )
        result = self._supervise(status, run)
        self.assertEqual(result["reason"], "stopped")
        self.assertEqual(result["stop_kind"], autopilot.STOP_KIND_ABORT)

    def test_stop_without_a_control_block_reads_as_a_boundary_stop(self) -> None:
        # An older runner (or a torn payload) exposes no control block. The safe
        # reading is the session-boundary stop, the outcome that leaves finished
        # work behind, so nothing claims a session was reverted.
        status = _StatusScript([_status_payload(3), _status_payload(2)])
        run = _RunScript(
            [{"ok": True, "paused": False, "reason": "stopped", "control": None}]
        )
        result = self._supervise(status, run)
        self.assertEqual(result["stop_kind"], autopilot.STOP_KIND_AFTER_CURRENT)

    def test_stop_reported_with_nothing_pending_completes(self) -> None:
        # A stop that lands exactly as pending reaches 0 must not pre-empt the
        # normal complete/review path.
        status = _StatusScript([_status_payload(1), _status_payload(0)])
        run = _RunScript([{"ok": True, "paused": False, "reason": "stopped"}])
        result = self._supervise(status, run)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "complete")
        self.assertEqual(run.chunks, [1])

    def test_stop_with_a_pending_review_stops_instead_of_completing(self) -> None:
        # The runner's stop covers the review too, so a stop that lands as
        # pending reaches 0 while a review is still projected is an early stop,
        # not a completion: relaunching would run the review the operator
        # asked to prevent.
        pending_review = _status_payload(0)
        pending_review["review_resume"] = _review_resume(
            "initial-review", eligible=True
        )
        status = _StatusScript([_status_payload(1), pending_review])
        run = _RunScript([{"ok": True, "paused": False, "reason": "stopped"}])
        parsed = {
            "plan_dir": "/tmp/plan",
            "argv": ["asl", "run", "/tmp/plan", "--review-harness", "codex", "--json"],
            "review_harness": "codex",
            "max_review_fix_cycles": 2,
        }
        result = self._supervise(status, run, parsed=parsed)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "stopped")
        self.assertEqual(run.chunks, [1])

    def test_stop_takes_precedence_over_a_relaunch_without_progress(self) -> None:
        # Even when the stopped chunk completed no session (pending unchanged),
        # the request is honored rather than relaunched, and the reported counts
        # stay consistent with the budget accounting.
        status = _StatusScript([_status_payload(2)])
        run = _RunScript([{"ok": True, "paused": False, "reason": "stopped"}])
        result = self._supervise(status, run, max_chunks=5)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "stopped")
        self.assertEqual(result["completed"], 0)
        self.assertEqual(result["pending"], 2)
        self.assertEqual(run.chunks, [1])

    def test_attached_run_that_honored_stop_is_terminal(self) -> None:
        # The supervisor attaches to a foreign run (run-a) that then honors an
        # out-of-band `asl control stop-after-current`: it releases the lock
        # having persisted `ok`/`reason: "stopped"` with sessions still pending.
        # The helper must report that clean early stop rather than launching a
        # chunk that would undo the operator's request.
        status = _StatusScript(
            [
                _attached_run_status(3),
                _finished_run_status(2, reason="stopped"),
            ]
        )
        result = self._supervise(status, _never_run)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "stopped")
        self.assertEqual(result["chunks"], 0)
        # One session finished while attached (pending 3 -> 2).
        self.assertEqual(result["completed"], 1)
        self.assertEqual(result["pending"], 2)
        self.assertEqual(result["stop_kind"], autopilot.STOP_KIND_AFTER_CURRENT)

    def test_attached_run_that_was_aborted_is_terminal(self) -> None:
        # Same attach path, but the foreign run was ended by `asl control abort`:
        # the persisted control block's terminal action is what distinguishes it,
        # and the reverted session keeps pending at 3.
        status = _StatusScript(
            [
                _attached_run_status(3),
                _finished_run_status(
                    3, reason="stopped", last_action="abort-turn-honored"
                ),
            ]
        )
        result = self._supervise(status, _never_run)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "stopped")
        self.assertEqual(result["stop_kind"], autopilot.STOP_KIND_ABORT)
        self.assertEqual(result["chunks"], 0)
        self.assertEqual(result["completed"], 0)
        self.assertEqual(result["pending"], 3)

    def test_attached_stop_is_terminal_before_any_run_id_is_observable(self) -> None:
        # The runner takes the plan lock before it persists the state carrying
        # its `run_id`. Our only attached poll lands in that window and the run
        # then finishes fast, honoring the stop — so no poll ever saw a
        # `run_id`. The lock we attached to still identifies the run that wrote
        # the state (it started after the lock was created), so the stop is
        # honored rather than relaunched over.
        status = _StatusScript(
            [
                _startup_attach_status(3),
                _finished_run_status(2, reason="stopped"),
            ]
        )
        result = self._supervise(status, _never_run)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "stopped")
        self.assertEqual(result["chunks"], 0)
        self.assertEqual(result["completed"], 1)
        self.assertEqual(result["pending"], 2)

    def test_attached_stop_ignores_the_previous_runs_leftover_run_id(self) -> None:
        # Same startup window, but `state.json` still holds the *previous* run's
        # outcome. Latching that `run_id` would make the new run's own stopped
        # state look like a mismatch; it is rejected as older than the lock, so
        # the fresh stop is still honored.
        status = _StatusScript(
            [
                _leftover_state_attach_status(3, run_id="run-old"),
                _finished_run_status(2, run_id="run-new", reason="stopped"),
            ]
        )
        result = self._supervise(status, _never_run)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "stopped")
        self.assertEqual(result["chunks"], 0)
        self.assertEqual(result["completed"], 1)
        self.assertEqual(result["pending"], 2)

    def test_attached_run_that_never_persisted_state_still_launches(self) -> None:
        # Startup window again, but the run we attached to died without writing
        # anything: the `stopped` state left behind started before our lock
        # existed, so it belongs to an earlier run and must not block a launch.
        status = _StatusScript(
            [
                _startup_attach_status(2),
                _finished_run_status(
                    2,
                    run_id="run-old",
                    reason="stopped",
                    run_started_at=OLDER_RUN_STARTED_AT,
                ),
                _status_payload(0),
            ]
        )
        run = _RunScript([{"ok": True, "paused": False, "reason": "all-complete"}])
        result = self._supervise(status, run)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "complete")
        self.assertEqual(run.chunks, [1])

    def test_attached_stop_without_a_readable_lock_timestamp_still_launches(
        self,
    ) -> None:
        # No `run_id` observed and no lock `created_at` to correlate against
        # (an older `asl status` payload): there is no trustworthy identity, so
        # the conservative pre-existing launch behavior stands.
        status = _StatusScript(
            [
                _startup_attach_status(2, lock_created_at=None),
                _finished_run_status(2, reason="stopped"),
                _status_payload(0),
            ]
        )
        run = _RunScript([{"ok": True, "paused": False, "reason": "all-complete"}])
        result = self._supervise(status, run)
        self.assertEqual(result["reason"], "complete")
        self.assertEqual(run.chunks, [1])

    def test_attached_stop_follows_a_lock_taken_over_mid_attach(self) -> None:
        # The run we first attached to (run-a) releases and another takes the
        # lock while we watch. The new holder's identity is not observable yet,
        # but its lock is: run-a's id must not be held against the run that
        # actually honored the stop.
        status = _StatusScript(
            [
                _attached_run_status(3, run_id="run-a"),
                _startup_attach_status(2, lock_created_at="2026-07-24T19:05:00+00:00"),
                _finished_run_status(
                    1,
                    run_id="run-b",
                    reason="stopped",
                    run_started_at="2026-07-24T19:06:00+00:00",
                ),
            ]
        )
        result = self._supervise(status, _never_run)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "stopped")
        self.assertEqual(result["chunks"], 0)
        self.assertEqual(result["completed"], 2)
        self.assertEqual(result["pending"], 1)

    def test_attached_stop_counts_every_session_finished_while_attached(self) -> None:
        # Progress made across several attach polls is attributed to `completed`,
        # measured from the pending count at the first poll (4 -> 1).
        status = _StatusScript(
            [
                _attached_run_status(4),
                _attached_run_status(3),
                _attached_run_status(2),
                _finished_run_status(1, reason="stopped"),
            ]
        )
        result = self._supervise(status, _never_run)
        self.assertEqual(result["reason"], "stopped")
        self.assertEqual(result["completed"], 3)
        self.assertEqual(result["pending"], 1)

    def test_attached_stop_with_nothing_pending_completes(self) -> None:
        # As with a spent budget, a stop that lands exactly as pending reaches 0
        # must not pre-empt the normal complete/review path.
        status = _StatusScript(
            [
                _attached_run_status(1),
                _finished_run_status(0, reason="stopped"),
            ]
        )
        result = self._supervise(status, _never_run)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "complete")

    def test_stale_stopped_state_from_an_older_run_still_launches(self) -> None:
        # Never attached: the plan is unlocked and the `stopped` state on disk
        # belongs to some earlier run. It must not block a new run.
        status = _StatusScript(
            [
                _finished_run_status(2, run_id="run-old", reason="stopped"),
                _status_payload(0),
            ]
        )
        run = _RunScript([{"ok": True, "paused": False, "reason": "all-complete"}])
        result = self._supervise(status, run)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "complete")
        self.assertEqual(run.chunks, [1])

    def test_stopped_state_from_a_different_run_id_still_launches(self) -> None:
        # We attached to run-b, but the state left behind was written by run-old
        # (run-b died before persisting its own outcome). The identity mismatch
        # means the `stopped` reason says nothing about the run we watched.
        status = _StatusScript(
            [
                _attached_run_status(2, run_id="run-b"),
                _finished_run_status(2, run_id="run-old", reason="stopped"),
                _status_payload(0),
            ]
        )
        run = _RunScript([{"ok": True, "paused": False, "reason": "all-complete"}])
        result = self._supervise(status, run)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "complete")
        self.assertEqual(run.chunks, [1])

    def test_attached_run_stopped_for_another_reason_still_launches(self) -> None:
        # The attached run ended on its own `--max-sessions` budget, not on a
        # control request: the supervisor owns the remaining sessions and its own
        # (absent) budget, so it launches.
        status = _StatusScript(
            [
                _attached_run_status(3, run_id="run-a"),
                _finished_run_status(2, run_id="run-a", reason="max-sessions"),
                _status_payload(0),
            ]
        )
        run = _RunScript([{"ok": True, "paused": False, "reason": "all-complete"}])
        result = self._supervise(status, run)
        self.assertEqual(result["reason"], "complete")
        self.assertEqual(run.chunks, [1])

    def test_unfinished_attached_stop_state_still_launches(self) -> None:
        # An orphaned state (no `run_finished_at`, flagged stale_unfinished) says
        # nothing about how the run ended, even when a `stopped` reason was
        # persisted mid-flight before the process died.
        status = _StatusScript(
            [
                _attached_run_status(3, run_id="run-a"),
                _finished_run_status(
                    2, run_id="run-a", reason="stopped", finished=False
                ),
                _status_payload(0),
            ]
        )
        run = _RunScript([{"ok": True, "paused": False, "reason": "all-complete"}])
        result = self._supervise(status, run)
        self.assertEqual(result["reason"], "complete")
        self.assertEqual(run.chunks, [1])

    def test_attached_stop_respects_a_spent_budget_first(self) -> None:
        # Sessions the attached run finished count against the global budget, so
        # a budget already spent while attached reports `max-sessions` (its own
        # clean early stop) rather than the control-request stop.
        status = _StatusScript(
            [
                _attached_run_status(3, run_id="run-a"),
                _finished_run_status(2, run_id="run-a", reason="stopped"),
            ]
        )
        result = self._supervise(status, _never_run, parsed=self._budget_parsed(1))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "max-sessions")
        self.assertEqual(result["completed"], 1)
        self.assertEqual(result["pending"], 2)


if __name__ == "__main__":
    unittest.main()
