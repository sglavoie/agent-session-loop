#!/usr/bin/env python3
"""Tests for the attached mailbox watcher and deferred termination signals."""

from __future__ import annotations

import os
import shutil
import signal
import tempfile
import unittest
from contextlib import ExitStack

from asl_autopilot_test_payloads import (
    ATTACHED_RUN_ID,
    RUN_STARTED_AT,
    _attach_status,
    _status_payload,
)
from asl_autopilot_test_support import (  # noqa: F401
    _counter_clock,
    _ManualClock,
    _never_run,
    _StatusScript,
    autopilot,
    setUpModule,
    tearDownModule,
)

from agent_session_loop import mailbox_vocabulary


class AttachedMailboxWatcherTest(unittest.TestCase):
    """Attach supervision shares the durable watcher without replaying history."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="asl-autopilot-attached-mailbox-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.state_path = os.path.join(self.tmp, "state", "plan-hash")
        self.mailbox_path = os.path.join(self.state_path, "mailbox.jsonl")
        self.lease_path = os.path.join(self.state_path, "mailbox-lease.json")

    def _attach_status(self, *, run_id=ATTACHED_RUN_ID):
        status = _attach_status(10)
        status["runner_state"].update(
            {
                "present": True,
                "error": None,
                "run_id": run_id,
                "run_started_at": RUN_STARTED_AT,
                "run_finished_at": None,
            }
        )
        status.update(
            {
                "state_dir": self.state_path,
                "mailbox": {
                    "path": self.mailbox_path,
                    "lease_path": self.lease_path,
                    "last_seq": 3,
                    "unanswered_questions": 1,
                    "open_questions": 1,
                },
            }
        )
        return status

    def test_attach_delivers_preexisting_unanswered_question_once(self) -> None:
        clock = _ManualClock()
        answered = autopilot._mailbox.MailboxMessage(
            seq=1,
            id="00000000-0000-0000-0000-000000000010",
            kind=mailbox_vocabulary.KIND_QUESTION,
            created_at="2026-07-24T18:59:00+00:00",
            text="answered before this supervisor attached",
            run_id=ATTACHED_RUN_ID,
        )
        reply = autopilot._mailbox.MailboxMessage(
            seq=2,
            id="00000000-0000-0000-0000-000000000011",
            kind=mailbox_vocabulary.KIND_REPLY,
            created_at="2026-07-24T18:59:01+00:00",
            text="already handled",
            to=answered.id,
        )
        unanswered = autopilot._mailbox.MailboxMessage(
            seq=3,
            id="00000000-0000-0000-0000-000000000012",
            kind=mailbox_vocabulary.KIND_QUESTION,
            created_at="2026-07-24T19:00:00+00:00",
            text="unanswered before this supervisor attached",
            run_id=ATTACHED_RUN_ID,
        )
        view = autopilot._mailbox.MailboxView(
            mailbox_path=autopilot.Path(self.mailbox_path),
            messages=(answered, reply, unanswered),
            last_seq=3,
        )
        lease_writes = []
        created = []

        def factory(status, **kwargs):
            watcher = autopilot._MailboxWatcher.from_status(
                status,
                **kwargs,
                read_mailbox=lambda *_args, **_kwargs: view,
                write_lease=lambda *args, **kw: lease_writes.append((args, kw)),
                supervisor_id="00000000-0000-0000-0000-000000000099",
            )
            created.append(watcher)
            return watcher

        attach = self._attach_status()
        status = _StatusScript([attach] * 7 + [_status_payload(0)])
        beats: list[str] = []
        result = autopilot.supervise_run(
            {
                "plan_dir": "/tmp/plan",
                "argv": ["asl", "run", "/tmp/plan", "--json"],
            },
            status_invoker=status,
            run_invoker=_never_run,
            clock=clock,
            sleep=clock.advance,
            heartbeat=beats.append,
            poll_interval=5.0,
            heartbeat_interval=30.0,
            mailbox_watcher_factory=factory,
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "complete")
        self.assertEqual(len(created), 1)
        watcher = created[0]
        self.assertIsNotNone(watcher)
        assert watcher is not None
        self.assertFalse(watcher.enabled)
        self.assertGreaterEqual(len(lease_writes), 2)
        question_beats = [beat for beat in beats if beat.startswith("QUESTION ")]
        self.assertEqual(len(question_beats), 1)
        self.assertIn(unanswered.id, question_beats[0])
        self.assertNotIn(answered.id, question_beats[0])
        self.assertEqual(result["outbox"]["since"], 3)
        self.assertEqual(result["outbox"]["next_since"], 3)
        self.assertEqual(
            [question["id"] for question in result["outbox"]["questions"]],
            [unanswered.id],
        )
        self.assertIn("pending outbox 1", "\n".join(beats))

    def test_attach_never_resurfaces_a_question_an_earlier_run_abandoned(
        self,
    ) -> None:
        clock = _ManualClock()
        orphan = autopilot._mailbox.MailboxMessage(
            seq=3,
            id="00000000-0000-0000-0000-000000000012",
            kind=mailbox_vocabulary.KIND_QUESTION,
            created_at="2026-07-24T05:00:00+00:00",
            text="wrangler auth expired",
            run_id="run-that-paused-14-hours-ago",
        )
        view = autopilot._mailbox.MailboxView(
            mailbox_path=autopilot.Path(self.mailbox_path),
            messages=(orphan,),
            last_seq=3,
        )
        lease_writes: list[dict] = []
        created = []

        def factory(status, **kwargs):
            watcher = autopilot._MailboxWatcher.from_status(
                status,
                **kwargs,
                read_mailbox=lambda *_args, **_kwargs: view,
                write_lease=(
                    lambda *_args, **projection: lease_writes.append(projection)
                ),
                supervisor_id="00000000-0000-0000-0000-000000000099",
            )
            created.append(watcher)
            return watcher

        attach = self._attach_status()
        status = _StatusScript([attach] * 7 + [_status_payload(0)])
        beats: list[str] = []
        result = autopilot.supervise_run(
            {
                "plan_dir": "/tmp/plan",
                "argv": ["asl", "run", "/tmp/plan", "--json"],
            },
            status_invoker=status,
            run_invoker=_never_run,
            clock=clock,
            sleep=clock.advance,
            heartbeat=beats.append,
            poll_interval=5.0,
            heartbeat_interval=30.0,
            mailbox_watcher_factory=factory,
        )

        self.assertTrue(result["ok"], result)
        watcher = created[0]
        assert watcher is not None
        self.assertEqual(watcher.run_id, ATTACHED_RUN_ID)
        self.assertEqual([], [beat for beat in beats if beat.startswith("QUESTION ")])
        self.assertEqual([], result["outbox"]["questions"])
        projections = [write for write in lease_writes if "run_id" in write]
        self.assertTrue(projections)
        # The reducer generation stays at the attach cursor: a question the
        # previous run abandoned is never a pending-set delta.
        self.assertTrue(
            all(
                write["unanswered_questions"] == 0
                and write["projection_seq"] == 3
                and "latest_unanswered_question_id" not in write
                for write in projections
            ),
            projections,
        )

    def test_attach_adopts_the_backlog_once_run_id_binds_late(self) -> None:
        clock = _ManualClock()
        notice = autopilot._mailbox.MailboxMessage(
            seq=2,
            id="00000000-0000-0000-0000-000000000011",
            kind=mailbox_vocabulary.KIND_NOTICE,
            created_at="2026-07-24T18:59:00+00:00",
            text="notice emitted before this supervisor attached",
            run_id=ATTACHED_RUN_ID,
            session_id="6",
            turn_index=1,
        )
        question = autopilot._mailbox.MailboxMessage(
            seq=3,
            id="00000000-0000-0000-0000-000000000012",
            kind=mailbox_vocabulary.KIND_QUESTION,
            created_at="2026-07-24T19:00:00+00:00",
            text="unanswered before this supervisor attached",
            run_id=ATTACHED_RUN_ID,
        )
        view = autopilot._mailbox.MailboxView(
            mailbox_path=autopilot.Path(self.mailbox_path),
            messages=(notice, question),
            last_seq=3,
        )
        created = []

        def factory(status, **kwargs):
            watcher = autopilot._MailboxWatcher.from_status(
                status,
                **kwargs,
                read_mailbox=lambda *_args, **_kwargs: view,
                write_lease=lambda *_args, **_kw: None,
                supervisor_id="00000000-0000-0000-0000-000000000099",
            )
            created.append(watcher)
            return watcher

        # The first probe carries no run id, so the backlog must stay armed.
        unbound = self._attach_status()
        unbound["runner_state"].pop("run_id")
        bound = self._attach_status()
        status = _StatusScript([unbound] + [bound] * 6 + [_status_payload(0)])
        beats: list[str] = []
        result = autopilot.supervise_run(
            {
                "plan_dir": "/tmp/plan",
                "argv": ["asl", "run", "/tmp/plan", "--json"],
            },
            status_invoker=status,
            run_invoker=_never_run,
            clock=clock,
            sleep=clock.advance,
            heartbeat=beats.append,
            poll_interval=5.0,
            heartbeat_interval=30.0,
            mailbox_watcher_factory=factory,
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(created[0].run_id, ATTACHED_RUN_ID)
        self.assertEqual(
            [item["id"] for item in result["outbox"]["questions"]],
            [question.id],
        )
        self.assertEqual(
            1, len([beat for beat in beats if beat.startswith("QUESTION ")])
        )
        notice_beats = [beat for beat in beats if beat.startswith("NOTICE ")]
        self.assertEqual(1, len(notice_beats))
        self.assertIn(notice.id, notice_beats[0])
        self.assertEqual(
            [item["id"] for item in result["outbox"]["notices"]],
            [notice.id],
        )

    def test_attach_notice_replays_once_per_supervisor_process(self) -> None:
        live = autopilot._mailbox.MailboxMessage(
            seq=2,
            id="00000000-0000-0000-0000-000000000012",
            kind=mailbox_vocabulary.KIND_NOTICE,
            created_at="2026-07-24T19:00:00+00:00",
            text="live run notice",
            run_id=ATTACHED_RUN_ID,
            session_id="6",
            turn_index=1,
        )
        stale = autopilot._mailbox.MailboxMessage(
            seq=1,
            id="00000000-0000-0000-0000-000000000011",
            kind=mailbox_vocabulary.KIND_NOTICE,
            created_at="2026-07-24T18:00:00+00:00",
            text="stale run notice",
            run_id="old-run",
            session_id="5",
            turn_index=2,
        )
        view = autopilot._mailbox.MailboxView(
            mailbox_path=autopilot.Path(self.mailbox_path),
            messages=(stale, live),
            last_seq=3,
        )

        def supervise_once() -> tuple[list[str], dict]:
            watcher = autopilot._MailboxWatcher.from_status(
                self._attach_status(),
                clock=lambda: 0.0,
                read_mailbox=lambda *_args, **_kwargs: view,
                write_lease=lambda *_args, **_kwargs: None,
                include_unanswered_backlog=True,
            )
            self.assertIsNotNone(watcher)
            assert watcher is not None
            watcher.establish(now=0.0)
            watcher.set_run_id(ATTACHED_RUN_ID, now=0.0)
            progress: list[str] = []
            autopilot._poll_mailbox_watcher(
                watcher,
                now=0.0,
                heartbeat=progress.append,
            )
            autopilot._poll_mailbox_watcher(
                watcher,
                now=5.0,
                heartbeat=progress.append,
            )
            return progress, watcher.outbox_payload()

        first_progress, first_outbox = supervise_once()
        second_progress, second_outbox = supervise_once()

        for progress, outbox in (
            (first_progress, first_outbox),
            (second_progress, second_outbox),
        ):
            notice_records = [
                record for record in progress if record.startswith("NOTICE ")
            ]
            self.assertEqual(len(notice_records), 1)
            self.assertIn(live.id, notice_records[0])
            self.assertEqual(
                [item["id"] for item in outbox["notices"]],
                [live.id],
            )

    def test_no_mailbox_attach_never_constructs_a_watcher(self) -> None:
        def fail_factory(*_args, **_kwargs):  # pragma: no cover - guard
            raise AssertionError("disabled mailbox must not construct a watcher")

        result = autopilot.supervise_run(
            {
                "plan_dir": "/tmp/plan",
                "argv": [
                    "asl",
                    "run",
                    "/tmp/plan",
                    "--no-mailbox",
                    "--json",
                ],
                "no_mailbox": True,
            },
            status_invoker=_StatusScript([self._attach_status(), _status_payload(0)]),
            run_invoker=_never_run,
            clock=_counter_clock(),
            sleep=lambda *_: None,
            heartbeat=lambda *_: None,
            mailbox_watcher_factory=fail_factory,
        )

        self.assertTrue(result["ok"], result)
        self.assertNotIn("outbox", result)


class DeferredTerminationSignalsTest(unittest.TestCase):
    """The deferral window holds both signals without touching dispositions."""

    def _restore_signal_state(self) -> None:
        blocked = signal.pthread_sigmask(signal.SIG_BLOCK, ())
        held = blocked & set(autopilot.TERMINATION_SIGNALS)
        if held:
            signal.pthread_sigmask(signal.SIG_UNBLOCK, held)

    def setUp(self) -> None:
        self.addCleanup(self._restore_signal_state)
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(autopilot._termination_handlers())

    def test_the_hold_is_atomic_and_leaves_dispositions_alone(self) -> None:
        for signal_number in autopilot.TERMINATION_SIGNALS:
            with self.subTest(signal=signal_number):
                dispositions = {
                    number: signal.getsignal(number)
                    for number in autopilot.TERMINATION_SIGNALS
                }

                with (
                    self.assertRaises(SystemExit) as caught,
                    autopilot._deferred_termination_signals() as deferred,
                ):
                    # Held, not trapped: nothing is raised here and the
                    # process-wide handlers are exactly the ones the caller
                    # installed.
                    signal.raise_signal(signal_number)
                    self.assertEqual(
                        signal.Signals(signal_number).name,
                        deferred.signal_name,
                    )
                    self.assertEqual(
                        dispositions,
                        {
                            number: signal.getsignal(number)
                            for number in autopilot.TERMINATION_SIGNALS
                        },
                    )

                self.assertEqual(128 + int(signal_number), caught.exception.code)
                self.assertEqual(
                    dispositions,
                    {
                        number: signal.getsignal(number)
                        for number in autopilot.TERMINATION_SIGNALS
                    },
                )

    def test_an_unsignalled_window_leaves_the_mask_untouched(self) -> None:
        before = signal.pthread_sigmask(signal.SIG_BLOCK, ())

        with autopilot._deferred_termination_signals() as deferred:
            self.assertIsNone(deferred.signal_name)
            self.assertTrue(
                set(autopilot.TERMINATION_SIGNALS)
                <= signal.pthread_sigmask(signal.SIG_BLOCK, ())
            )

        self.assertEqual(before, signal.pthread_sigmask(signal.SIG_BLOCK, ()))


if __name__ == "__main__":
    unittest.main()
