"""Stdio bridge mutations: shared services, freshness, receipts, and lifecycle.

Split from tests/test_asl_bridge_projection.py, which owns the read boundary.
This module owns what happens when the bridge is allowed to write: that every
mutation goes through the same service layer the CLI and the Workbench HTTP API
use, that a stale request writes nothing and spawns nothing, that an accepted
operation is only settled by ASL's own records, and that operator prose never
escapes into the protocol or the diagnostics stream.
"""

from __future__ import annotations

import io
import json
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    bridge,
    control_queue,
    events,
    intervention,
    mailbox,
    state,
    workbench,
)

from .support import (
    _make_workbench_plan,
    _write_lock_file,
    _write_state_payload,
)

QUESTION_BODY = "Should we drop the v1 column before the backfill lands?"
SECOND_QUESTION_BODY = "Which staging database should the rehearsal target?"
REPLY_TEXT = "Yes, drop it after the backfill verifies clean."
RUN_ID = "20260619T150000Z-1"

# Generous enough that a loaded machine cannot fail a handshake between the
# test and a worker thread, short enough that a real deadlock still fails.
WAIT = 5.0


class _Corpus:
    """A plans root plus its ASL state root, wired the way discovery expects."""

    def __init__(self, tmp_path: Path) -> None:
        self.plans_root = tmp_path / "plans"
        self.plans_root.mkdir()
        self.state_root = tmp_path / "state"

    def snapshot(self) -> dict[str, Any]:
        return bridge.build_runs_snapshot(
            state_dir=self.state_root,
            plan_roots=(self.plans_root,),
            include_default_dirs=False,
        )

    def session(self, **kwargs: Any) -> bridge.BridgeSession:
        return bridge.BridgeSession(
            snapshot_builder=self.snapshot, state_dir=self.state_root, **kwargs
        )

    def write_state(self, plan_dir: Path, payload: dict[str, Any]) -> Path:
        return _write_state_payload(
            plan_dir, self.state_root, {"plan_dir": str(plan_dir), **payload}
        )

    def waiting_plan(self, name: str = "plan-waiting") -> tuple[Path, str]:
        """A live run holding an open mailbox question, the reply-ready shape."""
        plan_dir = _make_workbench_plan(self.plans_root, name)
        _write_lock_file(
            plan_dir,
            self.state_root,
            pid=os.getpid(),
            hostname=socket.gethostname(),
        )
        question = mailbox.append_question(
            plan_dir,
            QUESTION_BODY,
            state_dir=self.state_root,
            run_id=RUN_ID,
            session_id="2",
            turn_index=3,
        )
        self.write_state(
            plan_dir,
            {
                "ok": None,
                "paused": False,
                "run_id": RUN_ID,
                "harness": "codex",
                "run_started_at": "2026-06-19T15:00:00+00:00",
                "active_turn": {
                    "session_id": "2",
                    "turn_index": 3,
                    "started_at": "2026-06-19T15:00:00+00:00",
                    "last_activity_at": "2026-06-19T15:04:00+00:00",
                },
                "waiting_for_reply": {
                    "question_id": question.id,
                    "hard_deadline": "2026-06-19T17:00:00+00:00",
                },
            },
        )
        return plan_dir, question.id

    def two_question_plan(
        self, name: str = "plan-two-questions"
    ) -> tuple[Path, str, str]:
        """A live run holding two open questions, the first one primary.

        The runner state names only the first question, so the second is
        reachable solely as an explicitly selected id.
        """
        plan_dir, primary_id = self.waiting_plan(name)
        second = mailbox.append_question(
            plan_dir,
            SECOND_QUESTION_BODY,
            state_dir=self.state_root,
            run_id=RUN_ID,
            session_id="2",
            turn_index=3,
        )
        return plan_dir, primary_id, second.id

    def running_plan(self, name: str = "plan-running") -> Path:
        """A live run with no open question: control commands only."""
        plan_dir = _make_workbench_plan(self.plans_root, name)
        _write_lock_file(
            plan_dir,
            self.state_root,
            pid=os.getpid(),
            hostname=socket.gethostname(),
        )
        self.write_state(
            plan_dir,
            {
                "ok": None,
                "paused": False,
                "run_id": RUN_ID,
                "harness": "codex",
                "run_started_at": "2026-06-19T15:00:00+00:00",
                "active_turn": {
                    "session_id": "2",
                    "turn_index": 3,
                    "started_at": "2026-06-19T15:00:00+00:00",
                    "last_activity_at": "2026-06-19T15:04:00+00:00",
                },
            },
        )
        return plan_dir

    def paused_plan(self, name: str = "plan-paused") -> Path:
        """A paused run whose question is the harness's own last message."""
        plan_dir = _make_workbench_plan(self.plans_root, name)
        artifact_dir = state.plan_state_dir(plan_dir, self.state_root) / "runs"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        last_message = artifact_dir / "last-message.txt"
        last_message.write_text(QUESTION_BODY, encoding="utf-8")
        self.write_state(
            plan_dir,
            {
                "ok": False,
                "paused": True,
                "reason": "agent-question",
                "pause_reason": "agent-question",
                "pause_message": "Awaiting confirmation on the v1 column",
                "run_id": "20260619T140000Z-1",
                "harness": "claude",
                "run_started_at": "2026-06-19T14:00:00+00:00",
                "turns": [
                    {
                        "session_id": "1",
                        "turn_index": 1,
                        "last_message_path": str(last_message),
                        "harness": "claude",
                        "started_at": "2026-06-19T14:00:00+00:00",
                    }
                ],
            },
        )
        return plan_dir

    def row_for(self, plan_dir: Path) -> dict[str, Any]:
        for row in self.snapshot()["rows"]:
            if row["plan_dir"] == str(plan_dir):
                return row
        raise AssertionError(f"no snapshot row for {plan_dir}")

    def mailbox_lines(self, plan_dir: Path) -> list[dict[str, Any]]:
        path = mailbox.mailbox_path_for_plan(plan_dir, self.state_root)
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def record_command_observed(self, plan_dir: Path, command_id: str) -> None:
        """Write the runner's own delivery receipt for one queued command."""
        plan_state = state.plan_state_dir(plan_dir, self.state_root)
        plan_state.mkdir(parents=True, exist_ok=True)
        record = {
            "name": events.RUN_CONTROL_EVENT,
            "payload": {
                "action": events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
                "command_id": command_id,
            },
        }
        with (plan_state / events.EVENTS_FILE_NAME).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")


def _request(request_id: str, method: str, **params: Any) -> bytes:
    payload: dict[str, Any] = {
        "type": "request",
        "protocol": bridge.PROTOCOL_VERSION,
        "id": request_id,
        "method": method,
        "params": params,
    }
    return json.dumps(payload).encode("utf-8")


class _RecordingServer:
    """A stand-in for the Workbench HTTP server that never binds a socket."""

    instances: list[_RecordingServer] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.token = "test-token"
        self.bound_host = "127.0.0.1"
        self.bound_port = 54321
        self.served = 0
        self.poll_intervals: list[float] = []
        self.shutdowns = 0
        self.closes = 0
        _RecordingServer.instances.append(self)

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        self.served += 1
        self.poll_intervals.append(poll_interval)

    def shutdown(self) -> None:
        self.shutdowns += 1

    def server_close(self) -> None:
        self.closes += 1


class BridgeCapabilityTests(unittest.TestCase):
    def test_every_advertised_capability_has_a_handler_or_is_an_event(self) -> None:
        for name in bridge.CAPABILITIES:
            with self.subTest(capability=name):
                self.assertTrue(
                    name in bridge.METHOD_HANDLERS or name in bridge.EVENT_NAMES,
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
            bridge.EVENT_OPERATION_UPDATED,
        ):
            with self.subTest(capability=name):
                self.assertIn(name, bridge.CAPABILITIES)

    def test_capability_order_is_the_candidate_order(self) -> None:
        # Derivation must filter, never reorder: a client compares the list.
        candidates = [
            name for name in bridge._CAPABILITY_ORDER if name in bridge.CAPABILITIES
        ]
        self.assertEqual(candidates, list(bridge.CAPABILITIES))


class BridgeRowActionTests(unittest.TestCase):
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
            for index in range(bridge.MAX_QUESTION_IDS + 4):
                mailbox.append_question(
                    plan_dir,
                    f"extra question {index}",
                    state_dir=corpus.state_root,
                    run_id=RUN_ID,
                )

            row = corpus.row_for(plan_dir)
            facts = intervention.inspect_run(plan_dir, state_dir=corpus.state_root)

            self.assertEqual(
                bridge.MAX_QUESTION_IDS, len(row["question"]["question_ids"])
            )
            self.assertEqual(intervention.action_token(facts), row["action_token"])


class BridgeQuestionGetTests(unittest.TestCase):
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
            question = mailbox.append_question(
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

            with self.assertRaises(bridge.BridgeError) as caught:
                corpus.session().question_get({"plan_dir": str(plan_dir)})

            self.assertEqual(intervention.REASON_BODY_TOO_LARGE, caught.exception.code)
            self.assertEqual(
                intervention.ACTION_WORKBENCH, caught.exception.details["fallback"]
            )

    def test_unknown_question_is_refused_without_a_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir, _ = corpus.waiting_plan()

            with self.assertRaises(bridge.BridgeError) as caught:
                corpus.session().question_get(
                    {"plan_dir": str(plan_dir), "question_id": "not-a-question"}
                )

            self.assertEqual(
                intervention.REASON_QUESTION_MISMATCH, caught.exception.code
            )


class BridgeReplyTests(unittest.TestCase):
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

            self.assertEqual(bridge.OPERATION_ACCEPTED, result["status"])
            self.assertEqual(question_id, result["question_id"])
            replies = [
                line
                for line in corpus.mailbox_lines(plan_dir)
                if line["kind"] == mailbox.KIND_REPLY
            ]
            self.assertEqual(1, len(replies))
            self.assertEqual(REPLY_TEXT, replies[0]["text"])
            # The lease is the service's, not the CLI's: a bridge reply must
            # leave the same supervisor evidence the mailbox CLI leaves.
            self.assertTrue(
                mailbox.lease_path_for_plan(plan_dir, corpus.state_root).exists()
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
            mailbox.append_receipt(
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
            mailbox.append_receipt(
                plan_dir,
                question_id,
                result["closure_id"],
                state_dir=corpus.state_root,
            )
            updates = session.poll_operations()

            self.assertEqual(bridge.EVENT_OPERATION_UPDATED, updates[0]["event"])
            self.assertEqual(bridge.OPERATION_CONSUMED, updates[0]["data"]["status"])
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

            self.assertEqual(bridge.OPERATION_ACCEPTED, result["status"])
            kinds = [line["kind"] for line in corpus.mailbox_lines(plan_dir)]
            self.assertIn(mailbox.KIND_RESOLUTION, kinds)
            self.assertNotIn(mailbox.KIND_REPLY, kinds)

    def test_empty_reply_is_refused_before_any_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir, _ = corpus.waiting_plan()
            row = corpus.row_for(plan_dir)
            before = corpus.mailbox_lines(plan_dir)

            with self.assertRaises(bridge.BridgeError) as caught:
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

            self.assertEqual(bridge.OPERATION_ACCEPTED, result["status"])
            self.assertEqual(second_id, result["question_id"])
            replies = [
                line
                for line in corpus.mailbox_lines(plan_dir)
                if line["kind"] == mailbox.KIND_REPLY
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

            with self.assertRaises(bridge.BridgeError) as caught:
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

            with self.assertRaises(bridge.BridgeError) as caught:
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

            self.assertEqual(bridge.OPERATION_ACCEPTED, first["status"])
            self.assertEqual(bridge.OPERATION_ACCEPTED, second["status"])
            self.assertEqual(second_id, first["question_id"])
            self.assertEqual(primary_id, second["question_id"])
            closures = {
                line["kind"]: line["to"]
                for line in corpus.mailbox_lines(plan_dir)
                if line["kind"] in (mailbox.KIND_REPLY, mailbox.KIND_RESOLUTION)
            }
            self.assertEqual(
                {mailbox.KIND_REPLY: second_id, mailbox.KIND_RESOLUTION: primary_id},
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

            with self.assertRaises(bridge.BridgeError) as caught:
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
            newer = mailbox.append_question(
                plan_dir, "a newer question", state_dir=corpus.state_root, run_id=RUN_ID
            )
            self.assertTrue(newer.id)

            with self.assertRaises(bridge.BridgeError) as caught:
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

            original = workbench.launch_plan
            workbench.launch_plan = _refuse  # type: ignore[assignment]
            try:
                with self.assertRaises(bridge.BridgeError) as caught:
                    corpus.session().run_resume(
                        {
                            "plan_dir": str(plan_dir),
                            "operation_id": "op-1",
                            "action_token": "0" * 32,
                            "text": REPLY_TEXT,
                        }
                    )
            finally:
                workbench.launch_plan = original  # type: ignore[assignment]

            self.assertEqual(
                intervention.REASON_STALE_ACTION_TOKEN, caught.exception.code
            )
            self.assertEqual([], spawned)

    def test_run_id_mismatch_is_refused_even_with_a_matching_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir, _ = corpus.waiting_plan()
            row = corpus.row_for(plan_dir)

            with self.assertRaises(bridge.BridgeError) as caught:
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

            with self.assertRaises(bridge.BridgeError) as caught:
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


class BridgeControlTests(unittest.TestCase):
    def test_stop_after_current_queues_the_operation_id_as_the_command_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir = corpus.running_plan()
            row = corpus.row_for(plan_dir)

            result = corpus.session().run_stop_after_current(
                {
                    "plan_dir": str(plan_dir),
                    "operation_id": "op-stop",
                    "action_token": row["action_token"],
                }
            )

            self.assertEqual("op-stop", result["command_id"])
            drained = control_queue.drain_queue(plan_dir, state_dir=corpus.state_root)
            self.assertEqual(1, len(drained.accepted))
            self.assertEqual(
                control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT,
                drained.accepted[0].command,
            )
            self.assertEqual("op-stop", drained.accepted[0].id)

    def test_abort_uses_the_default_grace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir = corpus.running_plan()
            row = corpus.row_for(plan_dir)

            corpus.session().run_abort(
                {
                    "plan_dir": str(plan_dir),
                    "operation_id": "op-abort",
                    "action_token": row["action_token"],
                }
            )

            drained = control_queue.drain_queue(plan_dir, state_dir=corpus.state_root)
            command = drained.accepted[0]
            self.assertEqual(control_queue.COMMAND_REQUEST_ABORT_TURN, command.command)
            self.assertEqual(
                control_queue.DEFAULT_ABORT_GRACE_SECONDS, command.grace_seconds
            )

    def test_observed_command_settles_as_consumed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir = corpus.running_plan()
            row = corpus.row_for(plan_dir)
            session = corpus.session()
            session.run_stop_after_current(
                {
                    "plan_dir": str(plan_dir),
                    "operation_id": "op-stop",
                    "action_token": row["action_token"],
                }
            )

            self.assertEqual([], session.poll_operations())
            corpus.record_command_observed(plan_dir, "op-stop")
            updates = session.poll_operations()

            self.assertEqual(bridge.OPERATION_CONSUMED, updates[0]["data"]["status"])

    def test_command_drained_after_the_run_ends_settles_as_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir = corpus.running_plan()
            row = corpus.row_for(plan_dir)
            session = corpus.session()
            session.run_abort(
                {
                    "plan_dir": str(plan_dir),
                    "operation_id": "op-abort",
                    "action_token": row["action_token"],
                }
            )

            control_queue.drain_queue(plan_dir, state_dir=corpus.state_root)
            # Draining alone is not conclusive; the runner may still log it.
            self.assertEqual([], session.poll_operations())

            (
                state.plan_state_dir(plan_dir, corpus.state_root) / state.LOCK_FILE_NAME
            ).unlink()
            updates = session.poll_operations()

            self.assertEqual(bridge.OPERATION_REJECTED, updates[0]["data"]["status"])


class BridgeResumeTests(unittest.TestCase):
    def test_resume_streams_the_reply_and_asks_for_persisted_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir = corpus.paused_plan()
            row = corpus.row_for(plan_dir)
            calls: list[dict[str, Any]] = []

            def _capture(target: Any, **kwargs: Any) -> Any:
                calls.append({"plan_dir": target, **kwargs})
                return workbench.LaunchResult(
                    pid=4242,
                    argv=("python3", "-m", "agent_session_loop", "run"),
                    plan_dir=Path(target),
                    profile_payload={},
                    reply_provided=True,
                    reply_length=len(kwargs.get("reply_text") or ""),
                    reply_origin="stdin",
                    state_dir=None,
                )

            original = workbench.launch_plan
            workbench.launch_plan = _capture  # type: ignore[assignment]
            try:
                result = corpus.session().run_resume(
                    {
                        "plan_dir": str(plan_dir),
                        "operation_id": "op-resume",
                        "action_token": row["action_token"],
                        "text": REPLY_TEXT,
                    }
                )
            finally:
                workbench.launch_plan = original  # type: ignore[assignment]

            self.assertEqual(bridge.OPERATION_ACCEPTED, result["status"])
            self.assertEqual(1, len(calls))
            self.assertTrue(calls[0]["resume"])
            self.assertEqual(REPLY_TEXT, calls[0]["reply_text"])
            self.assertNotIn(REPLY_TEXT, json.dumps(result))

    def test_one_request_spawns_at_most_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir = corpus.paused_plan()
            row = corpus.row_for(plan_dir)
            session = corpus.session()
            spawns: list[Any] = []

            def _capture(target: Any, **kwargs: Any) -> Any:
                spawns.append(target)
                return workbench.LaunchResult(
                    pid=4242,
                    argv=(),
                    plan_dir=Path(target),
                    profile_payload={},
                    reply_provided=True,
                    reply_length=1,
                    reply_origin="stdin",
                    state_dir=None,
                )

            params = {
                "plan_dir": str(plan_dir),
                "operation_id": "op-resume",
                "action_token": row["action_token"],
                "text": REPLY_TEXT,
            }
            original = workbench.launch_plan
            workbench.launch_plan = _capture  # type: ignore[assignment]
            try:
                first = session.run_resume(params)
                second = session.run_resume(params)
            finally:
                workbench.launch_plan = original  # type: ignore[assignment]

            self.assertEqual(1, len(spawns))
            self.assertEqual(first, second)

    def test_resume_settles_as_consumed_once_the_run_leaves_the_paused_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir = corpus.paused_plan()
            row = corpus.row_for(plan_dir)
            session = corpus.session()

            original = workbench.launch_plan
            workbench.launch_plan = lambda target, **kwargs: workbench.LaunchResult(  # type: ignore[assignment]
                pid=1,
                argv=(),
                plan_dir=Path(target),
                profile_payload={},
                reply_provided=True,
                reply_length=1,
                reply_origin="stdin",
                state_dir=None,
            )
            try:
                session.run_resume(
                    {
                        "plan_dir": str(plan_dir),
                        "operation_id": "op-resume",
                        "action_token": row["action_token"],
                        "text": REPLY_TEXT,
                    }
                )
            finally:
                workbench.launch_plan = original  # type: ignore[assignment]

            self.assertEqual([], session.poll_operations())
            corpus.write_state(
                plan_dir,
                {"ok": None, "paused": False, "run_id": "20260619T140000Z-1"},
            )
            updates = session.poll_operations()

            self.assertEqual(bridge.OPERATION_CONSUMED, updates[0]["data"]["status"])


class BridgeOperationWindowTests(unittest.TestCase):
    def test_window_evicts_settled_operations_but_never_live_ones(self) -> None:
        clock = [0.0]
        session = bridge.BridgeSession(
            snapshot_builder=lambda: {"digest": "d", "row_count": 0, "rows": []},
            clock=lambda: clock[0],
        )
        for index in range(bridge.MAX_OPERATIONS + 10):
            operation = bridge.Operation(
                operation_id=f"op-{index}",
                method=intervention.ACTION_REPLY,
                plan_dir="/plans/alpha",
                row_id="row",
                accepted_at=clock[0],
            )
            if index % 2 == 0:
                operation.settle(bridge.OPERATION_CONSUMED)
            session.operations[operation.operation_id] = operation
        session._evict()

        live = [op for op in session.operations.values() if not op.terminal]
        self.assertEqual(bridge.MAX_OPERATIONS // 2 + 5, len(live))
        self.assertLessEqual(len(session.operations), bridge.MAX_OPERATIONS)

    def test_unsettled_operation_expires_after_the_ttl(self) -> None:
        clock = [0.0]
        session = bridge.BridgeSession(
            snapshot_builder=lambda: {"digest": "d", "row_count": 0, "rows": []},
            clock=lambda: clock[0],
        )
        session.operations["op-1"] = bridge.Operation(
            operation_id="op-1",
            method=intervention.ACTION_REPLY,
            plan_dir="/plans/alpha",
            row_id="row",
            accepted_at=0.0,
        )

        clock[0] = bridge.OPERATION_TTL_SECONDS + 1.0
        updates = session.poll_operations()

        self.assertEqual(bridge.OPERATION_EXPIRED, updates[0]["data"]["status"])


class _HeldBuilder:
    """A corpus walk the test releases one at a time.

    Every call blocks until the test hands it a permit, so a walk can be held
    open while the test asserts what the rest of the bridge does meanwhile.
    """

    def __init__(self) -> None:
        self._entered = threading.Semaphore(0)
        self._permits = threading.Semaphore(0)
        self.calls = 0

    def __call__(self) -> dict[str, Any]:
        self.calls += 1
        digest = f"d{self.calls}"
        self._entered.release()
        if not self._permits.acquire(timeout=WAIT):
            raise AssertionError("the test never released the held projection")
        return {
            "digest": digest,
            "row_count": 0,
            "rows": [],
            "warnings": [],
            "truncated": False,
        }

    def await_entry(self) -> None:
        if not self._entered.acquire(timeout=WAIT):
            raise AssertionError("the projection never started")

    def let_through(self, count: int = 1) -> None:
        for _ in range(count):
            self._permits.release()


class _ColdWalkScript:
    """Drives serve_stdio's select loop in fake time while a walk is held.

    The first scan waits until the walk is actually in flight, so what follows
    is a loop running beside a held walk rather than one that got lucky on
    timing. The first two scans deliver a frame, advancing the clock by half
    the timeout the way a frame arriving early cuts a wait short; the rest wait
    the timeout out.
    """

    def __init__(
        self, clock: list[float], builder: _HeldBuilder, *, scans: int
    ) -> None:
        self.clock = clock
        self.builder = builder
        self.scans = scans
        self.timeouts: list[float] = []

    def select(
        self, rlist: list[Any], _wlist: list[Any], _xlist: list[Any], timeout: float
    ) -> tuple[list[Any], list[Any], list[Any]]:
        if len(self.timeouts) >= self.scans:
            # serve_stdio treats a select error as a closed stream and returns.
            raise OSError("the script has run out of scans")
        if not self.timeouts:
            self.builder.await_entry()
        self.timeouts.append(timeout)
        if len(self.timeouts) <= 2:
            self.clock[0] += timeout / 2
            return list(rlist), [], []
        self.clock[0] += timeout
        return [], [], []


class BridgeProjectionCadenceTests(unittest.TestCase):
    """A projection that costs more than the poll interval must not run back to back.

    Found against the installed bridge on a 444-plan corpus, where one
    projection took ten seconds: polling it every second left the server
    permanently mid-build, so it never returned to the select loop and no
    client ever received a snapshot within its read timeout.

    The cached-projection half below is what the protocol loop still does with
    whatever it is handed. The worker half is what it is handed: the walk runs
    on its own thread, so a request is answered from the last projection while
    the next one runs, and the next one is owed one interval after the last one
    started rather than one interval after it finished.
    """

    def _session(
        self, cost: float
    ) -> tuple[bridge.BridgeSession, list[float], list[int]]:
        clock = [0.0]
        builds: list[int] = []

        def build() -> dict[str, Any]:
            builds.append(1)
            clock[0] += cost
            return {
                "digest": "d",
                "row_count": len(builds),
                "rows": [],
                "warnings": [],
                "truncated": False,
            }

        session = bridge.BridgeSession(
            snapshot_builder=build, clock=lambda: clock[0], poll_interval=1.0
        )
        return session, clock, builds

    def test_interval_grows_to_the_cost_of_the_last_projection(self) -> None:
        session, _, _ = self._session(cost=10.0)

        self.assertEqual(1.0, session.projection_interval())
        session.poll()
        self.assertEqual(10.0, session.projection_interval())

    def test_request_inside_the_interval_reuses_the_polled_projection(self) -> None:
        session, clock, builds = self._session(cost=10.0)
        session.poll()

        clock[0] += 5.0
        session.runs_snapshot()
        self.assertEqual(1, len(builds))

        clock[0] += 6.0
        session.runs_snapshot()
        self.assertEqual(2, len(builds))

    def test_a_request_defers_the_next_poll_by_a_whole_interval(self) -> None:
        session, clock, builds = self._session(cost=10.0)

        session.runs_snapshot()

        self.assertEqual(clock[0] + 10.0, session.next_projection_at())
        self.assertEqual(1, len(builds))

    def test_a_cheap_corpus_still_reprojects_every_poll_interval(self) -> None:
        session, clock, builds = self._session(cost=0.0)
        session.poll()

        clock[0] += 1.5
        session.runs_snapshot()

        self.assertEqual(2, len(builds))

    # ----------------------------------------------------------------
    # The background worker: cadence, freshness, and what never waits.

    def _worker(
        self, cost: float, interval: float = 1.0
    ) -> tuple[bridge.ProjectionWorker, list[float], list[int]]:
        clock = [0.0]
        builds: list[int] = []

        def build() -> dict[str, Any]:
            builds.append(1)
            clock[0] += cost
            return {
                "digest": f"d{len(builds)}",
                "row_count": len(builds),
                "rows": [],
                "warnings": [],
                "truncated": False,
            }

        worker = bridge.ProjectionWorker(
            build, interval=interval, clock=lambda: clock[0]
        )
        return worker, clock, builds

    def test_an_overrunning_walk_owes_no_wait_before_the_next_one(self) -> None:
        """The regression: a ten-second walk used to observe change every twenty."""
        worker, clock, _ = self._worker(cost=10.0)

        self.assertEqual(0.0, worker.cycle())
        self.assertEqual(10.0, clock[0])
        self.assertEqual(0.0, worker.cycle())
        self.assertEqual(20.0, clock[0])
        self.assertEqual(10.0, worker.cadence()["observed_interval_seconds"])

    def test_a_walk_inside_the_interval_waits_out_the_declared_cadence(self) -> None:
        worker, clock, _ = self._worker(cost=0.1)

        owed = worker.cycle()

        self.assertAlmostEqual(0.9, owed)
        clock[0] += owed
        worker.cycle()
        self.assertEqual(1.0, worker.cadence()["observed_interval_seconds"])

    def test_the_cadence_reports_the_measured_bound_not_the_declared_floor(
        self,
    ) -> None:
        worker, clock, _ = self._worker(cost=10.0)
        worker.cycle()
        worker.cycle()

        clock[0] += 2.0
        cadence = worker.cadence()

        self.assertEqual(1.0, cadence["interval_seconds"])
        self.assertEqual(10.0, cadence["build_seconds"])
        self.assertEqual(2.0, cadence["age_seconds"])
        # Read at the start of a ten-second walk, ten seconds old when it
        # landed, two seconds old since, and not replaced for another ten.
        self.assertEqual(22.0, cadence["staleness_bound_seconds"])
        self.assertEqual(0, cadence["failed_projections"])

    def test_a_failed_walk_is_counted_and_does_not_replace_the_last_projection(
        self,
    ) -> None:
        clock = [0.0]
        outcomes: list[str] = ["ok", "boom"]

        def build() -> dict[str, Any]:
            clock[0] += 1.0
            if outcomes.pop(0) == "boom":
                raise OSError("the corpus went away")
            return {
                "digest": "d1",
                "row_count": 0,
                "rows": [],
                "warnings": [],
                "truncated": False,
            }

        worker = bridge.ProjectionWorker(build, interval=1.0, clock=lambda: clock[0])
        worker.cycle()
        worker.cycle()

        self.assertEqual("d1", worker.latest()["digest"])
        self.assertEqual(1, worker.cadence()["failed_projections"])

    def test_a_failed_walk_does_not_retime_the_snapshot_that_stays_published(
        self,
    ) -> None:
        """The regression: a cheap failure made an expensive walk look cheap."""
        clock = [0.0]
        costs: list[float] = [10.0, 0.1]

        def build() -> dict[str, Any]:
            cost = costs.pop(0)
            clock[0] += cost
            if cost == 0.1:
                raise OSError("the corpus went away")
            return {
                "digest": "d1",
                "row_count": 0,
                "rows": [],
                "warnings": [],
                "truncated": False,
            }

        worker = bridge.ProjectionWorker(build, interval=1.0, clock=lambda: clock[0])
        worker.cycle()
        after_success = worker.cadence()
        worker.cycle()
        cadence = worker.cadence()

        self.assertEqual("d1", worker.latest()["digest"])
        # The published walk cost ten seconds and is now 0.1 seconds old; the
        # failure that aged it must not claim to have built it in 0.1.
        self.assertEqual(10.0, cadence["build_seconds"])
        self.assertEqual(0.1, cadence["age_seconds"])
        self.assertEqual(20.1, cadence["staleness_bound_seconds"])
        self.assertEqual(
            after_success["observed_interval_seconds"],
            cadence["observed_interval_seconds"],
        )
        self.assertEqual(1, cadence["failed_projections"])

    def test_a_request_is_served_from_the_last_walk_while_the_next_one_runs(
        self,
    ) -> None:
        builder = _HeldBuilder()
        worker = bridge.ProjectionWorker(builder, interval=0.0)
        self.addCleanup(builder.let_through, 4)
        self.addCleanup(worker.stop)

        worker.start()
        builder.await_entry()
        builder.let_through()
        builder.await_entry()

        # A walk is in flight and holding. The published one still answers, and
        # answers now: a blocking read would sit here until the cleanup released
        # the builder and the test would time out instead of asserting.
        session = bridge.BridgeSession(snapshot_builder=worker.latest)

        self.assertEqual("d1", session.runs_snapshot()["digest"])
        self.assertEqual(2, builder.calls)

    def test_a_cold_request_refuses_retryably_while_the_first_walk_is_held(
        self,
    ) -> None:
        """The regression: a cold snapshot used to block on the whole first walk."""
        builder = _HeldBuilder()
        worker = bridge.ProjectionWorker(builder, interval=60.0)
        self.addCleanup(builder.let_through, 4)
        self.addCleanup(worker.stop)
        session = bridge.BridgeSession(snapshot_builder=worker.latest)

        worker.start()
        builder.await_entry()

        # The walk is in flight and holding, so this call returns only because
        # it refuses instead of waiting; a blocking read would sit here until
        # the cleanup released the builder and the test would time out.
        with self.assertRaises(bridge.BridgeError) as caught:
            session.runs_snapshot()

        self.assertEqual(bridge.ERROR_INTERNAL, caught.exception.code)
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(1, builder.calls)

    def test_the_loop_keeps_framing_while_the_first_walk_is_held(self) -> None:
        """The cold refusal is worth nothing if the loop is stuck behind the walk."""
        clock = [0.0]
        builder = _HeldBuilder()
        # Released only after the loop has returned, so every frame below was
        # framed while the first walk was still held.
        self.addCleanup(builder.let_through, 4)
        script = _ColdWalkScript(clock, builder, scans=3)
        read_fd, write_fd = os.pipe()
        os.write(
            write_fd,
            _request("r1", "runs.snapshot")
            + b"\n"
            + _request("r2", "runs.snapshot")
            + b"\n",
        )
        os.close(write_fd)
        writer = io.StringIO()

        with (
            mock.patch.object(bridge, "build_runs_snapshot", lambda **_k: builder()),
            mock.patch.object(bridge, "select", script),
            os.fdopen(read_fd, "rb") as reader,
        ):
            code = bridge.serve_stdio(
                reader=reader,
                writer=writer,
                diagnostics=io.StringIO(),
                include_default_dirs=False,
                poll_interval=1.0,
                clock=lambda: clock[0],
            )

        self.assertEqual(0, code)
        # One walk for the whole loop: neither request, nor the poll after them,
        # started a second one or waited on the first.
        self.assertEqual(1, builder.calls)
        # Each refusal owes a second, so the loop waits rather than spinning on
        # a projection that has not landed.
        self.assertEqual([1.0, 1.0, 1.0], script.timeouts)
        frames = [
            json.loads(line) for line in writer.getvalue().splitlines() if line.strip()
        ]
        self.assertEqual("hello", frames[0]["type"])
        self.assertEqual(["r1", "r2"], [frame["id"] for frame in frames[1:]])
        for frame in frames[1:]:
            self.assertEqual(bridge.ERROR_INTERNAL, frame["error"]["code"])
            self.assertTrue(frame["error"]["retryable"])

    def test_a_served_snapshot_carries_the_measured_cadence_not_the_floor(
        self,
    ) -> None:
        worker, clock, _ = self._worker(cost=10.0)
        session = bridge.BridgeSession(
            snapshot_builder=worker.latest,
            clock=lambda: clock[0],
            projection_cadence=worker.cadence,
        )
        worker.cycle()
        worker.cycle()

        cadence = session.runs_snapshot()["projection"]

        self.assertEqual(1.0, cadence["interval_seconds"])
        self.assertEqual(10.0, cadence["observed_interval_seconds"])
        self.assertEqual(20.0, cadence["staleness_bound_seconds"])

    def test_the_cadence_block_never_moves_the_digest(self) -> None:
        """Measured durations jitter; a corpus that did not change did not change."""
        worker, clock, _ = self._worker(cost=10.0)
        session = bridge.BridgeSession(
            snapshot_builder=worker.latest,
            clock=lambda: clock[0],
            projection_cadence=worker.cadence,
        )
        worker.cycle()
        served = session.runs_snapshot()

        clock[0] += 5.0
        again = session.runs_snapshot()

        self.assertNotEqual(served["projection"], again["projection"])
        self.assertEqual(served["digest"], again["digest"])
        self.assertEqual(bridge.snapshot_digest(served), bridge.snapshot_digest(again))

    def test_a_session_without_a_projector_still_reports_its_own_cadence(self) -> None:
        session, clock, _ = self._session(cost=10.0)
        session.runs_snapshot()

        clock[0] += 3.0
        cadence = session.runs_snapshot()["projection"]

        self.assertEqual(10.0, cadence["build_seconds"])
        self.assertEqual(3.0, cadence["age_seconds"])
        self.assertEqual(10.0, cadence["observed_interval_seconds"])

    def test_a_failed_first_walk_refuses_retryably_rather_than_waiting(self) -> None:
        def build() -> dict[str, Any]:
            raise OSError("the state root vanished")

        worker = bridge.ProjectionWorker(build, interval=60.0)
        session = bridge.BridgeSession(snapshot_builder=worker.latest)
        worker.cycle()

        with self.assertRaises(bridge.BridgeError) as caught:
            session.runs_snapshot()

        self.assertEqual(bridge.ERROR_INTERNAL, caught.exception.code)
        self.assertTrue(caught.exception.retryable)
        self.assertIn("the state root vanished", caught.exception.message)

    def test_a_stopped_worker_refuses_instead_of_blocking_the_loop(self) -> None:
        worker, _, builds = self._worker(cost=0.0, interval=60.0)
        worker.stop()

        with self.assertRaises(bridge.ProjectionPending):
            worker.latest()

        self.assertEqual([], builds)


class _FailingProjector:
    """Publishes one projection and then fails every walk after it.

    That is the exact shape the regression needs: the success stamps a
    deadline, and every failure after it leaves that deadline in the past.
    """

    def __init__(self) -> None:
        self.calls = 0

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def latest(self) -> dict[str, Any]:
        self.calls += 1
        if self.calls > 1:
            raise OSError("the corpus went away")
        return {
            "digest": "d1",
            "row_count": 0,
            "rows": [],
            "warnings": [],
            "truncated": False,
        }

    def cadence(self) -> dict[str, Any]:
        return bridge.projection_cadence(
            interval=1.0,
            observed=1.0,
            build_seconds=0.0,
            age=0.0,
            failures=max(0, self.calls - 1),
        )


class _LoopScript:
    """Drives serve_stdio's select loop in fake time, recording every timeout.

    Standing in for ``select`` is what makes the timeout the loop actually
    passes observable; nothing else in the process reports it. A scan that
    finds the reader ready advances the clock by half the timeout, the way a
    real frame arriving early cuts a wait short.
    """

    def __init__(
        self, clock: list[float], *, ready_scans: set[int], scans: int
    ) -> None:
        self.clock = clock
        self.ready_scans = ready_scans
        self.scans = scans
        self.timeouts: list[float] = []

    def select(
        self, rlist: list[Any], _wlist: list[Any], _xlist: list[Any], timeout: float
    ) -> tuple[list[Any], list[Any], list[Any]]:
        if len(self.timeouts) >= self.scans:
            # serve_stdio treats a select error as a closed stream and returns.
            raise OSError("the script has run out of scans")
        self.timeouts.append(timeout)
        if len(self.timeouts) in self.ready_scans:
            self.clock[0] += timeout / 2
            return list(rlist), [], []
        self.clock[0] += timeout
        return [], [], []


class BridgeProjectionFailureBackoffTests(unittest.TestCase):
    """A projection that fails must not be retried with no delay at all.

    A failure publishes nothing, so the deadline of the last *successful*
    projection stays where it was — in the past. Without a deadline of its own
    the loop selected with timeout zero, immediately retried the same failing
    walk, and spent a core on it while the corpus stayed broken.
    """

    def _session(
        self, outcomes: list[str]
    ) -> tuple[bridge.BridgeSession, list[float], list[str]]:
        clock = [0.0]
        calls: list[str] = []

        def build() -> dict[str, Any]:
            outcome = outcomes.pop(0) if outcomes else "boom"
            calls.append(outcome)
            if outcome == "boom":
                raise OSError("the corpus went away")
            return {
                "digest": "d",
                "row_count": 0,
                "rows": [],
                "warnings": [],
                "truncated": False,
            }

        session = bridge.BridgeSession(
            snapshot_builder=build, clock=lambda: clock[0], poll_interval=1.0
        )
        return session, clock, calls

    def test_a_failed_projection_owes_a_bounded_nonzero_delay(self) -> None:
        session, clock, _ = self._session(["ok"])
        session.poll()

        clock[0] += 1.0
        self.assertEqual(0.0, session.select_timeout())
        session.poll()

        self.assertEqual(
            bridge.PROJECTION_RETRY_INTERVAL_SECONDS, session.select_timeout()
        )

    def test_consecutive_failures_never_scan_with_a_zero_timeout(self) -> None:
        session, clock, calls = self._session(["ok"])
        session.poll()
        timeouts: list[float] = []

        for _ in range(5):
            timeouts.append(session.select_timeout())
            clock[0] += timeouts[-1]
            session.poll()

        self.assertEqual([1.0] * 5, timeouts)
        self.assertEqual(["ok"] + ["boom"] * 5, calls)

    def test_the_retry_deadline_only_ever_moves_forward(self) -> None:
        session, clock, _ = self._session(["ok"])
        session.poll()
        session.poll()
        deferred = session.next_projection_at()

        clock[0] += 0.25
        session.poll()

        self.assertGreater(session.next_projection_at(), deferred)

    def test_a_recovered_projection_returns_to_the_declared_cadence(self) -> None:
        session, clock, _ = self._session(["ok", "boom", "ok"])
        session.poll()
        clock[0] += 1.0
        session.poll()

        clock[0] += session.select_timeout()
        session.poll()

        self.assertEqual(2.0, clock[0])
        self.assertEqual(3.0, session.next_projection_at())

    def test_a_failed_walk_owes_the_worker_a_wait_even_when_it_overruns(self) -> None:
        clock = [0.0]

        def build() -> dict[str, Any]:
            clock[0] += 10.0
            raise OSError("the corpus went away")

        worker = bridge.ProjectionWorker(build, interval=1.0, clock=lambda: clock[0])

        owed = worker.cycle()

        self.assertEqual(bridge.PROJECTION_RETRY_INTERVAL_SECONDS, owed)
        self.assertEqual(1, worker.cadence()["failed_projections"])

    def test_the_event_loop_keeps_answering_between_failed_projections(self) -> None:
        clock = [0.0]
        projector = _FailingProjector()
        script = _LoopScript(clock, ready_scans={1, 3}, scans=5)
        read_fd, write_fd = os.pipe()
        os.write(
            write_fd,
            _request("r1", "runs.snapshot")
            + b"\n"
            + _request("r2", "runs.snapshot")
            + b"\n",
        )
        os.close(write_fd)
        writer = io.StringIO()

        with (
            mock.patch.object(bridge, "ProjectionWorker", lambda *_a, **_k: projector),
            mock.patch.object(bridge, "select", script),
            os.fdopen(read_fd, "rb") as reader,
        ):
            code = bridge.serve_stdio(
                reader=reader,
                writer=writer,
                diagnostics=io.StringIO(),
                include_default_dirs=False,
                poll_interval=1.0,
                clock=lambda: clock[0],
            )

        self.assertEqual(0, code)
        # Every scan but the first two polls a failing projection, and the scan
        # after each failure still waits, so the loop never spins.
        self.assertEqual([1.0, 1.0, 1.0, 0.5, 1.0], script.timeouts)
        # Four walks for five scans: the second request was answered from the
        # last good projection without attempting another one.
        self.assertEqual(4, projector.calls)
        frames = [
            json.loads(line) for line in writer.getvalue().splitlines() if line.strip()
        ]
        self.assertEqual("hello", frames[0]["type"])
        self.assertEqual(["r1", "r2"], [frame["id"] for frame in frames[1:]])
        self.assertEqual(
            ["d1", "d1"], [frame["result"]["digest"] for frame in frames[1:]]
        )


class BridgeWorkbenchLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        _RecordingServer.instances = []

    def test_link_carries_the_token_in_the_query_and_the_plan_in_the_fragment(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir, _ = corpus.waiting_plan()
            lifecycle = bridge.WorkbenchLifecycle(
                state_dir=corpus.state_root, server_factory=_RecordingServer
            )
            session = corpus.session(workbench_lifecycle=lifecycle)

            result = session.workbench_ensure({"plan_dir": str(plan_dir)})

            self.assertEqual(bridge.WORKBENCH_FOCUS_REPLY, result["focus"])
            self.assertTrue(result["url"].startswith("http://127.0.0.1:54321/?token="))
            query, _, fragment = result["url"].partition("#")
            self.assertIn("token=test-token", query)
            self.assertNotIn("token", fragment)
            self.assertTrue(fragment.startswith("plan?dir="))
            self.assertIn("focus=reply", fragment)
            lifecycle.stop()

    def test_one_server_is_started_lazily_and_reused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir, _ = corpus.waiting_plan()
            lifecycle = bridge.WorkbenchLifecycle(
                state_dir=corpus.state_root, server_factory=_RecordingServer
            )
            session = corpus.session(workbench_lifecycle=lifecycle)

            self.assertEqual([], _RecordingServer.instances)
            first = session.workbench_ensure({"plan_dir": str(plan_dir)})
            second = session.workbench_ensure({"plan_dir": str(plan_dir)})

            self.assertEqual(1, len(_RecordingServer.instances))
            self.assertEqual(first["url"], second["url"])
            self.assertEqual(0, _RecordingServer.instances[0].kwargs["port"])
            self.assertEqual("127.0.0.1", _RecordingServer.instances[0].kwargs["host"])
            lifecycle.stop()

    def test_stop_shuts_the_server_down(self) -> None:
        lifecycle = bridge.WorkbenchLifecycle(server_factory=_RecordingServer)
        lifecycle.ensure()

        lifecycle.stop()
        lifecycle.stop()

        self.assertEqual(1, _RecordingServer.instances[0].shutdowns)
        self.assertEqual(1, _RecordingServer.instances[0].closes)

    def test_the_listener_notices_a_stop_well_inside_the_client_grace(self) -> None:
        """A slow poll turns every clean bridge exit into a kill.

        The client closes stdin and terminates a bridge that has not exited
        within a fraction of a second, and the stdlib default of half a second
        is longer than that whole budget.
        """
        lifecycle = bridge.WorkbenchLifecycle(server_factory=_RecordingServer)
        lifecycle.ensure()

        lifecycle.stop()

        self.assertEqual(
            [bridge.WORKBENCH_POLL_INTERVAL_SECONDS],
            _RecordingServer.instances[0].poll_intervals,
        )
        self.assertLess(bridge.WORKBENCH_POLL_INTERVAL_SECONDS, 0.1)

    def test_unsupported_focus_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir, _ = corpus.waiting_plan()
            lifecycle = bridge.WorkbenchLifecycle(server_factory=_RecordingServer)
            session = corpus.session(workbench_lifecycle=lifecycle)

            with self.assertRaises(bridge.BridgeError) as caught:
                session.workbench_ensure(
                    {"plan_dir": str(plan_dir), "focus": "not-a-panel"}
                )

            self.assertEqual(bridge.ERROR_INVALID_REQUEST, caught.exception.code)
            self.assertEqual([], _RecordingServer.instances)

    def test_session_without_a_lifecycle_reports_the_workbench_unavailable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir, _ = corpus.waiting_plan()

            with self.assertRaises(bridge.BridgeError) as caught:
                corpus.session().workbench_ensure({"plan_dir": str(plan_dir)})

            self.assertEqual(bridge.ERROR_WORKBENCH_UNAVAILABLE, caught.exception.code)


class BridgeDispatchTests(unittest.TestCase):
    def test_mutating_methods_answer_over_the_frame_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir, _ = corpus.waiting_plan()
            row = corpus.row_for(plan_dir)
            session = corpus.session()

            response = session.handle_frame(
                _request(
                    "r1",
                    intervention.ACTION_REPLY,
                    plan_dir=str(plan_dir),
                    operation_id="op-1",
                    action_token=row["action_token"],
                    text=REPLY_TEXT,
                )
            )

            self.assertIsNotNone(response)
            assert response is not None
            self.assertTrue(response["ok"])
            self.assertEqual("r1", response["id"])
            self.assertEqual(bridge.OPERATION_ACCEPTED, response["result"]["status"])
            self.assertNotIn(REPLY_TEXT, json.dumps(response))

    def test_missing_operation_id_is_an_invalid_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir, _ = corpus.waiting_plan()

            response = corpus.session().handle_frame(
                _request("r1", intervention.ACTION_REPLY, plan_dir=str(plan_dir))
            )

            assert response is not None
            self.assertFalse(response["ok"])
            self.assertEqual(bridge.ERROR_INVALID_REQUEST, response["error"]["code"])

    def test_unknown_method_still_reports_unknown_method(self) -> None:
        session = bridge.BridgeSession(
            snapshot_builder=lambda: {"digest": "d", "row_count": 0, "rows": []}
        )

        response = session.handle_frame(_request("r1", "run.explode"))

        assert response is not None
        self.assertEqual(bridge.ERROR_UNKNOWN_METHOD, response["error"]["code"])


class BridgeDiagnosticsTests(unittest.TestCase):
    def test_serve_stdio_keeps_reply_text_out_of_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir, _ = corpus.waiting_plan()
            row = corpus.row_for(plan_dir)
            frames = _request(
                "r1",
                intervention.ACTION_REPLY,
                plan_dir=str(plan_dir),
                operation_id="op-1",
                action_token=row["action_token"],
                text=REPLY_TEXT,
            )
            read_fd, write_fd = os.pipe()
            os.write(write_fd, frames + b"\n")
            os.close(write_fd)
            writer = io.StringIO()
            diagnostics = io.StringIO()

            with os.fdopen(read_fd, "rb") as reader:
                bridge.serve_stdio(
                    reader=reader,
                    writer=writer,
                    diagnostics=diagnostics,
                    state_dir=corpus.state_root,
                    plan_roots=(corpus.plans_root,),
                    include_default_dirs=False,
                    poll_interval=3600.0,
                )

            self.assertNotIn(REPLY_TEXT, diagnostics.getvalue())
            self.assertNotIn(REPLY_TEXT, writer.getvalue())
            self.assertIn('"ok":true', writer.getvalue())
            self.assertIn(
                mailbox.KIND_REPLY,
                [line["kind"] for line in corpus.mailbox_lines(plan_dir)],
            )


if __name__ == "__main__":
    unittest.main()
