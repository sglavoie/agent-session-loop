#!/usr/bin/env python3
"""Tests for mailbox watcher question projection and reply streaming."""

from __future__ import annotations

import unittest

from asl_autopilot_test_bases import MailboxWatcherBase
from asl_autopilot_test_fakes import _DeadlinePopen
from asl_autopilot_test_payloads import (
    _attached_run_status,
    _leftover_state_attach_status,
)
from asl_autopilot_test_support import (  # noqa: F401
    _ManualClock,
    autopilot,
    setUpModule,
    tearDownModule,
)

from agent_session_loop import mailbox_vocabulary


class MailboxWatcherProjectionTest(MailboxWatcherBase):
    def test_projects_only_observed_unanswered_questions_for_exact_run(
        self,
    ) -> None:
        writes = []
        question = self._question(
            seq=1, text="private question body", run_id="run-current"
        )
        reply = autopilot._mailbox.MailboxMessage(
            seq=3,
            id="00000000-0000-0000-0000-000000000012",
            kind=mailbox_vocabulary.KIND_REPLY,
            created_at="2026-07-24T19:00:02+00:00",
            text="handled",
            to=question.id,
        )
        guidance = autopilot._mailbox.MailboxMessage(
            seq=2,
            id="00000000-0000-0000-0000-000000000013",
            kind=mailbox_vocabulary.KIND_GUIDANCE,
            created_at="2026-07-24T19:00:01+00:00",
            text="unrelated guidance",
        )
        views = iter(
            (
                autopilot._mailbox.MailboxView(
                    mailbox_path=autopilot.Path(self.mailbox_path),
                    messages=(question,),
                    last_seq=1,
                ),
                autopilot._mailbox.MailboxView(
                    mailbox_path=autopilot.Path(self.mailbox_path),
                    messages=(question, guidance),
                    last_seq=2,
                ),
                autopilot._mailbox.MailboxView(
                    mailbox_path=autopilot.Path(self.mailbox_path),
                    messages=(question, guidance, reply),
                    last_seq=3,
                ),
            )
        )

        def write_lease(plan_dir, supervisor_id, *, state_dir=None, **projection):
            writes.append(projection)

        watcher = autopilot._MailboxWatcher.from_status(
            self._status(last_seq=0),
            clock=lambda: 0.0,
            read_mailbox=lambda *_args, **_kwargs: next(views),
            write_lease=write_lease,
            supervisor_id="00000000-0000-0000-0000-000000000099",
        )
        self.assertIsNotNone(watcher)
        assert watcher is not None
        watcher.set_run_id("run-current")
        watcher.establish(now=0.0)
        watcher.poll(now=5.0)
        watcher.poll(now=10.0)
        watcher.poll(now=15.0)

        self.assertEqual(
            writes,
            [
                {
                    "projection_seq": 0,
                    "run_id": "run-current",
                    "unanswered_questions": 0,
                },
                {
                    "latest_unanswered_question_created_at": (question.created_at),
                    "latest_unanswered_question_id": question.id,
                    "projection_seq": 1,
                    "run_id": "run-current",
                    "unanswered_questions": 1,
                },
                {
                    "latest_unanswered_question_created_at": (question.created_at),
                    "latest_unanswered_question_id": question.id,
                    "projection_seq": 1,
                    "run_id": "run-current",
                    "unanswered_questions": 1,
                },
                {
                    "projection_seq": 3,
                    "run_id": "run-current",
                    "unanswered_questions": 0,
                },
            ],
        )
        self.assertNotIn(question.text, repr(writes))

    def test_relaunch_drops_old_projection_and_rebinds_from_current_lock(
        self,
    ) -> None:
        clock = _ManualClock()
        writes: list[dict] = []
        old_question = self._question(seq=1, text="old run question", run_id="run-old")
        current_question = autopilot._mailbox.MailboxMessage(
            seq=2,
            id="00000000-0000-0000-0000-000000000022",
            kind=mailbox_vocabulary.KIND_QUESTION,
            created_at="2026-07-24T19:01:00+00:00",
            text="current run question",
            run_id="run-current",
        )
        old_view = autopilot._mailbox.MailboxView(
            mailbox_path=autopilot.Path(self.mailbox_path),
            messages=(old_question,),
            last_seq=1,
        )
        current_view = autopilot._mailbox.MailboxView(
            mailbox_path=autopilot.Path(self.mailbox_path),
            messages=(old_question, current_question),
            last_seq=2,
        )
        views = iter((old_view, current_view, current_view))

        def write_lease(_plan_dir, _supervisor_id, *, state_dir=None, **projection):
            writes.append(projection)

        watcher = autopilot._MailboxWatcher(
            plan_dir=self.plan_dir,
            state_root=self.state_root,
            state_path=self.state_path,
            mailbox_path=self.mailbox_path,
            lease_path=self.lease_path,
            since=0,
            clock=clock,
            read_mailbox=lambda *_args, **_kwargs: next(views),
            write_lease=write_lease,
            supervisor_id="00000000-0000-0000-0000-000000000099",
        )
        watcher.set_run_id("run-old")
        watcher.establish(now=0.0)
        clock.advance(5.0)
        watcher.poll(now=clock())
        self.assertEqual(watcher.last_seq, 1)
        self.assertEqual(writes[-1]["run_id"], "run-old")
        self.assertEqual(
            writes[-1]["latest_unanswered_question_id"],
            old_question.id,
        )

        watcher.prepare_for_launch(now=clock())
        unbound_write = len(writes) - 1
        self.assertEqual(writes[unbound_write], {})
        self.assertIsNone(watcher.run_id)
        self.assertEqual(watcher.last_seq, 1)

        stale_status = _leftover_state_attach_status(
            1,
            run_id="run-old",
        )
        current_status = _attached_run_status(
            1,
            run_id="run-current",
        )
        statuses = iter((stale_status, current_status, current_status))
        status_calls = 0

        def status_probe():
            nonlocal status_calls
            status_calls += 1
            return next(statuses, current_status)

        final = '{"ok": true, "paused": false, "reason": "all-complete"}'
        payload = autopilot._supervision_session._invoke_run(
            {
                "argv": ["asl", "run", self.plan_dir, "--json"],
                "plan_dir": self.plan_dir,
            },
            chunk_index=2,
            clock=clock,
            heartbeat=lambda *_: None,
            popen=_DeadlinePopen(clock, final),
            status_probe=status_probe,
            mailbox_watcher=watcher,
        )

        self.assertEqual(payload["reason"], "all-complete")
        self.assertGreaterEqual(status_calls, 2)
        self.assertEqual(watcher.run_id, "run-current")
        self.assertEqual(watcher.last_seq, 2)
        successor_writes = writes[unbound_write + 1 :]
        self.assertTrue(successor_writes)
        self.assertFalse(
            any(write.get("run_id") == "run-old" for write in successor_writes)
        )
        self.assertTrue(
            any(
                write.get("run_id") == "run-current"
                and write.get("latest_unanswered_question_id") == current_question.id
                for write in successor_writes
            )
        )
        self.assertFalse(
            any(
                write.get("latest_unanswered_question_id") == old_question.id
                for write in successor_writes
            )
        )

    def test_invoke_run_polls_at_five_seconds_and_streams_reply_once(self) -> None:
        clock = _ManualClock()
        full_text = 'Line one\n"quoted" ' + ("x" * 500)
        question = self._question(seq=1, text=full_text)
        view = autopilot._mailbox.MailboxView(
            mailbox_path=autopilot.Path(self.mailbox_path),
            messages=(question,),
            last_seq=1,
        )
        watcher = autopilot._MailboxWatcher(
            plan_dir=self.plan_dir,
            state_root=self.state_root,
            state_path=self.state_path,
            mailbox_path=self.mailbox_path,
            lease_path=self.lease_path,
            since=0,
            clock=clock,
            read_mailbox=lambda *_args, **_kwargs: view,
            write_lease=lambda *_args, **_kwargs: None,
            supervisor_id="00000000-0000-0000-0000-000000000099",
        )
        self.assertTrue(watcher.establish(now=0.0))
        final = '{"ok": true, "paused": false, "reason": "all-complete"}'
        popen = _DeadlinePopen(clock, final)
        progress = []

        payload = autopilot._supervision_session._invoke_run(
            {
                "argv": ["asl", "run", self.plan_dir, "--json"],
                "plan_dir": self.plan_dir,
            },
            chunk_index=1,
            reply_text="operator reply",
            clock=clock,
            heartbeat=progress.append,
            popen=popen,
            mailbox_watcher=watcher,
        )

        self.assertEqual(popen.timeouts, [5.0, 5.0])
        self.assertEqual(popen.inputs, ["operator reply", None])
        question_records = [line for line in progress if line.startswith("QUESTION ")]
        self.assertEqual(len(question_records), 1)
        self.assertNotIn("\n", question_records[0])
        preview = question_records[0].split("preview=", 1)[1]
        self.assertLessEqual(len(preview), autopilot.QUESTION_PREVIEW_MAX_CHARS)
        self.assertEqual(payload["outbox"]["questions"][0]["text"], full_text)

    def test_final_mailbox_drain_after_child_exit(self) -> None:
        clock = _ManualClock()
        question = self._question(
            seq=1,
            text="question appended immediately before child exit",
        )
        notice = self._notice(
            seq=2,
            text="notice appended immediately before child exit",
        )
        empty_view = autopilot._mailbox.MailboxView(
            mailbox_path=autopilot.Path(self.mailbox_path),
            messages=(),
            last_seq=0,
        )
        final_view = autopilot._mailbox.MailboxView(
            mailbox_path=autopilot.Path(self.mailbox_path),
            messages=(question, notice),
            last_seq=2,
        )
        child_exited = False
        reads = []

        def read_mailbox(*_args, **_kwargs):
            reads.append(child_exited)
            return final_view if child_exited else empty_view

        class ExitAppendingPopen:
            returncode = 0

            def __call__(self, argv, **kwargs):
                self.argv = argv
                self.kwargs = kwargs
                return self

            def communicate(self, input=None, timeout=None):
                nonlocal child_exited
                child_exited = True
                return (
                    '{"ok": true, "paused": false, "reason": "all-complete", '
                    '"run_id": "run-current"}',
                    "",
                )

        watcher = autopilot._MailboxWatcher(
            plan_dir=self.plan_dir,
            state_root=self.state_root,
            state_path=self.state_path,
            mailbox_path=self.mailbox_path,
            lease_path=self.lease_path,
            since=0,
            clock=clock,
            read_mailbox=read_mailbox,
            write_lease=lambda *_args, **_kwargs: None,
            supervisor_id="00000000-0000-0000-0000-000000000099",
        )
        self.assertTrue(watcher.establish(now=0.0))
        progress = []

        payload = autopilot._supervision_session._invoke_run(
            {
                "argv": ["asl", "run", self.plan_dir, "--json"],
                "plan_dir": self.plan_dir,
            },
            chunk_index=1,
            clock=clock,
            heartbeat=progress.append,
            popen=ExitAppendingPopen(),
            mailbox_watcher=watcher,
        )

        self.assertEqual(reads, [True])
        self.assertEqual(
            [item["id"] for item in payload["outbox"]["questions"]],
            [question.id],
        )
        question_records = [
            record for record in progress if record.startswith("QUESTION ")
        ]
        self.assertEqual(len(question_records), 1)
        self.assertIn(question.id, question_records[0])
        self.assertEqual(
            [item["id"] for item in payload["outbox"]["notices"]],
            [notice.id],
        )
        notice_records = [record for record in progress if record.startswith("NOTICE ")]
        self.assertEqual(len(notice_records), 1)
        self.assertIn(notice.id, notice_records[0])


if __name__ == "__main__":
    unittest.main()
