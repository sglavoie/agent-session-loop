"""Suspend grace and unsupervised-time accounting tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tests.reply_flow_support as rfs
from agent_session_loop import (
    events,
    mailbox,
    run_coordination,
    run_persistence,
)
from tests.support import (
    read_jsonl,
)


class SuspendGraceFlowTests(rfs.ReplyResumeFlowCase):
    """Suspend grace windows and unsupervised-seconds tracking."""

    def test_stale_lease_inside_suspend_grace_keeps_wait_alive(self) -> None:
        settings = run_coordination.MailboxWaitSettings(
            base_seconds=4.0,
            lease_extension_seconds=2.0,
            unsupervised_ceiling_seconds=200.0,
            poll_seconds=1.0,
            io_grace_seconds=1.0,
        )

        result, elapsed, probe_times, sleeps = self._run_suspend_grace_wait(
            settings=settings,
            reply_from=8.0,
        )

        self.assertEqual("reply", result.reason)
        self.assertIsNotNone(result.reply)
        assert result.reply is not None
        self.assertEqual(
            "00000000-0000-0000-0000-000000000045",
            result.reply.id,
        )
        self.assertEqual(8.0, elapsed)
        # Every stale probe landed inside the grace at poll cadence: none
        # ended the wait and none ratcheted the active deadline.
        self.assertEqual([4.0, 5.0, 6.0, 7.0], probe_times)
        # The reply exit payload stays evidence-free even after a suspend.
        self.assertIsNone(result.suspend_skew_seconds)
        self.assertIsNone(result.suspend_lease_grace_seconds)
        self.assertTrue(all(seconds > 0.0 for seconds in sleeps), sleeps)

    def test_stale_lease_after_grace_lapse_exits_supervisor_lost(
        self,
    ) -> None:
        settings = run_coordination.MailboxWaitSettings(
            base_seconds=4.0,
            lease_extension_seconds=2.0,
            unsupervised_ceiling_seconds=200.0,
            poll_seconds=1.0,
            io_grace_seconds=1.0,
        )

        result, elapsed, probe_times, sleeps = self._run_suspend_grace_wait(
            settings=settings,
        )

        self.assertEqual(
            run_coordination.MAILBOX_REPLY_WAIT_REASON_SUPERVISOR_LOST,
            result.reason,
        )
        # Detection fired at monotonic 1.0, so the grace covered probes up
        # to 91.0; the first probe at that bound exits immediately — no
        # ratchet ever delayed it, and the grace poll pacing never spun.
        self.assertEqual(91.0, elapsed)
        self.assertEqual(4.0, probe_times[0])
        self.assertEqual(91.0, probe_times[-1])
        self.assertEqual(mailbox.LEASE_EXPIRED, result.lease_reason)
        self.assertEqual(
            "00000000-0000-0000-0000-000000000046",
            result.lease_supervisor_id,
        )
        self.assertEqual(3600.0, result.suspend_skew_seconds)
        self.assertEqual(
            run_coordination.MAILBOX_REPLY_WAIT_SUSPEND_LEASE_GRACE_SECONDS,
            result.suspend_lease_grace_seconds,
        )
        self.assertTrue(all(seconds > 0.0 for seconds in sleeps), sleeps)

        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / events.EVENTS_FILE_NAME
            run_coordination.clear_waiting_for_reply(
                event_path,
                {
                    "run_id": "run-1",
                    "waiting_for_reply": {
                        "question_id": ("00000000-0000-0000-0000-000000000044"),
                        "session_id": "9",
                        "turn_index": 3,
                    },
                },
                reason=result.reason,
                pending_outbox=result.pending_outbox,
                lease_updated_at=result.lease_updated_at,
                lease_reason=result.lease_reason,
                lease_supervisor_id=result.lease_supervisor_id,
                suspend_skew_seconds=result.suspend_skew_seconds,
                suspend_lease_grace_seconds=(result.suspend_lease_grace_seconds),
                persistence=run_persistence._RunPersistence(write_state=False),
            )
            [cleared_event] = read_jsonl(event_path)
        self.assertEqual(
            run_coordination.MAILBOX_REPLY_WAIT_SUSPEND_LEASE_GRACE_SECONDS,
            cleared_event["payload"]["suspend_lease_grace_seconds"],
        )
        self.assertEqual(
            3600.0,
            cleared_event["payload"]["suspend_skew_seconds"],
        )

    def test_lease_turning_fresh_inside_grace_resumes_ratcheting(
        self,
    ) -> None:
        settings = run_coordination.MailboxWaitSettings(
            base_seconds=4.0,
            lease_extension_seconds=2.0,
            unsupervised_ceiling_seconds=20.0,
            poll_seconds=1.0,
            io_grace_seconds=1.0,
        )

        result, elapsed, probe_times, sleeps = self._run_suspend_grace_wait(
            settings=settings,
            lease_fresh_from=8.0,
            lease_stale_from=100.0,
        )

        self.assertEqual(
            run_coordination.MAILBOX_REPLY_WAIT_REASON_SUPERVISOR_LOST,
            result.reason,
        )
        # Stale probes inside the grace stay on the poll cadence; once the
        # lease turns fresh the active-deadline ratchet resumes, spaces probes
        # by the lease extension again, and rolls the ceiling with it — the
        # wait outlives five ceilings and ends only when freshness stops.
        self.assertEqual(
            [4.0, 5.0, 6.0, 7.0, 8.0, 10.0, 12.0, 14.0, 16.0, 18.0],
            probe_times[:10],
        )
        self.assertEqual(100.0, elapsed)
        self.assertGreater(elapsed, settings.unsupervised_ceiling_seconds)
        # Only the final probe rejected the lease, so the wait spent one
        # extension unsupervised however long it ran.
        self.assertEqual(mailbox.LEASE_EXPIRED, result.lease_reason)
        self.assertEqual(
            settings.lease_extension_seconds,
            result.unsupervised_seconds,
        )
        self.assertEqual(
            "00000000-0000-0000-0000-000000000046",
            result.lease_supervisor_id,
        )
        self.assertEqual(3600.0, result.suspend_skew_seconds)
        self.assertEqual(
            run_coordination.MAILBOX_REPLY_WAIT_SUSPEND_LEASE_GRACE_SECONDS,
            result.suspend_lease_grace_seconds,
        )
        self.assertTrue(all(seconds > 0.0 for seconds in sleeps), sleeps)

    def test_unsupervised_ceiling_fires_despite_active_grace(self) -> None:
        settings = run_coordination.MailboxWaitSettings(
            base_seconds=4.0,
            lease_extension_seconds=2.0,
            unsupervised_ceiling_seconds=20.0,
            poll_seconds=1.0,
            io_grace_seconds=1.0,
        )

        result, elapsed, probe_times, sleeps = self._run_suspend_grace_wait(
            settings=settings,
        )

        # The grace ran to monotonic 91.0, far past the 20-second ceiling, and
        # no probe inside it ever proved a supervisor. The ceiling therefore
        # still ends the wait, measured from the 1.0 wake: a grace can suppress
        # the supervisor-lost exit but never make an unwatched wait unbounded.
        self.assertEqual("timeout", result.reason)
        self.assertEqual(1.0 + settings.unsupervised_ceiling_seconds, elapsed)
        self.assertEqual(4.0, probe_times[0])
        self.assertEqual(20.0, probe_times[-1])
        self.assertEqual(
            settings.unsupervised_ceiling_seconds,
            result.unsupervised_seconds,
        )
        self.assertEqual(mailbox.LEASE_EXPIRED, result.lease_reason)
        self.assertEqual(3600.0, result.suspend_skew_seconds)
        self.assertEqual(
            run_coordination.MAILBOX_REPLY_WAIT_SUSPEND_LEASE_GRACE_SECONDS,
            result.suspend_lease_grace_seconds,
        )
        self.assertTrue(all(seconds > 0.0 for seconds in sleeps), sleeps)

    def test_unsupervised_seconds_tracks_freshness_not_wait_length(
        self,
    ) -> None:
        settings = run_coordination.MailboxWaitSettings(
            base_seconds=4.0,
            lease_extension_seconds=2.0,
            unsupervised_ceiling_seconds=20.0,
            poll_seconds=1.0,
            io_grace_seconds=1.0,
        )

        stale, stale_elapsed, stale_probes, _ = self._run_suspend_grace_wait(
            settings=settings,
        )
        fresh, fresh_elapsed, fresh_probes, _ = self._run_suspend_grace_wait(
            settings=settings,
            lease_fresh_from=8.0,
            lease_stale_from=100.0,
        )

        # Stale run: every probe from 4.0 to 20.0 proved nothing, so the
        # tracked instant froze at the 1.0 wake and the ceiling ended the
        # wait one full unsupervised stretch later.
        self.assertEqual("timeout", stale.reason)
        self.assertEqual(21.0, stale_elapsed)
        self.assertEqual(20.0, stale_probes[-1])
        self.assertEqual(20.0, stale.unsupervised_seconds)

        # Fresh run: nearly five times longer on the clock, yet freshness
        # advanced the tracked instant with every probe, so its unsupervised
        # stretch is an order of magnitude *smaller*. Wall-clock length and
        # unsupervised length are now genuinely different measurements.
        self.assertEqual(
            run_coordination.MAILBOX_REPLY_WAIT_REASON_SUPERVISOR_LOST,
            fresh.reason,
        )
        self.assertEqual(100.0, fresh_elapsed)
        self.assertEqual(100.0, fresh_probes[-1])
        self.assertEqual(2.0, fresh.unsupervised_seconds)
        self.assertGreater(fresh_elapsed, stale_elapsed)
        self.assertLess(fresh.unsupervised_seconds, stale.unsupervised_seconds)

    def test_unsupervised_seconds_excludes_suspended_time(self) -> None:
        settings = run_coordination.MailboxWaitSettings(
            base_seconds=4.0,
            lease_extension_seconds=2.0,
            unsupervised_ceiling_seconds=200.0,
            poll_seconds=1.0,
            io_grace_seconds=1.0,
        )

        result, elapsed, _, _ = self._run_suspend_grace_wait(
            settings=settings,
        )

        self.assertEqual(
            run_coordination.MAILBOX_REPLY_WAIT_REASON_SUPERVISOR_LOST,
            result.reason,
        )
        # An hour of sleeping machine is not an hour of lost supervision:
        # the tracked instant re-anchors at the wake, so the unsupervised
        # stretch is exactly the awake grace the stale lease then burned.
        self.assertEqual(3600.0, result.suspend_skew_seconds)
        self.assertEqual(91.0, elapsed)
        self.assertEqual(
            run_coordination.MAILBOX_REPLY_WAIT_SUSPEND_LEASE_GRACE_SECONDS,
            result.unsupervised_seconds,
        )
        self.assertLess(result.unsupervised_seconds, elapsed)

    def test_wait_cleared_carries_unsupervised_seconds_when_tracked(
        self,
    ) -> None:
        question_id = "00000000-0000-0000-0000-000000000047"
        question = mailbox.MailboxMessage(
            seq=1,
            id=question_id,
            kind=mailbox.KIND_QUESTION,
            created_at="2026-07-24T12:00:00+00:00",
            text="Which option?",
        )
        view = mailbox.MailboxView(
            mailbox_path=Path("/tmp/mailbox.jsonl"),
            messages=(question,),
            last_seq=1,
        )
        lease_record = mailbox.MailboxLease(
            supervisor_id="00000000-0000-0000-0000-000000000048",
            updated_at="2026-07-24T12:00:00+00:00",
        )
        lease_path = Path("/tmp/mailbox-lease.json")
        fresh_lease = mailbox.MailboxLeaseView(
            lease_path=lease_path,
            lease=lease_record,
            fresh=True,
        )
        stale_lease = mailbox.MailboxLeaseView(
            lease_path=lease_path,
            lease=lease_record,
            fresh=False,
            reason=mailbox.LEASE_EXPIRED,
        )
        settings = run_coordination.MailboxWaitSettings(
            base_seconds=4.0,
            lease_extension_seconds=2.0,
            unsupervised_ceiling_seconds=20.0,
            poll_seconds=1.0,
            io_grace_seconds=1.0,
        )
        supervision_ends = 40.0
        now = [0.0]

        def sleep(seconds: float) -> None:
            now[0] += seconds

        def read_lease(*_args: object, **_kwargs: object) -> object:
            if now[0] >= supervision_ends:
                return stale_lease
            return fresh_lease

        with (
            mock.patch.object(
                mailbox,
                "mailbox_change_token",
                return_value=(1, 2, 3, 4),
            ),
            mock.patch.object(
                mailbox,
                "read_mailbox",
                return_value=view,
            ),
            mock.patch.object(
                mailbox,
                "read_lease",
                side_effect=read_lease,
            ),
        ):
            result = run_coordination.wait_for_mailbox_reply(
                plan_dir=Path("/tmp/plan"),
                state_dir_arg=None,
                payload={},
                question_id=question_id,
                run_id="RUN-TEST",
                settings=settings,
                persistence=run_persistence._RunPersistence(
                    write_state=False,
                    append_events=False,
                ),
                started_monotonic=0.0,
                monotonic=lambda: now[0],
                sleep=sleep,
            )

        # No suspend here: an unbroken run of fresh probes carried the wait
        # twice past the ceiling, so only the final extension went unproved.
        self.assertEqual(
            run_coordination.MAILBOX_REPLY_WAIT_REASON_SUPERVISOR_LOST,
            result.reason,
        )
        self.assertEqual(supervision_ends, now[0])
        self.assertGreater(now[0], settings.unsupervised_ceiling_seconds)
        self.assertEqual(
            settings.lease_extension_seconds,
            result.unsupervised_seconds,
        )

        waiting = {
            "question_id": question_id,
            "session_id": "9",
            "turn_index": 3,
        }
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / events.EVENTS_FILE_NAME
            run_coordination.clear_waiting_for_reply(
                event_path,
                {"run_id": "run-1", "waiting_for_reply": dict(waiting)},
                reason=result.reason,
                pending_outbox=result.pending_outbox,
                lease_updated_at=result.lease_updated_at,
                lease_reason=result.lease_reason,
                lease_supervisor_id=result.lease_supervisor_id,
                unsupervised_seconds=result.unsupervised_seconds,
                persistence=run_persistence._RunPersistence(write_state=False),
            )
            run_coordination.clear_waiting_for_reply(
                event_path,
                {"run_id": "run-1", "waiting_for_reply": dict(waiting)},
                reason="reply",
                pending_outbox=0,
                persistence=run_persistence._RunPersistence(write_state=False),
            )
            tracked_event, untracked_event = read_jsonl(event_path)

        self.assertEqual(
            settings.lease_extension_seconds,
            tracked_event["payload"]["unsupervised_seconds"],
        )
        # The key stays additive: a closure exit that tracked nothing keeps
        # its payload exactly as it was.
        self.assertNotIn("unsupervised_seconds", untracked_event["payload"])


if __name__ == "__main__":
    unittest.main()
