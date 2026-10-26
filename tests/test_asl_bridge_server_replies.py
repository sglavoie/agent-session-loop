"""Bridge reply, multi-question mutation, and stale-request behavior.

Split from tests/test_asl_bridge_server.py."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    bridge_protocol,
    control_queue,
    events,
    intervention,
    mailbox_appenders,
    mailbox_cursors,
    mailbox_vocabulary,
    state,
    workbench_launch,
)

from .asl_bridge_server_support import (
    REPLY_TEXT,
    RUN_ID,
    _Corpus,
)

ASL_TEST_LANE = "integration"


class BridgeReplyTests(unittest.TestCase):
    def test_reply_consumes_an_in_turn_input_wait_question(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir, question_id = corpus.input_wait_plan()
            row = corpus.row_for(plan_dir)

            result = corpus.session().question_reply(
                {
                    "plan_dir": str(plan_dir),
                    "operation_id": "op-input-wait",
                    "action_token": row["action_token"],
                    "text": REPLY_TEXT,
                }
            )

            self.assertEqual("awaiting-reply", row["phase"])
            self.assertEqual(question_id, row["question"]["question_id"])
            self.assertIn(intervention.ACTION_REPLY, row["actions"])
            self.assertEqual(bridge_protocol.OPERATION_ACCEPTED, result["status"])
            self.assertEqual(question_id, result["question_id"])

    def test_reply_appends_through_the_shared_service_and_returns_a_receipt(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir, question_id = corpus.waiting_plan()
            row = corpus.row_for(plan_dir)
            session = corpus.session()

            result = session.question_reply(
                {
                    "plan_dir": str(plan_dir),
                    "operation_id": "op-1",
                    "action_token": row["action_token"],
                    "text": REPLY_TEXT,
                }
            )

            self.assertEqual(bridge_protocol.OPERATION_ACCEPTED, result["status"])
            self.assertEqual(question_id, result["question_id"])
            replies = [
                line
                for line in corpus.mailbox_lines(plan_dir)
                if line["kind"] == mailbox_vocabulary.KIND_REPLY
            ]
            self.assertEqual(1, len(replies))
            self.assertEqual(REPLY_TEXT, replies[0]["text"])
            # The lease is the service's, not the CLI's: a bridge reply must
            # leave the same supervisor evidence the mailbox CLI leaves.
            self.assertTrue(
                mailbox_cursors.lease_path_for_plan(
                    plan_dir, corpus.state_root
                ).exists()
            )

    def test_reply_text_never_appears_in_the_response_or_the_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir, _ = corpus.waiting_plan()
            row = corpus.row_for(plan_dir)
            session = corpus.session()

            result = session.question_reply(
                {
                    "plan_dir": str(plan_dir),
                    "operation_id": "op-1",
                    "action_token": row["action_token"],
                    "text": REPLY_TEXT,
                }
            )
            mailbox_appenders.append_receipt(
                plan_dir,
                row["question"]["question_id"],
                result["closure_id"],
                state_dir=corpus.state_root,
            )
            updates = session.poll_operations()

            self.assertEqual(1, len(updates))
            self.assertNotIn(REPLY_TEXT, json.dumps(result))
            self.assertNotIn(REPLY_TEXT, json.dumps(updates))
            self.assertNotIn(REPLY_TEXT, json.dumps(corpus.snapshot()))

    def test_receipt_settles_the_operation_as_consumed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir, question_id = corpus.waiting_plan()
            row = corpus.row_for(plan_dir)
            session = corpus.session()
            result = session.question_reply(
                {
                    "plan_dir": str(plan_dir),
                    "operation_id": "op-1",
                    "action_token": row["action_token"],
                    "text": REPLY_TEXT,
                }
            )

            self.assertEqual([], session.poll_operations())
            mailbox_appenders.append_receipt(
                plan_dir,
                question_id,
                result["closure_id"],
                state_dir=corpus.state_root,
            )
            updates = session.poll_operations()

            self.assertEqual(
                bridge_protocol.EVENT_OPERATION_UPDATED, updates[0]["event"]
            )
            self.assertEqual(
                bridge_protocol.OPERATION_CONSUMED, updates[0]["data"]["status"]
            )
            self.assertEqual("op-1", updates[0]["data"]["operation_id"])
            self.assertEqual([], session.poll_operations())

    def test_resolution_closes_the_question_without_a_reply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir, _ = corpus.waiting_plan()
            row = corpus.row_for(plan_dir)

            result = corpus.session().question_resolve(
                {
                    "plan_dir": str(plan_dir),
                    "operation_id": "op-2",
                    "action_token": row["action_token"],
                    "action": "continue",
                }
            )

            self.assertEqual(bridge_protocol.OPERATION_ACCEPTED, result["status"])
            kinds = [line["kind"] for line in corpus.mailbox_lines(plan_dir)]
            self.assertIn(mailbox_vocabulary.KIND_RESOLUTION, kinds)
            self.assertNotIn(mailbox_vocabulary.KIND_REPLY, kinds)

    def test_empty_reply_is_refused_before_any_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir, _ = corpus.waiting_plan()
            row = corpus.row_for(plan_dir)
            before = corpus.mailbox_lines(plan_dir)

            with self.assertRaises(bridge_protocol.BridgeError) as caught:
                corpus.session().question_reply(
                    {
                        "plan_dir": str(plan_dir),
                        "operation_id": "op-3",
                        "action_token": row["action_token"],
                        "text": "   ",
                    }
                )

            self.assertEqual(intervention.REASON_EMPTY_TEXT, caught.exception.code)
            self.assertEqual(before, corpus.mailbox_lines(plan_dir))


class BridgeMultipleQuestionMutationTests(unittest.TestCase):
    """Closing several open questions on one run, in the operator's own order."""

    def test_reply_targets_the_explicitly_selected_non_primary_question(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir, primary_id, second_id = corpus.two_question_plan()
            row = corpus.row_for(plan_dir)

            result = corpus.session().question_reply(
                {
                    "plan_dir": str(plan_dir),
                    "operation_id": "op-second",
                    "action_token": row["action_token"],
                    "question_id": second_id,
                    "text": REPLY_TEXT,
                }
            )

            self.assertEqual(bridge_protocol.OPERATION_ACCEPTED, result["status"])
            self.assertEqual(second_id, result["question_id"])
            replies = [
                line
                for line in corpus.mailbox_lines(plan_dir)
                if line["kind"] == mailbox_vocabulary.KIND_REPLY
            ]
            self.assertEqual([second_id], [line["to"] for line in replies])
            self.assertEqual(
                (primary_id,),
                intervention.unanswered_question_ids(
                    plan_dir, state_dir=corpus.state_root
                ),
            )

    def test_a_question_that_is_not_open_is_still_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir, _, _ = corpus.two_question_plan()
            row = corpus.row_for(plan_dir)
            before = corpus.mailbox_lines(plan_dir)

            with self.assertRaises(bridge_protocol.BridgeError) as caught:
                corpus.session().question_reply(
                    {
                        "plan_dir": str(plan_dir),
                        "operation_id": "op-unknown",
                        "action_token": row["action_token"],
                        "question_id": "00000000-0000-4000-8000-000000000000",
                        "text": REPLY_TEXT,
                    }
                )

            self.assertEqual(
                intervention.REASON_QUESTION_MISMATCH, caught.exception.code
            )
            self.assertEqual(before, corpus.mailbox_lines(plan_dir))

    def test_the_first_closure_invalidates_the_token_the_second_one_needs(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir, primary_id, second_id = corpus.two_question_plan()
            stale_token = corpus.row_for(plan_dir)["action_token"]
            session = corpus.session()
            session.question_reply(
                {
                    "plan_dir": str(plan_dir),
                    "operation_id": "op-second",
                    "action_token": stale_token,
                    "question_id": second_id,
                    "text": REPLY_TEXT,
                }
            )

            with self.assertRaises(bridge_protocol.BridgeError) as caught:
                session.question_resolve(
                    {
                        "plan_dir": str(plan_dir),
                        "operation_id": "op-primary",
                        "action_token": stale_token,
                        "question_id": primary_id,
                        "action": "continue",
                    }
                )

            self.assertEqual(
                intervention.REASON_STALE_ACTION_TOKEN, caught.exception.code
            )

    def test_two_questions_close_sequentially_against_refreshed_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir, primary_id, second_id = corpus.two_question_plan()
            session = corpus.session()

            first = session.question_reply(
                {
                    "plan_dir": str(plan_dir),
                    "operation_id": "op-second",
                    "action_token": corpus.row_for(plan_dir)["action_token"],
                    "question_id": second_id,
                    "text": REPLY_TEXT,
                }
            )
            second = session.question_resolve(
                {
                    "plan_dir": str(plan_dir),
                    "operation_id": "op-primary",
                    "action_token": corpus.row_for(plan_dir)["action_token"],
                    "question_id": primary_id,
                    "action": "continue",
                }
            )

            self.assertEqual(bridge_protocol.OPERATION_ACCEPTED, first["status"])
            self.assertEqual(bridge_protocol.OPERATION_ACCEPTED, second["status"])
            self.assertEqual(second_id, first["question_id"])
            self.assertEqual(primary_id, second["question_id"])
            closures = {
                line["kind"]: line["to"]
                for line in corpus.mailbox_lines(plan_dir)
                if line["kind"]
                in (mailbox_vocabulary.KIND_REPLY, mailbox_vocabulary.KIND_RESOLUTION)
            }
            self.assertEqual(
                {
                    mailbox_vocabulary.KIND_REPLY: second_id,
                    mailbox_vocabulary.KIND_RESOLUTION: primary_id,
                },
                closures,
            )
            self.assertEqual(
                (),
                intervention.unanswered_question_ids(
                    plan_dir, state_dir=corpus.state_root
                ),
            )


class BridgeStaleRequestTests(unittest.TestCase):
    def test_stale_token_refuses_a_reply_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir, _ = corpus.waiting_plan()
            before = corpus.mailbox_lines(plan_dir)

            with self.assertRaises(bridge_protocol.BridgeError) as caught:
                corpus.session().question_reply(
                    {
                        "plan_dir": str(plan_dir),
                        "operation_id": "op-1",
                        "action_token": "0" * 32,
                        "text": REPLY_TEXT,
                    }
                )

            self.assertEqual(
                intervention.REASON_STALE_ACTION_TOKEN, caught.exception.code
            )
            self.assertTrue(caught.exception.retryable)
            self.assertEqual(before, corpus.mailbox_lines(plan_dir))

    def test_token_from_a_superseded_question_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir, _ = corpus.waiting_plan()
            stale_token = corpus.row_for(plan_dir)["action_token"]
            newer = mailbox_appenders.append_question(
                plan_dir, "a newer question", state_dir=corpus.state_root, run_id=RUN_ID
            )
            self.assertTrue(newer.id)

            with self.assertRaises(bridge_protocol.BridgeError) as caught:
                corpus.session().question_reply(
                    {
                        "plan_dir": str(plan_dir),
                        "operation_id": "op-1",
                        "action_token": stale_token,
                        "text": REPLY_TEXT,
                    }
                )

            self.assertEqual(
                intervention.REASON_STALE_ACTION_TOKEN, caught.exception.code
            )

    def test_stale_resume_never_spawns_a_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir = corpus.paused_plan()
            spawned: list[Any] = []

            def _refuse(*args: Any, **kwargs: Any) -> Any:
                spawned.append((args, kwargs))
                raise AssertionError("a stale resume must not spawn")

            original = workbench_launch.launch_plan
            workbench_launch.launch_plan = _refuse  # type: ignore[assignment]
            try:
                with self.assertRaises(bridge_protocol.BridgeError) as caught:
                    corpus.session().run_resume(
                        {
                            "plan_dir": str(plan_dir),
                            "operation_id": "op-1",
                            "action_token": "0" * 32,
                            "text": REPLY_TEXT,
                        }
                    )
            finally:
                workbench_launch.launch_plan = original  # type: ignore[assignment]

            self.assertEqual(
                intervention.REASON_STALE_ACTION_TOKEN, caught.exception.code
            )
            self.assertEqual([], spawned)

    def test_run_id_mismatch_is_refused_even_with_a_matching_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir, _ = corpus.waiting_plan()
            row = corpus.row_for(plan_dir)

            with self.assertRaises(bridge_protocol.BridgeError) as caught:
                corpus.session().run_abort(
                    {
                        "plan_dir": str(plan_dir),
                        "operation_id": "op-1",
                        "action_token": row["action_token"],
                        "run_id": "20990101T000000Z-9",
                    }
                )

            self.assertEqual(intervention.REASON_RUN_MISMATCH, caught.exception.code)

    def test_control_on_a_plan_with_no_active_run_is_refused(self) -> None:
        # The control CLI deliberately accepts these for a future runner; a
        # bridge client acted on a row, so the same request is a bug there.
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir = corpus.paused_plan()
            row = corpus.row_for(plan_dir)

            with self.assertRaises(bridge_protocol.BridgeError) as caught:
                corpus.session().run_stop_after_current(
                    {
                        "plan_dir": str(plan_dir),
                        "operation_id": "op-1",
                        "action_token": row["action_token"],
                    }
                )

            self.assertEqual(intervention.REASON_NO_ACTIVE_RUN, caught.exception.code)
            self.assertEqual(
                frozenset(),
                intervention.pending_command_ids(plan_dir, state_dir=corpus.state_root),
            )


class UndecodableControlRecordTests(unittest.TestCase):
    # Bytes that are not UTF-8 used to escape as UnicodeDecodeError instead of
    # spoiling only their own line, as a torn line already did.

    def test_a_non_utf8_queue_line_hides_only_itself(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            queue_path = control_queue.queue_path_for_plan(plan_dir, state_dir)
            queue_path.parent.mkdir(parents=True, exist_ok=True)
            queue_path.write_bytes(b'{"id": "\xff"}\n{"id": "kept"}\n')

            self.assertEqual(
                frozenset({"kept"}),
                intervention.pending_command_ids(plan_dir, state_dir=state_dir),
            )

    def test_a_non_utf8_event_line_does_not_hide_a_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            events_path = (
                state.plan_state_dir(plan_dir, state_dir) / events.EVENTS_FILE_NAME
            )
            events_path.parent.mkdir(parents=True, exist_ok=True)
            receipt = json.dumps(
                {
                    "name": events.RUN_CONTROL_EVENT,
                    "payload": {
                        "action": events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
                        "command_id": "op-1",
                    },
                }
            )
            events_path.write_bytes(b'{"name": "\xff"}\n' + receipt.encode() + b"\n")

            self.assertTrue(
                intervention.command_observed(plan_dir, "op-1", state_dir=state_dir)
            )


if __name__ == "__main__":
    unittest.main()
