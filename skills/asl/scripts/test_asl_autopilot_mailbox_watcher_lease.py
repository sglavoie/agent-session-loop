#!/usr/bin/env python3
"""Tests for the mailbox watcher lease lifecycle and mailbox policy."""

from __future__ import annotations

import os
import unittest

from asl_autopilot_test_bases import MailboxWatcherBase
from asl_autopilot_test_support import (  # noqa: F401
    MODULE_PATH,
    autopilot,
    setUpModule,
    tearDownModule,
)

from agent_session_loop import mailbox_constants, mailbox_vocabulary


class MailboxWatcherLeaseTest(MailboxWatcherBase):
    def test_watcher_emits_bounded_wait_end_and_receipt_progress(self) -> None:
        question = self._question(seq=1, run_id="run-current")
        wait_end = autopilot._mailbox.MailboxMessage(
            seq=2,
            id="00000000-0000-0000-0000-000000000021",
            kind=mailbox_vocabulary.KIND_WAIT_END,
            created_at="2026-07-24T19:00:01+00:00",
            to=question.id,
            wait_id="00000000-0000-0000-0000-000000000020",
            wait_end_reason=mailbox_vocabulary.WAIT_END_REASON_CLOSURE,
            closure_id="00000000-0000-0000-0000-000000000022",
        )
        receipt = autopilot._mailbox.MailboxMessage(
            seq=3,
            id="00000000-0000-0000-0000-000000000023",
            kind=mailbox_vocabulary.KIND_RECEIPT,
            created_at="2026-07-24T19:00:01+00:00",
            to=question.id,
            wait_id=wait_end.wait_id,
            closure_id=wait_end.closure_id,
        )
        view = autopilot._mailbox.MailboxView(
            mailbox_path=autopilot.Path(self.mailbox_path),
            messages=(question, receipt, wait_end),
            last_seq=3,
        )
        watcher = autopilot._MailboxWatcher.from_status(
            self._status(last_seq=0),
            clock=lambda: 0.0,
            read_mailbox=lambda *_args, **_kwargs: view,
            write_lease=lambda *_args, **_kwargs: None,
        )
        assert watcher is not None
        watcher.set_run_id("run-current")
        watcher.establish(now=0.0)
        watcher.poll(now=0.0, force_read=True)
        progress = watcher.drain_progress_records()
        self.assertTrue(any(line.startswith("WAIT_ENDED ") for line in progress))
        self.assertTrue(any(line.startswith("CLOSURE_CONSUMED ") for line in progress))
        self.assertNotIn(question.text, repr(progress))

    def test_resolves_paths_renews_lease_and_returns_questions_once(self) -> None:
        writes = []
        reads = []
        question = self._question(text="Full question body\nwith a second line")
        guidance = autopilot._mailbox.MailboxMessage(
            seq=3,
            id="00000000-0000-0000-0000-000000000012",
            kind=mailbox_vocabulary.KIND_GUIDANCE,
            created_at="2026-07-24T19:00:01+00:00",
            text="guidance advances the physical cursor",
        )
        view = autopilot._mailbox.MailboxView(
            mailbox_path=autopilot.Path(self.mailbox_path),
            messages=(question, guidance),
            last_seq=3,
        )

        def write_lease(plan_dir, supervisor_id, *, state_dir=None, **projection):
            writes.append((plan_dir, supervisor_id, state_dir, projection))

        def read_mailbox(plan_dir, *, state_dir=None):
            reads.append((plan_dir, state_dir))
            return view

        watcher = autopilot._MailboxWatcher.from_status(
            self._status(),
            clock=lambda: 0.0,
            read_mailbox=read_mailbox,
            write_lease=write_lease,
            supervisor_id="00000000-0000-0000-0000-000000000099",
        )
        self.assertIsNotNone(watcher)
        assert watcher is not None
        self.assertTrue(watcher.establish(now=0.0))
        self.assertEqual(watcher.next_deadline(), 5.0)
        self.assertEqual(writes[0][0], os.path.realpath(self.plan_dir))
        self.assertEqual(writes[0][2], os.path.realpath(self.state_root))

        self.assertEqual(watcher.poll(now=5.0), (question,))
        self.assertEqual(watcher.poll(now=10.0), ())
        watcher.poll(now=30.0)
        self.assertEqual(len(writes), 4)
        self.assertEqual(len(reads), 3)

        outbox = watcher.outbox_payload()
        self.assertEqual(outbox["since"], 1)
        self.assertEqual(outbox["next_since"], 3)
        self.assertEqual(len(outbox["questions"]), 1)
        self.assertEqual(outbox["questions"][0]["text"], question.text)
        self.assertEqual(outbox["notices"], [])

    def test_notices_are_run_scoped_deduplicated_and_not_leased(self) -> None:
        writes: list[dict] = []
        full_text = 'notice line one\n"quoted" ' + ("x" * 500)
        current = self._notice(seq=2, text=full_text)
        stale = self._notice(seq=1, text="stale run notice", run_id="run-old")
        view = autopilot._mailbox.MailboxView(
            mailbox_path=autopilot.Path(self.mailbox_path),
            messages=(stale, current),
            last_seq=2,
        )

        watcher = autopilot._MailboxWatcher.from_status(
            self._status(last_seq=0),
            clock=lambda: 0.0,
            read_mailbox=lambda *_args, **_kwargs: view,
            write_lease=(
                lambda *_args, state_dir=None, **projection: writes.append(projection)
            ),
            supervisor_id="00000000-0000-0000-0000-000000000099",
        )
        self.assertIsNotNone(watcher)
        assert watcher is not None
        watcher.set_run_id("run-current")
        watcher.establish(now=0.0)
        progress: list[str] = []

        self.assertEqual(
            autopilot._poll_mailbox_watcher(
                watcher,
                now=5.0,
                heartbeat=progress.append,
            ),
            (),
        )
        self.assertEqual(
            autopilot._poll_mailbox_watcher(
                watcher,
                now=10.0,
                heartbeat=progress.append,
            ),
            (),
        )

        notice_records = [record for record in progress if record.startswith("NOTICE ")]
        self.assertEqual(len(notice_records), 1)
        self.assertIn(current.id, notice_records[0])
        self.assertNotIn("\n", notice_records[0])
        preview = notice_records[0].split("preview=", 1)[1]
        self.assertLessEqual(len(preview), autopilot.QUESTION_PREVIEW_MAX_CHARS)
        outbox = watcher.outbox_payload()
        self.assertEqual(outbox["include"], "all")
        self.assertEqual(outbox["questions"], [])
        self.assertEqual(
            [notice["id"] for notice in outbox["notices"]],
            [current.id],
        )
        self.assertEqual(outbox["notices"][0]["text"], full_text)
        self.assertTrue(writes)
        self.assertTrue(
            all(
                set(projection).issubset(
                    {
                        "run_id",
                        "projection_seq",
                        "unanswered_questions",
                        "latest_unanswered_question_id",
                        "latest_unanswered_question_created_at",
                    }
                )
                for projection in writes
            )
        )
        self.assertTrue(
            all(projection.get("unanswered_questions", 0) == 0 for projection in writes)
        )

    def test_lease_transient_failure_recovers_and_delivers_question(self) -> None:
        writes = []
        reads = []
        question = self._question(seq=1, text="question after lease recovery")
        empty_view = autopilot._mailbox.MailboxView(
            mailbox_path=autopilot.Path(self.mailbox_path),
            messages=(),
            last_seq=0,
        )
        question_view = autopilot._mailbox.MailboxView(
            mailbox_path=autopilot.Path(self.mailbox_path),
            messages=(question,),
            last_seq=1,
        )
        views = iter((empty_view, question_view))

        def write_lease(plan_dir, supervisor_id, *, state_dir=None, **projection):
            writes.append((plan_dir, supervisor_id, state_dir, projection))
            if len(writes) == 2:
                raise OSError("transient lease write failure")

        def read_mailbox(plan_dir, *, state_dir=None):
            reads.append((plan_dir, state_dir))
            return next(views)

        watcher = autopilot._MailboxWatcher.from_status(
            self._status(last_seq=0),
            clock=lambda: 0.0,
            read_mailbox=read_mailbox,
            write_lease=write_lease,
            supervisor_id="00000000-0000-0000-0000-000000000099",
        )
        self.assertIsNotNone(watcher)
        assert watcher is not None
        self.assertTrue(watcher.establish(now=0.0))
        progress: list[str] = []

        self.assertEqual(
            autopilot._poll_mailbox_watcher(
                watcher,
                now=30.0,
                heartbeat=progress.append,
            ),
            (),
        )
        self.assertTrue(watcher.enabled)
        self.assertEqual(watcher.next_deadline(), 35.0)
        self.assertIn(
            "MAILBOX WARN lease renewal failed; retry scheduled",
            progress,
        )

        self.assertEqual(
            autopilot._poll_mailbox_watcher(
                watcher,
                now=60.0,
                heartbeat=progress.append,
            ),
            (question,),
        )
        self.assertTrue(watcher.enabled)
        self.assertIn("MAILBOX lease renewal recovered", progress)
        self.assertTrue(
            any(record.startswith(f"QUESTION id={question.id} ") for record in progress)
        )
        self.assertEqual(len(writes), 3)
        self.assertEqual(len(reads), 2)
        self.assertEqual(
            watcher.outbox_payload()["lease_supervision"],
            {
                "degraded": False,
                "renewal_failures": 1,
                "renewal_recoveries": 1,
            },
        )

    def test_initial_lease_failure_stays_armed_and_recovers(self) -> None:
        writes = []
        empty_view = autopilot._mailbox.MailboxView(
            mailbox_path=autopilot.Path(self.mailbox_path),
            messages=(),
            last_seq=0,
        )

        def write_lease(plan_dir, supervisor_id, *, state_dir=None, **projection):
            writes.append((plan_dir, supervisor_id, state_dir, projection))
            if len(writes) == 1:
                raise autopilot._mailbox.MailboxError(
                    mailbox_constants.ERROR_LEASE_WRITE,
                    "transient establishment failure",
                )

        watcher = autopilot._MailboxWatcher.from_status(
            self._status(last_seq=0),
            clock=lambda: 0.0,
            read_mailbox=lambda *_args, **_kwargs: empty_view,
            write_lease=write_lease,
            supervisor_id="00000000-0000-0000-0000-000000000099",
        )
        self.assertIsNotNone(watcher)
        assert watcher is not None
        progress: list[str] = []

        self.assertTrue(watcher.establish(now=0.0))
        self.assertTrue(watcher.enabled)
        self.assertEqual(watcher.next_deadline(), 5.0)
        self.assertEqual(
            autopilot._poll_mailbox_watcher(
                watcher,
                now=5.0,
                heartbeat=progress.append,
            ),
            (),
        )
        self.assertIn(
            "MAILBOX WARN lease renewal failed; retry scheduled",
            progress,
        )

        self.assertEqual(
            autopilot._poll_mailbox_watcher(
                watcher,
                now=30.0,
                heartbeat=progress.append,
            ),
            (),
        )
        self.assertTrue(watcher.enabled)
        self.assertIn("MAILBOX lease renewal recovered", progress)
        self.assertEqual(len(writes), 3)
        self.assertEqual(
            watcher.outbox_payload()["lease_supervision"],
            {
                "degraded": False,
                "renewal_failures": 1,
                "renewal_recoveries": 1,
            },
        )

    def test_initial_lease_persistent_failure_remains_armed(self) -> None:
        writes = []
        empty_view = autopilot._mailbox.MailboxView(
            mailbox_path=autopilot.Path(self.mailbox_path),
            messages=(),
            last_seq=0,
        )

        def write_lease(*_args, **_kwargs):
            writes.append(True)
            raise OSError("persistent lease write failure")

        watcher = autopilot._MailboxWatcher.from_status(
            self._status(last_seq=0),
            clock=lambda: 0.0,
            read_mailbox=lambda *_args, **_kwargs: empty_view,
            write_lease=write_lease,
        )
        self.assertIsNotNone(watcher)
        assert watcher is not None
        progress: list[str] = []

        self.assertTrue(watcher.establish(now=0.0))
        for now in (5.0, 30.0, 60.0):
            self.assertEqual(
                autopilot._poll_mailbox_watcher(
                    watcher,
                    now=now,
                    heartbeat=progress.append,
                ),
                (),
            )

        self.assertTrue(watcher.enabled)
        self.assertEqual(len(writes), 4)
        self.assertEqual(
            progress.count("MAILBOX WARN lease renewal failed; retry scheduled"),
            1,
        )
        self.assertEqual(
            watcher.outbox_payload()["lease_supervision"],
            {
                "degraded": True,
                "renewal_failures": 4,
                "renewal_recoveries": 0,
            },
        )


class AutopilotMailboxPolicyTest(unittest.TestCase):
    def test_skill_distinguishes_notices_replies_and_resolutions(self) -> None:
        skill_dir = os.path.realpath(os.path.join(os.path.dirname(MODULE_PATH), ".."))

        def read(*parts: str) -> str:
            with open(os.path.join(skill_dir, *parts), encoding="utf-8") as handle:
                return handle.read()

        # SKILL.md keeps the short notice and closure rules; the watcher's
        # record format and each closure in full live in on-demand references.
        skill = read("SKILL.md")
        self.assertIn("A `NOTICE id=...` record is nonblocking", skill)
        self.assertIn("references/questions-and-pauses.md", skill)
        watcher = read("references", "helper-internals.md")
        self.assertIn("NOTICE id=... preview=...", watcher)
        self.assertIn("notices` array", watcher)
        text = read("references", "questions-and-pauses.md")
        self.assertIn("Resolve `continue` autonomously only", text)
        self.assertIn("Send a factual reply only", text)
        self.assertIn("Relay ambiguous decisions to the user", text)
        self.assertIn("Resolve `pause` only", text)
        self.assertIn("Do not use a fake", text)


if __name__ == "__main__":
    unittest.main()
