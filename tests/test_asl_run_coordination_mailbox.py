"""Test asl run coordination mailbox."""

from __future__ import annotations

import json
import unittest
import uuid
from typing import Any
from unittest import mock

from agent_session_loop import (
    clock,
    events,
    mailbox,
    mailbox_appenders,
    mailbox_constants,
    mailbox_cursors,
    mailbox_models,
    mailbox_vocabulary,
    mailbox_waits,
    run_coordination_mailbox_question,
    run_coordination_mailbox_wait,
    run_coordination_wait_models,
    run_coordination_wait_record,
)
from agent_session_loop import run_coordination_constants as wait_budgets
from tests import support_asl_run_coordination as fixtures


class ReplyRaceTests(fixtures.CoordinationTestCase):
    """A closure committed as the wait gives up wins, and is consumed once."""

    def timed_out(
        self, **overrides: Any
    ) -> run_coordination_wait_models.MailboxWaitResult:
        return run_coordination_wait_models.MailboxWaitResult.unclosed(
            wait_budgets.WAIT_REASON_TIMEOUT,
            pending_outbox=overrides.pop("pending_outbox", 1),
            evidence=overrides.pop(
                "evidence",
                run_coordination_wait_record.LeaseEvidence.observed(
                    fixtures.lease_view(
                        fresh=False, updated_at="2026-01-01T00:00:00+00:00"
                    ),
                    unsupervised_seconds=42.0,
                ),
            ),
        )

    def finish(
        self,
        question: mailbox_models.MailboxMessage,
        wait_start: mailbox_models.MailboxMessage,
        result: run_coordination_wait_models.MailboxWaitResult,
    ) -> run_coordination_wait_models.MailboxWaitResult:
        return run_coordination_mailbox_wait.finish_mailbox_reply_wait_lifecycle(
            plan_dir=self.plan_dir,
            state_dir_arg=self.state_dir_arg,
            question_id=question.id,
            wait_id=wait_start.id,
            result=result,
        )

    def test_a_reply_landing_at_the_timeout_overrides_the_timeout(self) -> None:
        question, wait_start = self.open_wait()
        # The poll loop already decided it had timed out; the reply commits
        # before ``finish_wait`` takes the mailbox lock.
        reply = mailbox_appenders.append_reply(
            self.plan_dir, question.id, "here you go", state_dir=self.state_dir_arg
        )

        result = self.finish(question, wait_start, self.timed_out())

        self.assertEqual(result.reason, wait_budgets.WAIT_REASON_REPLY)
        self.assertEqual(result.reply, reply)
        self.assertIsNone(result.resolution)
        self.assertEqual(result.closure_id, reply.id)

    def test_the_overridden_timeout_keeps_the_evidence_the_loop_gathered(self) -> None:
        question, wait_start = self.open_wait()
        mailbox_appenders.append_reply(
            self.plan_dir, question.id, "here you go", state_dir=self.state_dir_arg
        )
        timed_out = self.timed_out()

        result = self.finish(question, wait_start, timed_out)

        self.assertEqual(result.lease_updated_at, timed_out.lease_updated_at)
        self.assertEqual(result.unsupervised_seconds, 42.0)
        self.assertEqual(result.pending_outbox, timed_out.pending_outbox)

    def test_a_resolution_landing_at_the_timeout_overrides_the_timeout(self) -> None:
        question, wait_start = self.open_wait()
        resolution = mailbox_appenders.append_resolution(
            self.plan_dir,
            question.id,
            mailbox_vocabulary.RESOLUTION_ACTION_PAUSE,
            expected_run_id=fixtures.RUN_ID,
            state_dir=self.state_dir_arg,
        )

        result = self.finish(question, wait_start, self.timed_out())

        self.assertEqual(result.reason, wait_budgets.WAIT_REASON_RESOLUTION)
        self.assertEqual(result.resolution, resolution)
        self.assertIsNone(result.reply)

    def test_a_closure_replacing_a_reply_result_clears_the_other_field(self) -> None:
        question, wait_start = self.open_wait()
        resolution = mailbox_appenders.append_resolution(
            self.plan_dir,
            question.id,
            mailbox_vocabulary.RESOLUTION_ACTION_CONTINUE,
            expected_run_id=fixtures.RUN_ID,
            state_dir=self.state_dir_arg,
        )
        stale_reply = mailbox_models.MailboxMessage(
            id="33333333-4444-5555-6666-777777777777",
            seq=1,
            kind=mailbox_vocabulary.KIND_REPLY,
            created_at=clock.isoformat(clock.utc_now()),
        )
        arrived = run_coordination_wait_models.MailboxWaitResult(
            reason=wait_budgets.WAIT_REASON_REPLY,
            pending_outbox=0,
            reply=stale_reply,
        )

        result = self.finish(question, wait_start, arrived)

        self.assertIsNone(result.reply)
        self.assertEqual(result.resolution, resolution)

    def test_a_wait_that_really_timed_out_keeps_its_timeout(self) -> None:
        question, wait_start = self.open_wait()

        result = self.finish(question, wait_start, self.timed_out())

        self.assertEqual(result.reason, wait_budgets.WAIT_REASON_TIMEOUT)
        self.assertIsNone(result.closure)

    def test_the_same_wait_cannot_be_finished_twice(self) -> None:
        question, wait_start = self.open_wait()
        mailbox_appenders.append_reply(
            self.plan_dir, question.id, "here you go", state_dir=self.state_dir_arg
        )
        self.finish(question, wait_start, self.timed_out())

        with self.assertRaises(mailbox_models.MailboxError):
            self.finish(question, wait_start, self.timed_out())

    def test_a_reply_after_a_clean_timeout_is_rejected(self) -> None:
        question, wait_start = self.open_wait()
        self.finish(question, wait_start, self.timed_out())

        with self.assertRaises(mailbox_models.MailboxError) as caught:
            mailbox_appenders.append_reply(
                self.plan_dir, question.id, "too late", state_dir=self.state_dir_arg
            )

        self.assertEqual(
            caught.exception.reason, mailbox_constants.REJECT_QUESTION_NOT_WAITING
        )


class ResumedWaitStartTests(fixtures.CoordinationTestCase):
    """A resumed wait that cannot start degrades to a pause, never a crash."""

    def test_a_refused_wait_start_is_an_unavailable_wait(self) -> None:
        question, wait_start = self.open_wait()
        mailbox_waits.finish_wait(
            self.plan_dir,
            question.id,
            wait_start.id,
            reason=mailbox_vocabulary.WAIT_END_REASON_TIMEOUT,
            state_dir=self.state_dir_arg,
        )

        with (
            mock.patch.object(
                mailbox_waits,
                "append_wait_start",
                side_effect=mailbox_models.MailboxError(
                    mailbox_constants.ERROR_MAILBOX_FULL
                ),
            ),
            self.assertRaises(
                run_coordination_wait_models.MailboxWaitUnavailable
            ) as caught,
        ):
            run_coordination_mailbox_question.ensure_mailbox_question(
                plan_dir=self.plan_dir,
                state_dir_arg=self.state_dir_arg,
                question_id=question.id,
                text=question.text or "",
                run_id=fixtures.RUN_ID,
                session_id=fixtures.SESSION_ID,
                turn_index=1,
            )

        self.assertEqual(caught.exception.reason, mailbox_constants.ERROR_MAILBOX_FULL)


class MailboxWaitCoordinatorTests(fixtures.CoordinationTestCase):
    """Announce, poll, commit, clear -- in that order, whatever the poll does."""

    def prepared(
        self,
        question: mailbox_models.MailboxMessage,
        wait_start: mailbox_models.MailboxMessage,
    ) -> run_coordination_wait_models.PreparedMailboxWait:
        return run_coordination_wait_models.PreparedMailboxWait(
            context=fixtures.qna_context(
                self.plan_dir, self.state_dir, last_message=question.text or ""
            ),
            question=question,
            wait_start=wait_start,
            waiting_for_reply=fixtures.waiting_payload(question.id),
            pending_outbox=1,
            settings=fixtures.settings(),
            started_monotonic=100.0,
        )

    def coordinator(
        self,
        prepared: run_coordination_wait_models.PreparedMailboxWait,
        *,
        wait_for_reply,
        notifier: fixtures.RecordingNotifier | None = None,
        finish_lifecycle=(
            run_coordination_mailbox_wait.finish_mailbox_reply_wait_lifecycle
        ),
    ) -> run_coordination_mailbox_wait.MailboxWaitCoordinator:
        return run_coordination_mailbox_wait.MailboxWaitCoordinator(
            plan_dir=self.plan_dir,
            state_dir_arg=self.state_dir_arg,
            prepared=prepared,
            run_id=fixtures.RUN_ID,
            store=self.store(),
            persistence=self.persistence,
            notifier=notifier,
            wait_for_reply=wait_for_reply,
            finish_lifecycle=finish_lifecycle,
        )

    def test_a_reply_racing_the_timeout_is_honoured_end_to_end(self) -> None:
        question, wait_start = self.open_wait()
        prepared = self.prepared(question, wait_start)
        notifier = fixtures.RecordingNotifier()

        def poll(**_kwargs: Any) -> run_coordination_wait_models.MailboxWaitResult:
            # The supervisor answers in the window between the loop's last
            # unlocked read and the locked commit below.
            mailbox_appenders.append_reply(
                self.plan_dir, question.id, "answered", state_dir=self.state_dir_arg
            )
            return run_coordination_wait_models.MailboxWaitResult(
                reason=wait_budgets.WAIT_REASON_TIMEOUT, pending_outbox=1
            )

        result = self.coordinator(
            prepared, wait_for_reply=poll, notifier=notifier
        ).run()

        self.assertEqual(result.reason, wait_budgets.WAIT_REASON_REPLY)
        self.assertNotIn("waiting_for_reply", self.payload)
        self.assertEqual(notifier.started, [(question.id, question.text)])
        self.assertEqual(
            notifier.ended,
            [
                (
                    question.id,
                    wait_budgets.WAIT_REASON_REPLY,
                    result.closure_id,
                )
            ],
        )
        self.assertEqual(
            [
                payload["action"]
                for payload in self.persistence.payloads(events.MAILBOX_EVENT)
            ],
            [
                events.MAILBOX_ACTION_WAIT_STARTED,
                events.MAILBOX_ACTION_WAIT_CLEARED,
            ],
        )

    def test_the_wait_is_announced_before_the_poll_can_block(self) -> None:
        question, wait_start = self.open_wait()
        prepared = self.prepared(question, wait_start)
        notifier = fixtures.RecordingNotifier()
        observed: list[tuple[bool, int]] = []

        def poll(**_kwargs: Any) -> run_coordination_wait_models.MailboxWaitResult:
            observed.append(
                ("waiting_for_reply" in self.payload, len(notifier.started))
            )
            return run_coordination_wait_models.MailboxWaitResult(
                reason=wait_budgets.WAIT_REASON_TIMEOUT, pending_outbox=0
            )

        self.coordinator(prepared, wait_for_reply=poll, notifier=notifier).run()

        self.assertEqual(observed, [(True, 1)])

    def test_a_poll_that_raises_still_clears_and_announces_the_wait(self) -> None:
        question, wait_start = self.open_wait()
        prepared = self.prepared(question, wait_start)
        notifier = fixtures.RecordingNotifier()

        def poll(**_kwargs: Any) -> run_coordination_wait_models.MailboxWaitResult:
            raise RuntimeError("poll exploded")

        coordinator = self.coordinator(prepared, wait_for_reply=poll, notifier=notifier)
        with self.assertRaises(RuntimeError):
            coordinator.run()

        self.assertNotIn("waiting_for_reply", self.payload)
        self.assertEqual(
            notifier.ended,
            [(question.id, wait_budgets.WAIT_REASON_ERROR, None)],
        )
        cleared = self.persistence.payloads(events.MAILBOX_EVENT)[-1]
        self.assertEqual(cleared["action"], events.MAILBOX_ACTION_WAIT_CLEARED)
        self.assertEqual(cleared["reason"], wait_budgets.WAIT_REASON_ERROR)

    def test_an_interrupted_poll_ends_the_mailbox_wait(self) -> None:
        # Before the fix the question stayed waiting: `asl inbox reply`
        # accepted an answer the stopped run would never consume.
        question, wait_start = self.open_wait()
        prepared = self.prepared(question, wait_start)

        def poll(**_kwargs: Any) -> run_coordination_wait_models.MailboxWaitResult:
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            self.coordinator(prepared, wait_for_reply=poll).run()

        view = mailbox.read_mailbox(self.plan_dir, state_dir=self.state_dir_arg)
        self.assertIsNone(view.active_wait_for(question.id))
        with self.assertRaises(mailbox_models.MailboxError) as caught:
            mailbox_appenders.append_reply(
                self.plan_dir, question.id, "too late", state_dir=self.state_dir_arg
            )
        self.assertEqual(
            caught.exception.reason, mailbox_constants.REJECT_QUESTION_NOT_WAITING
        )

    def test_an_interrupted_poll_never_receipts_an_answer_it_did_not_deliver(
        self,
    ) -> None:
        question, wait_start = self.open_wait()
        prepared = self.prepared(question, wait_start)

        def poll(**_kwargs: Any) -> run_coordination_wait_models.MailboxWaitResult:
            mailbox_appenders.append_reply(
                self.plan_dir, question.id, "answered", state_dir=self.state_dir_arg
            )
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            self.coordinator(prepared, wait_for_reply=poll).run()

        view = mailbox.read_mailbox(self.plan_dir, state_dir=self.state_dir_arg)
        closure = view.closure_for(question.id)
        assert closure is not None
        self.assertIsNone(view.receipt_for_closure(closure.id))

    def test_a_wait_end_that_cannot_be_written_pauses_instead_of_raising(
        self,
    ) -> None:
        question, wait_start = self.open_wait()
        prepared = self.prepared(question, wait_start)

        def refuse(**_kwargs: Any) -> run_coordination_wait_models.MailboxWaitResult:
            raise mailbox_models.MailboxError(mailbox_constants.ERROR_MAILBOX_FULL)

        result = self.coordinator(
            prepared,
            wait_for_reply=lambda **_kwargs: (
                run_coordination_wait_models.MailboxWaitResult(
                    reason=mailbox_constants.ERROR_MAILBOX_FULL, pending_outbox=1
                )
            ),
            finish_lifecycle=refuse,
        ).run()

        self.assertEqual(result.reason, mailbox_constants.ERROR_MAILBOX_FULL)
        self.assertIsNone(result.closure)
        self.assertNotIn("waiting_for_reply", self.payload)

    def test_a_wait_that_gives_up_on_a_full_mailbox_can_still_end(self) -> None:
        # The poll used to give up only past ``view.full`` (128 bytes short of
        # the cap), far above the wait-end cap, so ending the wait always
        # raised. It now gives up once no answer can land, below that cap.
        question, wait_start = self.open_wait()
        mailbox_path = mailbox_cursors.mailbox_path_for_plan(
            self.plan_dir, self.state_dir_arg
        )
        target = (
            mailbox_constants.MAILBOX_MAX_BYTES
            - mailbox_constants.MAILBOX_WAIT_END_RESERVE_BYTES
            - mailbox_constants.MAILBOX_RECEIPT_RESERVE_BYTES
            + 1024
        )
        record = {
            "created_at": clock.isoformat(clock.utc_now()),
            "delivery_mode": mailbox_vocabulary.DELIVERY_MODE_ASYNC,
            "id": str(uuid.uuid4()),
            "kind": mailbox_vocabulary.KIND_QUESTION,
            "text": "",
            "version": mailbox_constants.MAILBOX_SCHEMA_VERSION,
        }
        overhead = len(json.dumps(record, separators=(",", ":"))) + 1
        record["text"] = "x" * (target - mailbox_path.stat().st_size - overhead)
        with mailbox_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, separators=(",", ":")) + "\n")
        view = mailbox.read_mailbox(self.plan_dir, state_dir=self.state_dir_arg)
        self.assertFalse(view.full)
        self.assertFalse(view.accepts_wait_closures)

        result = run_coordination_mailbox_wait.wait_for_mailbox_reply(
            plan_dir=self.plan_dir,
            state_dir_arg=self.state_dir_arg,
            payload=self.payload,
            question_id=question.id,
            run_id=fixtures.RUN_ID,
            settings=fixtures.settings(),
            persistence=self.persistence,
            started_monotonic=0.0,
            monotonic=lambda: 0.0,
            sleep=lambda _seconds: self.fail("a full mailbox must not poll"),
        )
        self.assertEqual(result.reason, mailbox_constants.ERROR_MAILBOX_FULL)

        completion = mailbox_waits.finish_wait(
            self.plan_dir,
            question.id,
            wait_start.id,
            reason=mailbox_vocabulary.WAIT_END_REASON_ERROR,
            state_dir=self.state_dir_arg,
        )
        self.assertEqual(
            completion.wait_end.wait_end_reason,
            mailbox_vocabulary.WAIT_END_REASON_ERROR,
        )

    def test_the_injected_poll_receives_the_prepared_wait_verbatim(self) -> None:
        question, wait_start = self.open_wait()
        prepared = self.prepared(question, wait_start)
        seen: dict[str, Any] = {}

        def poll(**kwargs: Any) -> run_coordination_wait_models.MailboxWaitResult:
            seen.update(kwargs)
            return run_coordination_wait_models.MailboxWaitResult(
                reason=wait_budgets.WAIT_REASON_TIMEOUT, pending_outbox=0
            )

        self.coordinator(prepared, wait_for_reply=poll).run()

        self.assertEqual(seen["question_id"], question.id)
        self.assertEqual(seen["run_id"], fixtures.RUN_ID)
        self.assertEqual(seen["settings"], prepared.settings)
        self.assertEqual(seen["started_monotonic"], 100.0)
        self.assertIs(seen["payload"], self.payload)

    def test_a_wait_without_a_notifier_still_runs(self) -> None:
        question, wait_start = self.open_wait()
        prepared = self.prepared(question, wait_start)

        result = self.coordinator(
            prepared,
            wait_for_reply=lambda **_kwargs: (
                run_coordination_wait_models.MailboxWaitResult(
                    reason=wait_budgets.WAIT_REASON_TIMEOUT,
                    pending_outbox=0,
                )
            ),
        ).run()

        self.assertEqual(result.reason, wait_budgets.WAIT_REASON_TIMEOUT)
        self.assertNotIn("waiting_for_reply", self.payload)


if __name__ == "__main__":
    unittest.main()
