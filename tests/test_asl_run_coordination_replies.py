"""Test asl run coordination replies."""

from __future__ import annotations

import io
import threading
import types
import unittest
from datetime import timedelta
from pathlib import Path
from typing import Any, cast
from unittest import mock

from agent_session_loop import (
    mailbox_constants,
    run_coordination_reply_state,
    run_coordination_wait_models,
    run_coordination_wait_record,
    run_loop_terminal,
)
from agent_session_loop import run_coordination_constants as wait_budgets
from tests import support_asl_run_coordination as fixtures


class ReplyStatePreserverTests(fixtures.CoordinationTestCase):
    """An unconsumed reply attempt is rolled back; a consumed one is not."""

    def setUp(self) -> None:
        super().setUp()
        self.state_path = Path(self.tmp.name) / "state.json"
        self.state_path.write_text('{"before": true}\n', encoding="utf-8")
        self.event_path.write_text('{"event": "before"}\n', encoding="utf-8")

    def preserver(
        self, *, enabled: bool = True
    ) -> run_coordination_reply_state.ReplyStatePreserver:
        return run_coordination_reply_state.ReplyStatePreserver(
            state_path=self.state_path, event_path=self.event_path, enabled=enabled
        )

    def test_an_unconsumed_attempt_is_rolled_back(self) -> None:
        preserver = self.preserver()
        self.state_path.write_text('{"during": true}\n', encoding="utf-8")

        preserver.restore_if_unconsumed()

        self.assertFalse(preserver.consumed)
        self.assertEqual(
            self.state_path.read_text(encoding="utf-8"), '{"before": true}\n'
        )

    def test_a_consumed_attempt_is_left_alone(self) -> None:
        preserver = self.preserver()
        self.state_path.write_text('{"during": true}\n', encoding="utf-8")
        preserver.mark_consumed()

        preserver.restore_if_unconsumed()

        self.assertTrue(preserver.consumed)
        self.assertEqual(
            self.state_path.read_text(encoding="utf-8"), '{"during": true}\n'
        )

    def test_a_file_that_did_not_exist_is_removed_again(self) -> None:
        missing = Path(self.tmp.name) / "absent.json"
        preserver = run_coordination_reply_state.ReplyStatePreserver(
            state_path=missing, event_path=self.event_path, enabled=True
        )
        missing.write_text("created\n", encoding="utf-8")

        preserver.restore_if_unconsumed()

        self.assertFalse(missing.exists())

    def test_an_unconsumed_attempt_without_side_effects_is_rolled_back(
        self,
    ) -> None:
        preserver = self.preserver()
        self.state_path.write_text(
            '{"commit_groups": [], "during": true}\n', encoding="utf-8"
        )
        with self.event_path.open("a", encoding="utf-8") as handle:
            handle.write('{"name": "run_started", "payload": {}}\n')

        self.assertIsNone(preserver.restore_if_unconsumed())

        self.assertEqual(
            self.state_path.read_text(encoding="utf-8"), '{"before": true}\n'
        )
        self.assertEqual(
            self.event_path.read_text(encoding="utf-8"), '{"event": "before"}\n'
        )

    def test_a_commit_before_consumption_keeps_the_ledger(self) -> None:
        # Settling an owed group can commit before the reply is consumed. The
        # events naming that commit are the only record of it, so rolling them
        # back would leave the ledger disagreeing with Git.
        preserver = self.preserver()
        during_state = '{"commit_groups": [{"group_id": "g1"}], "during": true}\n'
        self.state_path.write_text(during_state, encoding="utf-8")
        with self.event_path.open("a", encoding="utf-8") as handle:
            handle.write('{"name": "commit_finished", "payload": {}}\n')
        during_events = self.event_path.read_text(encoding="utf-8")

        skipped = preserver.restore_if_unconsumed()

        assert skipped is not None
        self.assertEqual("side-effect-before-consumption", skipped["reason"])
        self.assertEqual(
            ["commit_finished", "state.commit_groups"], skipped["evidence"]
        )
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), during_state)
        self.assertEqual(self.event_path.read_text(encoding="utf-8"), during_events)

    def test_a_ledger_entry_carried_over_unchanged_is_not_a_side_effect(
        self,
    ) -> None:
        carried = '{"commit_failures": [{"session_id": "1"}]}\n'
        self.state_path.write_text(carried, encoding="utf-8")
        preserver = self.preserver()
        self.state_path.write_text(
            '{"commit_failures": [{"session_id": "1"}], "during": true}\n',
            encoding="utf-8",
        )

        self.assertIsNone(preserver.restore_if_unconsumed())
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), carried)

    def test_the_run_payload_and_stderr_say_why_the_rollback_was_skipped(
        self,
    ) -> None:
        preserver = self.preserver()
        with self.event_path.open("a", encoding="utf-8") as handle:
            handle.write('{"name": "commit_started", "payload": {}}\n')
        written: list[dict[str, object]] = []
        ctx = types.SimpleNamespace(
            reply_state_preserver=preserver,
            persistence=types.SimpleNamespace(
                lock=threading.Lock(), write_state=written.append
            ),
        )
        payload: dict[str, object] = {"status": "paused"}

        with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
            code, result = run_loop_terminal._finish_run_locked(
                cast(Any, ctx), 3, payload
            )

        self.assertEqual(3, code)
        skipped = cast(
            dict[str, Any], result[run_coordination_reply_state.ROLLBACK_SKIPPED_KEY]
        )
        self.assertEqual(["commit_started"], skipped["evidence"])
        self.assertEqual([payload], written)
        self.assertIn("kept rather than rolled back", stderr.getvalue())

    def test_a_disabled_preserver_captures_and_restores_nothing(self) -> None:
        preserver = self.preserver(enabled=False)
        self.state_path.write_text('{"during": true}\n', encoding="utf-8")

        preserver.restore_if_unconsumed()

        self.assertEqual(
            self.state_path.read_text(encoding="utf-8"), '{"during": true}\n'
        )


class SupervisionCursorTests(unittest.TestCase):
    """Loss, recovery, and suspend move the cursor's ratchets one way only."""

    def cursor(self, clocks: fixtures.FakeClock, **overrides: float):
        return run_coordination_wait_models.SupervisionCursor(
            settings=fixtures.settings(**overrides),
            started=clocks.monotonic(),
            monotonic=clocks.monotonic,
            wall_clock=clocks.wall,
        )

    def test_a_new_cursor_measures_from_its_own_start(self) -> None:
        clocks = fixtures.FakeClock()
        cursor = self.cursor(clocks)

        self.assertEqual(cursor.active_deadline, clocks.monotonic_value + 300.0)
        self.assertEqual(
            cursor.unsupervised_deadline(), clocks.monotonic_value + 1800.0
        )
        self.assertEqual(cursor.unsupervised_elapsed(), 0.0)

    def test_a_fresh_lease_extends_the_deadline_and_rolls_the_ceiling(self) -> None:
        clocks = fixtures.FakeClock()
        cursor = self.cursor(clocks)
        clocks.advance(300.0)

        verdict = cursor.observe_lease(
            fixtures.lease_view(fresh=True, updated_at="2026-01-01T00:00:00+00:00"),
            clocks.monotonic(),
        )

        self.assertEqual(verdict, "fresh")
        self.assertEqual(cursor.active_deadline, clocks.monotonic_value + 60.0)
        self.assertEqual(
            cursor.unsupervised_deadline(), clocks.monotonic_value + 1800.0
        )
        self.assertEqual(cursor.unsupervised_elapsed(), 0.0)

    def test_a_stale_lease_outside_any_grace_ends_the_wait(self) -> None:
        clocks = fixtures.FakeClock()
        cursor = self.cursor(clocks)
        clocks.advance(300.0)

        verdict = cursor.observe_lease(
            fixtures.lease_view(fresh=False, reason=mailbox_constants.LEASE_EXPIRED),
            clocks.monotonic(),
        )

        self.assertEqual(verdict, "lost")
        self.assertEqual(cursor.active_deadline, clocks.monotonic_value)

    def test_recovery_after_a_stale_probe_restores_the_ratchet(self) -> None:
        clocks = fixtures.FakeClock()
        cursor = self.cursor(clocks)
        clocks.advance(300.0)
        cursor.observe_lease(fixtures.lease_view(fresh=False), clocks.monotonic())
        clocks.advance(30.0)

        verdict = cursor.observe_lease(
            fixtures.lease_view(fresh=True, updated_at="2026-01-01T00:05:30+00:00"),
            clocks.monotonic(),
        )

        self.assertEqual(verdict, "fresh")
        self.assertEqual(cursor.last_fresh_lease, clocks.monotonic_value)
        self.assertEqual(
            cursor.evidence().lease_updated_at, "2026-01-01T00:05:30+00:00"
        )

    def test_an_undisturbed_clock_detects_no_suspend(self) -> None:
        clocks = fixtures.FakeClock()
        cursor = self.cursor(clocks)
        clocks.advance(120.0)

        self.assertIsNone(cursor.observe_clock())
        self.assertEqual(cursor.suspend_skew_seconds, 0.0)
        self.assertIsNone(cursor.lease_grace_until)

    def test_wall_clock_running_ahead_is_read_as_a_suspend(self) -> None:
        clocks = fixtures.FakeClock()
        cursor = self.cursor(clocks)
        ceiling_before = cursor.unsupervised_deadline()
        clocks.advance(2.0, wall_extra=600.0)

        rebased = cursor.observe_clock()

        assert rebased is not None
        base_deadline, unsupervised_deadline_at = rebased
        self.assertAlmostEqual(cursor.suspend_skew_seconds, 600.0, places=6)
        self.assertEqual(
            cursor.suspend_lease_grace_seconds,
            wait_budgets.MAILBOX_REPLY_WAIT_SUSPEND_LEASE_GRACE_SECONDS,
        )
        self.assertEqual(cursor.last_fresh_lease, clocks.monotonic_value)
        # The ceiling restarts at the wake instant: nobody could prove liveness
        # while the machine slept.
        self.assertGreater(cursor.unsupervised_deadline(), ceiling_before)
        self.assertEqual(
            base_deadline,
            clocks.wall_value
            + timedelta(seconds=cursor.active_deadline - clocks.monotonic_value),
        )
        self.assertEqual(
            unsupervised_deadline_at,
            clocks.wall_value + timedelta(seconds=1800.0),
        )

    def test_a_stale_lease_inside_the_post_suspend_grace_proves_nothing(self) -> None:
        clocks = fixtures.FakeClock()
        cursor = self.cursor(clocks)
        clocks.advance(2.0, wall_extra=600.0)
        cursor.observe_clock()
        active_before = cursor.active_deadline
        ceiling_before = cursor.unsupervised_deadline()
        clocks.advance(10.0)

        verdict = cursor.observe_lease(
            fixtures.lease_view(fresh=False), clocks.monotonic()
        )

        self.assertEqual(verdict, "grace")
        self.assertEqual(cursor.active_deadline, active_before)
        self.assertEqual(cursor.unsupervised_deadline(), ceiling_before)

    def test_a_stale_lease_after_the_grace_expires_ends_the_wait(self) -> None:
        clocks = fixtures.FakeClock()
        cursor = self.cursor(clocks)
        clocks.advance(2.0, wall_extra=600.0)
        cursor.observe_clock()
        clocks.advance(
            wait_budgets.MAILBOX_REPLY_WAIT_SUSPEND_LEASE_GRACE_SECONDS + 1.0
        )

        verdict = cursor.observe_lease(
            fixtures.lease_view(fresh=False), clocks.monotonic()
        )

        self.assertEqual(verdict, "lost")

    def test_evidence_distinguishes_no_probe_from_an_empty_lease(self) -> None:
        clocks = fixtures.FakeClock()
        cursor = self.cursor(clocks)

        unprobed = cursor.evidence()
        cursor.observe_lease(fixtures.lease_view(fresh=False), clocks.monotonic())
        probed = cursor.evidence()

        self.assertFalse(unprobed.lease_observed)
        self.assertEqual(unprobed.to_payload(), {"unsupervised_seconds": 0.0})
        self.assertTrue(probed.lease_observed)
        self.assertIn("lease_supervisor_id", probed.to_payload())
        self.assertIsNone(probed.to_payload()["lease_supervisor_id"])

    def test_a_zero_suspend_is_absence_rather_than_a_measurement(self) -> None:
        evidence = run_coordination_wait_record.LeaseEvidence.observed(
            fixtures.lease_view(fresh=True, updated_at="2026-01-01T00:00:00+00:00")
        )

        self.assertIsNone(evidence.suspend_skew_seconds)
        self.assertIsNone(evidence.suspend_lease_grace_seconds)
        self.assertNotIn("suspend_skew_seconds", evidence.to_payload())


class MailboxWaitSettingsTests(unittest.TestCase):
    """Environment overrides are a narrow seam that can only fail safe."""

    def test_the_defaults_are_the_production_budgets(self) -> None:
        resolved = run_coordination_wait_models.mailbox_reply_wait_settings({})

        self.assertEqual(
            resolved.base_seconds,
            wait_budgets.MAILBOX_REPLY_WAIT_BASE_SECONDS,
        )
        self.assertEqual(
            resolved.unsupervised_ceiling_seconds,
            wait_budgets.MAILBOX_REPLY_WAIT_UNSUPERVISED_CEILING_SECONDS,
        )

    def test_a_nonsense_override_falls_back_rather_than_raising(self) -> None:
        resolved = run_coordination_wait_models.mailbox_reply_wait_settings(
            {
                wait_budgets.MAILBOX_REPLY_WAIT_BASE_ENV_VAR: "not-a-number",
                wait_budgets.MAILBOX_REPLY_WAIT_POLL_ENV_VAR: "-1",
                wait_budgets.MAILBOX_REPLY_WAIT_IO_GRACE_ENV_VAR: "inf",
            }
        )

        self.assertEqual(
            resolved.base_seconds,
            wait_budgets.MAILBOX_REPLY_WAIT_BASE_SECONDS,
        )
        self.assertEqual(
            resolved.poll_seconds,
            wait_budgets.MAILBOX_REPLY_WAIT_POLL_SECONDS,
        )
        self.assertEqual(
            resolved.io_grace_seconds,
            wait_budgets.MAILBOX_REPLY_WAIT_IO_GRACE_SECONDS,
        )

    def test_a_base_beyond_the_ceiling_is_clamped_not_raised(self) -> None:
        resolved = run_coordination_wait_models.mailbox_reply_wait_settings(
            {
                wait_budgets.MAILBOX_REPLY_WAIT_BASE_ENV_VAR: "10000",
                wait_budgets.MAILBOX_REPLY_WAIT_UNSUPERVISED_CEILING_ENV_VAR: "60",
            }
        )

        self.assertEqual(resolved.base_seconds, 60.0)
        self.assertEqual(resolved.unsupervised_ceiling_seconds, 60.0)


if __name__ == "__main__":
    unittest.main()
