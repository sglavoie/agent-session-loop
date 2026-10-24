"""Bridge capability, row-action, and question-get behavior.

Split from tests/test_asl_bridge_server.py."""

from __future__ import annotations

import json
import os
import socket
import tempfile
import unittest
from pathlib import Path

from agent_session_loop import (
    bridge_protocol,
    bridge_session,
    events,
    intervention,
    mailbox,
    mailbox_appenders,
    state,
    workbench_details,
)

from .asl_bridge_server_support import (
    QUESTION_BODY,
    RUN_ID,
    _Corpus,
)
from .support import (
    _make_workbench_plan,
    _write_lock_file,
)

ASL_TEST_LANE = "integration"


class BridgeCapabilityTests(unittest.TestCase):
    def test_every_advertised_capability_has_a_handler_or_is_an_event(self) -> None:
        for name in bridge_session.CAPABILITIES:
            with self.subTest(capability=name):
                self.assertTrue(
                    name in bridge_session.METHOD_HANDLERS
                    or name in bridge_protocol.EVENT_NAMES,
                    f"{name} is advertised with nothing behind it",
                )

    def test_mutation_capabilities_are_advertised(self) -> None:
        for name in (
            intervention.ACTION_REPLY,
            intervention.ACTION_RESOLVE,
            intervention.ACTION_RESUME,
            intervention.ACTION_STOP_AFTER_CURRENT,
            intervention.ACTION_ABORT,
            intervention.ACTION_WORKBENCH,
            "question.get",
            bridge_protocol.METHOD_HANDSHAKE,
        ):
            with self.subTest(capability=name):
                self.assertIn(name, bridge_session.CAPABILITIES)

    def test_events_are_advertised_separately_from_methods(self) -> None:
        """Protocol 1 published one list, so "callable" and "delivered" looked alike."""
        for name in (
            bridge_protocol.EVENT_RUNS_CHANGED,
            bridge_protocol.EVENT_OPERATION_UPDATED,
        ):
            with self.subTest(event=name):
                self.assertIn(name, bridge_protocol.EVENT_NAMES)
                self.assertNotIn(name, bridge_session.CAPABILITIES)

    def test_capability_order_is_the_candidate_order(self) -> None:
        # Derivation must filter, never reorder: a client compares the list.
        candidates = [
            name
            for name in bridge_protocol._METHOD_ORDER
            if name in bridge_session.CAPABILITIES
        ]
        self.assertEqual(candidates, list(bridge_session.CAPABILITIES))


class BridgeRowActionTests(unittest.TestCase):
    def test_wait_metadata_belongs_to_exact_question_not_newest_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir, primary = corpus.waiting_plan()
            first = mailbox.unanswered_questions(plan_dir, state_dir=corpus.state_root)[
                0
            ]
            mailbox_appenders.append_question(
                plan_dir,
                "Later question",
                run_id=RUN_ID,
                session_id="9",
                turn_index=8,
                state_dir=corpus.state_root,
            )
            row = corpus.row_for(plan_dir)
            self.assertEqual(primary, row["question"]["question_id"])
            self.assertEqual(first.created_at, row["question"]["asked_at"])
            self.assertEqual("2", row["question"]["session_id"])
            self.assertEqual(3, row["question"]["turn_index"])
            self.assertEqual("2", row["session_id"])
            self.assertEqual(2, row["question"]["unanswered_count"])

    def test_missing_wait_metadata_stays_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir, _ = corpus.waiting_plan()
            bare = mailbox_appenders.append_question(
                plan_dir, "Bare question", run_id=RUN_ID, state_dir=corpus.state_root
            )
            path = (
                state.plan_state_dir(plan_dir, corpus.state_root)
                / events.STATE_FILE_NAME
            )
            payload = json.loads(path.read_text())
            payload["waiting_for_reply"]["question_id"] = bare.id
            corpus.write_state(plan_dir, payload)
            row = corpus.row_for(plan_dir)
            self.assertIsNone(row["question"]["session_id"])
            self.assertIsNone(row["question"]["turn_index"])
            self.assertIsNone(row["session_id"])

    def test_cross_run_body_and_actions_are_refused_without_closing_history(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir, primary = corpus.waiting_plan()
            historical = mailbox_appenders.append_question(
                plan_dir,
                "Historical question",
                run_id="older-run",
                session_id="2",
                state_dir=corpus.state_root,
            )
            row = corpus.row_for(plan_dir)
            facts = intervention.inspect_run(plan_dir, state_dir=corpus.state_root)
            self.assertEqual((primary,), facts.unanswered_ids)
            self.assertEqual(1, row["question"]["unanswered_count"])
            with self.assertRaises(intervention.InterventionError) as caught:
                intervention.question_body(
                    plan_dir, question_id=historical.id, state_dir=corpus.state_root
                )
            self.assertEqual(
                intervention.REASON_QUESTION_MISMATCH, caught.exception.reason
            )
            with self.assertRaises(intervention.InterventionError):
                intervention.validate_action(
                    facts,
                    intervention.ACTION_REPLY,
                    token=row["action_token"],
                    question_id=historical.id,
                )
            self.assertEqual(
                2,
                len(
                    mailbox.unanswered_questions(plan_dir, state_dir=corpus.state_root)
                ),
            )
            path = (
                state.plan_state_dir(plan_dir, corpus.state_root)
                / events.STATE_FILE_NAME
            )
            payload = json.loads(path.read_text())
            payload["waiting_for_reply"]["question_id"] = historical.id
            corpus.write_state(plan_dir, payload)
            row = corpus.row_for(plan_dir)
            self.assertIsNone(row["question"]["question_id"])
            self.assertIsNone(row["question"]["asked_at"])
            self.assertNotIn(intervention.ACTION_REPLY, row["actions"])

    def test_row_advertises_actions_and_a_token_that_validates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir, question_id = corpus.waiting_plan()

            row = corpus.row_for(plan_dir)

            self.assertIn(intervention.ACTION_REPLY, row["actions"])
            self.assertIn(intervention.ACTION_ABORT, row["actions"])
            self.assertNotIn(intervention.ACTION_RESUME, row["actions"])
            self.assertIn(
                intervention.DISABLED_NOT_PAUSED,
                row["disabled_reasons"][intervention.ACTION_RESUME],
            )
            facts = intervention.inspect_run(plan_dir, state_dir=corpus.state_root)
            self.assertEqual(intervention.action_token(facts), row["action_token"])
            self.assertEqual(question_id, row["question"]["question_id"])

    def test_paused_row_offers_resume_and_refuses_reply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir = corpus.paused_plan()

            row = corpus.row_for(plan_dir)

            self.assertEqual(
                [intervention.ACTION_RESUME, intervention.ACTION_WORKBENCH],
                row["actions"],
            )
            self.assertEqual(
                intervention.DISABLED_PAUSED,
                row["disabled_reasons"][intervention.ACTION_REPLY],
            )

    def test_row_token_survives_more_open_questions_than_the_display_bound(
        self,
    ) -> None:
        # The row shows at most MAX_QUESTION_IDS ids; the token must still
        # fingerprint all of them or every mutation on a busy plan is refused.
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir, _ = corpus.waiting_plan()
            for index in range(bridge_protocol.MAX_QUESTION_IDS + 4):
                mailbox_appenders.append_question(
                    plan_dir,
                    f"extra question {index}",
                    state_dir=corpus.state_root,
                    run_id=RUN_ID,
                )

            row = corpus.row_for(plan_dir)
            facts = intervention.inspect_run(plan_dir, state_dir=corpus.state_root)

            self.assertEqual(
                bridge_protocol.MAX_QUESTION_IDS, len(row["question"]["question_ids"])
            )
            self.assertEqual(intervention.action_token(facts), row["action_token"])


class BridgeQuestionGetTests(unittest.TestCase):
    def test_workbench_questions_include_an_in_turn_mailbox_wait(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir, question_id = corpus.input_wait_plan()

            result = workbench_details.read_questions(
                plan_dir, state_dir=corpus.state_root
            )

            self.assertEqual("waiting", result["context_kind"])
            self.assertEqual(question_id, result["question_id"])
            self.assertEqual(QUESTION_BODY, result["last_message"])
            self.assertEqual(QUESTION_BODY, result["rendered_prompt"])
            self.assertIn(intervention.ACTION_REPLY, result["actions"])
            self.assertEqual(
                intervention.action_token(
                    intervention.inspect_run(plan_dir, state_dir=corpus.state_root)
                ),
                result["action_token"],
            )

    def test_live_question_body_is_returned_with_its_size(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir, question_id = corpus.waiting_plan()

            result = corpus.session().question_get({"plan_dir": str(plan_dir)})

            self.assertEqual(question_id, result["question_id"])
            self.assertEqual("waiting", result["context_kind"])
            self.assertEqual(QUESTION_BODY, result["body"])
            self.assertEqual(len(QUESTION_BODY.encode("utf-8")), result["body_bytes"])

    def test_paused_run_returns_the_synthetic_question(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir = corpus.paused_plan()

            result = corpus.session().question_get({"plan_dir": str(plan_dir)})

            self.assertEqual("pause", result["context_kind"])
            self.assertEqual(QUESTION_BODY, result["body"])
            self.assertTrue(result["question_id"])

    def test_oversize_body_is_refused_with_a_workbench_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir = _make_workbench_plan(corpus.plans_root, "plan-huge")
            _write_lock_file(
                plan_dir,
                corpus.state_root,
                pid=os.getpid(),
                hostname=socket.gethostname(),
            )
            body = "x" * (intervention.MAX_QUESTION_BODY_BYTES + 1)
            question = mailbox_appenders.append_question(
                plan_dir, body, state_dir=corpus.state_root, run_id=RUN_ID
            )
            corpus.write_state(
                plan_dir,
                {
                    "ok": None,
                    "paused": False,
                    "run_id": RUN_ID,
                    "waiting_for_reply": {"question_id": question.id},
                },
            )

            with self.assertRaises(bridge_protocol.BridgeError) as caught:
                corpus.session().question_get({"plan_dir": str(plan_dir)})

            self.assertEqual(intervention.REASON_BODY_TOO_LARGE, caught.exception.code)
            self.assertEqual(
                intervention.ACTION_WORKBENCH, caught.exception.details["fallback"]
            )

    def test_unknown_question_is_refused_without_a_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir, _ = corpus.waiting_plan()

            with self.assertRaises(bridge_protocol.BridgeError) as caught:
                corpus.session().question_get(
                    {"plan_dir": str(plan_dir), "question_id": "not-a-question"}
                )

            self.assertEqual(
                intervention.REASON_QUESTION_MISMATCH, caught.exception.code
            )


if __name__ == "__main__":
    unittest.main()
