"""Mailbox wait preparation failures and deadline extension tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tests.reply_flow_support as rfs
from agent_session_loop import (
    events,
    harnesses,
    mailbox,
    mailbox_appenders,
    mailbox_constants,
    mailbox_leases,
    mailbox_models,
    mailbox_snapshot,
    mailbox_vocabulary,
    run_coordination,
    run_persistence,
    state,
)
from tests.support import (
    init_git_repo,
    read_jsonl,
    write_plan,
)


class MailboxWaitExtensionTests(rfs.ReplyResumeFlowCase):
    """Stale leases, monotonic jumps, and wait ceilings."""

    def test_mailbox_with_stale_lease_records_wait_preparation_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            init_git_repo(tmp_dir)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            write_plan(plan_dir, ("pending",))
            mailbox_appenders.append_guidance(
                plan_dir,
                "Supervision is active.",
                state_dir=state_dir,
            )
            mailbox_leases.write_lease(
                plan_dir,
                "00000000-0000-0000-0000-000000000002",
                state_dir=state_dir,
                updated_at="2000-01-01T00:00:00.000000+00:00",
            )
            pause_message = "blocked - need operator answer"

            def fake_run_harness(name, **_kwargs):
                return harnesses.HarnessResult(
                    name=name,
                    argv=[name],
                    returncode=0,
                    stdout="waiting\n",
                    stderr="",
                    last_message=(
                        f"Choose option 1a or 1b.\nRUNNER_PAUSE: {pause_message}\n"
                    ),
                )

            args = self._build_args(
                plan_dir,
                state_dir,
                reply=None,
                cwd=tmp_dir,
            )
            with (
                mock.patch.object(
                    harnesses,
                    "run_harness",
                    side_effect=fake_run_harness,
                ),
                mock.patch.object(
                    run_coordination,
                    "wait_for_mailbox_reply",
                    side_effect=AssertionError(
                        "mailbox wait entered without a fresh lease"
                    ),
                ) as wait_for_reply,
                mock.patch.object(
                    run_persistence, "_notify_agent_mux_loop_paused"
                ) as notify_paused,
            ):
                code, out, _err = self._run_capture(args)

            self.assertEqual(1, code, out)
            wait_for_reply.assert_not_called()
            payload = json.loads(out)
            self.assertTrue(payload["paused"])
            self.assertEqual("blocked", payload["pause_reason"])
            self.assertEqual(pause_message, payload["pause_message"])
            self.assertNotIn("waiting_for_reply", payload)
            notify_paused.assert_called_once()

            plan_state = state.plan_state_dir(plan_dir, state_dir)
            records = read_jsonl(plan_state / events.EVENTS_FILE_NAME)
            pause_events = [
                record
                for record in records
                if record.get("name") == events.RUN_PAUSED_EVENT
            ]
            wait_events = [
                record
                for record in records
                if record.get("name") == events.MAILBOX_EVENT
                and record.get("payload", {}).get("action")
                == events.MAILBOX_ACTION_WAIT_STARTED
            ]
            cleared_events = [
                record
                for record in records
                if record.get("name") == events.MAILBOX_EVENT
                and record.get("payload", {}).get("action")
                == events.MAILBOX_ACTION_WAIT_CLEARED
            ]
            preparation_failed_events = [
                record
                for record in records
                if record.get("name") == events.MAILBOX_EVENT
                and record.get("payload", {}).get("action")
                == events.MAILBOX_ACTION_WAIT_PREPARATION_FAILED
            ]
            self.assertEqual([], wait_events, records)
            self.assertEqual([], cleared_events, records)
            self.assertEqual(1, len(preparation_failed_events), records)
            preparation_payload = preparation_failed_events[0]["payload"]
            self.assertEqual(
                mailbox_constants.LEASE_EXPIRED, preparation_payload["reason"]
            )
            self.assertEqual("1", preparation_payload["session_id"])
            self.assertEqual(1, preparation_payload["session_number"])
            self.assertEqual(1, preparation_payload["turn_index"])
            self.assertEqual(1, len(pause_events), records)
            self.assertEqual("blocked", pause_events[0]["payload"]["reason"])
            self.assertEqual(
                pause_message,
                pause_events[0]["payload"]["message"],
            )
            view = mailbox.read_mailbox(plan_dir, state_dir=state_dir)
            self.assertEqual([], list(view.unanswered_questions))

    def test_mailbox_reply_wait_extension_survives_monotonic_jump_without_spin(
        self,
    ) -> None:
        question_id = "00000000-0000-0000-0000-000000000014"
        question = mailbox_models.MailboxMessage(
            seq=1,
            id=question_id,
            kind=mailbox_vocabulary.KIND_QUESTION,
            created_at="2026-07-24T12:00:00+00:00",
            text="Which option?",
        )
        view = mailbox_snapshot.MailboxView(
            mailbox_path=Path("/tmp/mailbox.jsonl"),
            messages=(question,),
            last_seq=1,
        )
        lease_record = mailbox_models.MailboxLease(
            supervisor_id="00000000-0000-0000-0000-000000000015",
            updated_at="2026-07-24T12:00:00+00:00",
        )
        lease_path = Path("/tmp/mailbox-lease.json")
        fresh_lease = mailbox_models.MailboxLeaseView(
            lease_path=lease_path,
            lease=lease_record,
            fresh=True,
        )
        stale_lease = mailbox_models.MailboxLeaseView(
            lease_path=lease_path,
            lease=lease_record,
            fresh=False,
            reason=mailbox_constants.LEASE_EXPIRED,
        )
        settings = run_coordination.MailboxWaitSettings(
            base_seconds=4.0,
            lease_extension_seconds=2.0,
            unsupervised_ceiling_seconds=20.0,
            poll_seconds=1.0,
            io_grace_seconds=1.0,
        )
        supervision_ends = 60.0
        now = [12.0]
        sleeps: list[float] = []
        read_times: list[float] = []

        def read_mailbox(*_args, **_kwargs):
            read_times.append(now[0])
            return view

        def read_lease(*_args, **_kwargs):
            if now[0] >= supervision_ends:
                return stale_lease
            return fresh_lease

        def monotonic() -> float:
            return now[0]

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            now[0] += seconds

        with (
            mock.patch.object(
                mailbox,
                "mailbox_change_token",
                return_value=(1, 2, 3, 4),
            ),
            mock.patch.object(
                mailbox,
                "read_mailbox",
                side_effect=read_mailbox,
            ),
            mock.patch.object(
                mailbox_leases,
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
                monotonic=monotonic,
                sleep=sleep,
            )

        self.assertIsNone(result.reply)
        self.assertEqual(
            run_coordination.MAILBOX_REPLY_WAIT_REASON_SUPERVISOR_LOST,
            result.reason,
        )
        self.assertTrue(sleeps)
        self.assertTrue(all(seconds > 0.0 for seconds in sleeps), sleeps)
        self.assertEqual(1, len(read_times))
        # The clock started past the base deadline, so the very first probe
        # ratchets. Freshness then carried the wait three ceilings deep before
        # the lease lapsed — a wall-clock bound would have ended it at 20.
        self.assertEqual(supervision_ends, now[0])
        self.assertGreater(now[0], settings.unsupervised_ceiling_seconds)

    def test_mailbox_reply_wait_production_base_and_unsupervised_ceiling(
        self,
    ) -> None:
        question_id = "00000000-0000-0000-0000-000000000019"
        question = mailbox_models.MailboxMessage(
            seq=1,
            id=question_id,
            kind=mailbox_vocabulary.KIND_QUESTION,
            created_at="2026-07-24T12:00:00+00:00",
            text="Which option?",
        )
        view = mailbox_snapshot.MailboxView(
            mailbox_path=Path("/tmp/mailbox.jsonl"),
            messages=(question,),
            last_seq=1,
        )
        lease_record = mailbox_models.MailboxLease(
            supervisor_id="00000000-0000-0000-0000-000000000020",
            updated_at="2026-07-24T12:00:00+00:00",
        )
        fresh_lease = mailbox_models.MailboxLeaseView(
            lease_path=Path("/tmp/mailbox-lease.json"),
            lease=lease_record,
            fresh=True,
        )
        stale_lease = mailbox_models.MailboxLeaseView(
            lease_path=Path("/tmp/mailbox-lease.json"),
            lease=lease_record,
            fresh=False,
            reason=mailbox_constants.LEASE_EXPIRED,
        )
        settings = run_coordination.mailbox_reply_wait_settings({})
        self.assertEqual(
            run_coordination.MAILBOX_REPLY_WAIT_BASE_SECONDS,
            settings.base_seconds,
        )
        self.assertEqual(
            run_coordination.MAILBOX_REPLY_WAIT_LEASE_EXTENSION_SECONDS,
            settings.lease_extension_seconds,
        )
        self.assertEqual(
            run_coordination.MAILBOX_REPLY_WAIT_UNSUPERVISED_CEILING_SECONDS,
            settings.unsupervised_ceiling_seconds,
        )
        self.assertEqual(
            run_coordination.MAILBOX_REPLY_WAIT_POLL_SECONDS,
            settings.poll_seconds,
        )
        # Four times the ceiling: long enough that any wall-clock bound would
        # have fired three times over.
        supervision_ends = 4 * settings.unsupervised_ceiling_seconds

        def run_wait(
            lease: mailbox_models.MailboxLeaseView,
            *,
            until: float | None = None,
        ) -> tuple[run_coordination.MailboxWaitResult, float, int]:
            now = [0.0]

            def monotonic() -> float:
                return now[0]

            def sleep(seconds: float) -> None:
                self.assertGreater(seconds, 0.0)
                now[0] += seconds

            def read_lease_seam(*_args: object, **_kwargs: object) -> object:
                if until is not None and now[0] >= until:
                    return stale_lease
                return lease

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
                    mailbox_leases,
                    "read_lease",
                    side_effect=read_lease_seam,
                ) as read_lease,
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
                    monotonic=monotonic,
                    sleep=sleep,
                )
            return result, now[0], read_lease.call_count

        fresh_result, fresh_elapsed, fresh_checks = run_wait(
            fresh_lease,
            until=supervision_ends,
        )
        stale_result, stale_elapsed, stale_checks = run_wait(stale_lease)

        self.assertIsNone(fresh_result.reply)
        # A continuously proved supervisor has no terminal instant: the wait
        # ran two hours, four times the ceiling, and ended only when the
        # supervisor stopped proving liveness.
        self.assertEqual(
            run_coordination.MAILBOX_REPLY_WAIT_REASON_SUPERVISOR_LOST,
            fresh_result.reason,
        )
        self.assertEqual(supervision_ends, fresh_elapsed)
        self.assertGreater(
            fresh_elapsed,
            settings.unsupervised_ceiling_seconds,
        )
        self.assertEqual(
            settings.lease_extension_seconds,
            fresh_result.unsupervised_seconds,
        )
        # The first probe fires at the base deadline; each fresh lease then
        # buys one extension, so the ratchet spans the whole supervised run.
        self.assertEqual(
            1
            + int(
                (supervision_ends - settings.base_seconds)
                // settings.lease_extension_seconds
            ),
            fresh_checks,
        )
        self.assertIsNone(stale_result.reply)
        self.assertEqual("supervisor-lost", stale_result.reason)
        self.assertEqual(settings.base_seconds, stale_elapsed)
        self.assertEqual(1, stale_checks)
        # Both waits end the same way, but the unsupervised stretch separates
        # a supervisor that watched to the end from one that was never there.
        self.assertEqual(settings.base_seconds, stale_result.unsupervised_seconds)
        self.assertLess(
            fresh_result.unsupervised_seconds,
            stale_result.unsupervised_seconds,
        )

        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / events.EVENTS_FILE_NAME
            payload = {
                "run_id": "run-1",
                "waiting_for_reply": {
                    "question_id": question_id,
                    "session_id": "9",
                    "turn_index": 3,
                },
            }
            run_coordination.clear_waiting_for_reply(
                event_path,
                payload,
                reason=stale_result.reason,
                pending_outbox=stale_result.pending_outbox,
                persistence=run_persistence._RunPersistence(write_state=False),
            )
            [cleared_event] = read_jsonl(event_path)

        self.assertEqual(
            events.MAILBOX_ACTION_WAIT_CLEARED,
            cleared_event["payload"]["action"],
        )
        self.assertEqual(
            "supervisor-lost",
            cleared_event["payload"]["reason"],
        )


if __name__ == "__main__":
    unittest.main()
