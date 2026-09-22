"""Read-only stdio bridge: protocol framing and run projection.

Split from tests/test_asl_workbench_summary.py: the bridge reuses Workbench
classification but owns its own boundary — what a snapshot may contain, what
observation may touch, and what a malformed client may do to the server loop.
"""

from __future__ import annotations

import io
import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    bridge,
    cli,
    intervention,
    mailbox,
    plans,
    state,
    workbench,
)

from .support import (
    _WORKBENCH_DEAD_PID,
    _discovered,
    _make_workbench_plan,
    _write_lock_file,
    _write_state_payload,
    write_plan,
)

QUESTION_BODY = "Should we drop the v1 column before the backfill lands?"
PAUSE_MESSAGE = "Awaiting confirmation on the v1 column"


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

    def write_state(self, plan_dir: Path, payload: dict[str, Any]) -> Path:
        return _write_state_payload(
            plan_dir, self.state_root, {"plan_dir": str(plan_dir), **payload}
        )

    def paused_plan(self, name: str = "plan-alpha") -> Path:
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
                "pause_message": PAUSE_MESSAGE,
                "run_id": "20260619T140000Z-1",
                "harness": "claude",
                "kitty_window_id": 17,
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

    def ready_plan(self, name: str = "plan-ready") -> Path:
        return _make_workbench_plan(self.plans_root, name)

    def recent_plan(self, name: str = "plan-recent") -> Path:
        plan_dir = _make_workbench_plan(self.plans_root, name)
        self.write_state(
            plan_dir,
            {
                "ok": True,
                "paused": False,
                "run_id": "20260619T110000Z-1",
                "harness": "claude",
                "run_started_at": "2026-06-19T11:00:00+00:00",
                "run_finished_at": "2026-06-19T11:30:00+00:00",
            },
        )
        return plan_dir

    def complete_plan(self, name: str = "plan-complete") -> Path:
        plan_dir = self.plans_root / name
        write_plan(plan_dir, ("complete", "complete"))
        canonical = plans.canonicalize_path(plan_dir)
        self.write_state(
            canonical,
            {
                "ok": True,
                "paused": False,
                "run_id": "20260619T100000Z-1",
                "harness": "claude",
                "run_started_at": "2026-06-19T10:00:00+00:00",
                "run_finished_at": "2026-06-19T10:30:00+00:00",
            },
        )
        return canonical

    def running_plan(self, name: str = "plan-running") -> Path:
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
                "run_id": "20260619T150000Z-1",
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

    def waiting_plan(self, name: str = "plan-waiting") -> Path:
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
            run_id="20260619T160000Z-1",
            session_id="1",
            turn_index=4,
            state_dir=self.state_root,
        )
        self.write_state(
            plan_dir,
            {
                "ok": None,
                "paused": False,
                "run_id": "20260619T160000Z-1",
                "harness": "codex",
                "run_started_at": "2026-06-19T16:00:00+00:00",
                "active_turn": None,
                "waiting_for_reply": {
                    "question_id": question.id,
                    "session_id": "1",
                    "turn_index": 4,
                    "started_at": "2026-06-19T16:05:00+00:00",
                    "base_deadline": "2099-06-19T16:10:00+00:00",
                    "hard_deadline": "2099-06-19T16:15:00+00:00",
                    "last_lease_activity_at": None,
                },
            },
        )
        return plan_dir


def _request(request_id: str, method: str, **params: Any) -> bytes:
    payload: dict[str, Any] = {
        "type": "request",
        "protocol": bridge.PROTOCOL_VERSION,
        "id": request_id,
        "method": method,
        "params": params,
    }
    return json.dumps(payload).encode("utf-8")


class BridgeHelloTests(unittest.TestCase):
    def test_hello_declares_protocol_version_and_ordered_capabilities(self) -> None:
        payload = bridge.hello_payload()

        self.assertEqual("hello", payload["type"])
        self.assertEqual(2, payload["protocol"])
        self.assertEqual(list(bridge.CAPABILITIES), payload["capabilities"])
        # The handshake leads because nothing else a client wants to do is
        # available until it has completed one.
        self.assertEqual(
            [bridge.METHOD_HANDSHAKE, "runs.snapshot"],
            payload["capabilities"][:2],
        )
        self.assertEqual(list(bridge.EVENT_NAMES), payload["events"])
        self.assertEqual(list(bridge.MUTATION_METHODS), payload["mutation_methods"])
        self.assertEqual(1024 * 1024, payload["max_frame_bytes"])
        self.assertIsInstance(payload["asl_version"], str)

    def test_hello_names_the_contract_set_the_bridge_implements(self) -> None:
        payload = bridge.hello_payload()

        self.assertEqual(bridge.CONTRACT_SET_ID, payload["contract_set_id"])
        self.assertIs(True, payload["handshake_required"])

    def test_hello_frame_is_one_newline_terminated_utf8_line(self) -> None:
        frame = bridge.encode_frame(bridge.hello_payload())

        self.assertTrue(frame.endswith(b"\n"))
        self.assertEqual(1, frame.count(b"\n"))
        self.assertEqual(bridge.hello_payload(), json.loads(frame.decode("utf-8")))


class RunsProjectionTests(unittest.TestCase):
    def test_snapshot_excludes_ready_recent_and_complete_plans(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            paused = corpus.paused_plan()
            corpus.ready_plan()
            corpus.recent_plan()
            corpus.complete_plan()

            snapshot = corpus.snapshot()

            self.assertEqual(
                [str(paused)], [row["plan_dir"] for row in snapshot["rows"]]
            )
            self.assertEqual(1, snapshot["row_count"])
            self.assertFalse(snapshot["truncated"])

    def test_snapshot_rows_cover_only_active_and_actionable_states(self) -> None:
        self.assertEqual(
            {
                "paused-for-reply",
                "failed",
                "stale",
                "invalid",
                "verifying",
                "running",
            },
            set(bridge.ACTIVE_PRIMARY_STATES),
        )
        excluded = set(workbench.PRIMARY_STATES) - set(bridge.ACTIVE_PRIMARY_STATES)
        self.assertEqual({"ready", "recent", "complete"}, excluded)

    def test_snapshot_serialization_never_carries_question_bodies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            corpus.paused_plan()

            frame = bridge.encode_frame(corpus.snapshot()).decode("utf-8")

            self.assertNotIn(QUESTION_BODY, frame)
            self.assertNotIn("v1 column", frame)
            self.assertNotIn(PAUSE_MESSAGE, frame)

    def test_paused_row_carries_body_free_question_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir = corpus.paused_plan()
            mailbox.append_question(
                plan_dir,
                QUESTION_BODY,
                state_dir=corpus.state_root,
                run_id="20260619T140000Z-1",
                session_id="1",
            )

            (row,) = corpus.snapshot()["rows"]

            question = row["question"]
            self.assertEqual("pause", question["context_kind"])
            self.assertEqual(1, question["unanswered_count"])
            self.assertEqual(1, len(question["question_ids"]))
            self.assertEqual(question["question_ids"][0], question["question_id"])
            self.assertEqual("1", question["session_id"])
            self.assertNotIn("text", question)
            self.assertNotIn(QUESTION_BODY, json.dumps(question))

    def test_new_run_does_not_adopt_historical_questions_from_same_session(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir = corpus.paused_plan()
            for owner in ("previous-run", None):
                mailbox.append_question(
                    plan_dir,
                    "Historical question",
                    run_id=owner,
                    session_id="1",
                    state_dir=corpus.state_root,
                )
            (row,) = corpus.snapshot()["rows"]
            self.assertEqual(0, row["question"]["unanswered_count"])
            self.assertIsNone(row["question"]["question_id"])
            self.assertIsNone(row["question"]["asked_at"])
            self.assertEqual(
                2,
                len(
                    mailbox.unanswered_questions(plan_dir, state_dir=corpus.state_root)
                ),
            )

    def test_paused_activity_uses_latest_valid_run_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir = corpus.paused_plan()
            path = state.plan_state_dir(plan_dir, corpus.state_root) / "state.json"
            payload = json.loads(path.read_text())
            payload["run_finished_at"] = "2026-06-19T15:00:00+00:00"
            payload["turns"][0].update(
                finished_at="2026-06-19T14:55:00+00:00", last_activity_at="invalid"
            )
            corpus.write_state(plan_dir, payload)
            (row,) = corpus.snapshot()["rows"]
            self.assertEqual(payload["run_finished_at"], row["last_activity_at"])
            self.assertEqual("run-finished", row["last_activity_source"])
            payload["turns"][0]["last_activity_at"] = "2026-06-19T15:05:00+00:00"
            corpus.write_state(plan_dir, payload)
            (row,) = corpus.snapshot()["rows"]
            self.assertEqual("2026-06-19T15:05:00+00:00", row["last_activity_at"])
            self.assertEqual("turn-record", row["last_activity_source"])
            payload["run_finished_at"] = "invalid"
            payload["turns"][0].update(
                started_at=None, finished_at=None, last_activity_at=None
            )
            corpus.write_state(plan_dir, payload)
            (row,) = corpus.snapshot()["rows"]
            self.assertEqual(payload["run_started_at"], row["last_activity_at"])
            self.assertEqual("run-started", row["last_activity_source"])

    def test_row_projects_identity_state_and_window_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir = corpus.paused_plan()

            (row,) = corpus.snapshot()["rows"]

            self.assertEqual(str(plan_dir), row["plan_dir"])
            self.assertEqual("20260619T140000Z-1", row["run_id"])
            self.assertEqual(
                bridge.row_id(str(plan_dir), "20260619T140000Z-1"), row["row_id"]
            )
            self.assertEqual("paused-for-reply", row["primary_state"])
            self.assertEqual("awaiting-reply", row["phase"])
            self.assertEqual("needs-action", row["urgency_group"])
            self.assertEqual(
                workbench.URGENCY_RANK_BY_STATE["paused-for-reply"],
                row["urgency_rank"],
            )
            self.assertEqual("reply", row["recommended_action"])
            self.assertEqual(
                workbench.ACTIVITY_LABEL_BY_STATE["paused-for-reply"],
                row["activity_label"],
            )
            self.assertEqual("claude", row["harness"])
            self.assertEqual({"kitty_window_id": 17}, row["window"])
            self.assertEqual("agent-question", row["pause_reason"])

    def test_row_identity_is_stable_across_repeated_projections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            corpus.paused_plan()

            first = corpus.snapshot()
            second = corpus.snapshot()

            self.assertEqual(first["rows"], second["rows"])
            self.assertEqual(first["digest"], second["digest"])

    def test_rows_preserve_workbench_urgency_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            corpus.running_plan()
            corpus.paused_plan()

            snapshot = corpus.snapshot()

            self.assertEqual(
                ["paused-for-reply", "running"],
                [row["primary_state"] for row in snapshot["rows"]],
            )
            ranks = [row["urgency_rank"] for row in snapshot["rows"]]
            self.assertEqual(sorted(ranks), ranks)

    def test_running_plan_projects_active_turn_session_and_phase(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            corpus.running_plan()

            (row,) = corpus.snapshot()["rows"]

            self.assertEqual("running", row["primary_state"])
            self.assertEqual("running", row["phase"])
            self.assertEqual("2", row["session_id"])
            self.assertEqual("codex", row["harness"])
            self.assertIsNone(row["question"])

    def test_live_wait_between_turns_projects_reply_operation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            corpus.waiting_plan()

            (row,) = corpus.snapshot()["rows"]

            self.assertEqual("running", row["primary_state"])
            self.assertEqual("awaiting-reply", row["phase"])
            self.assertEqual("1", row["session_id"])
            self.assertEqual(1, row["question"]["unanswered_count"])
            self.assertEqual("live", row["question"]["waiting_status"])
            self.assertIn("question.reply", row["actions"])

    def test_live_wait_inside_turn_projects_reply_operation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir = corpus.running_plan()
            question = mailbox.append_question(
                plan_dir,
                QUESTION_BODY,
                state_dir=corpus.state_root,
                run_id="20260619T150000Z-1",
                session_id="2",
                turn_index=3,
            )
            path = state.plan_state_dir(plan_dir, corpus.state_root) / "state.json"
            payload = json.loads(path.read_text())
            payload["active_turn"]["input_wait_active"] = True
            corpus.write_state(plan_dir, payload)

            (row,) = corpus.snapshot()["rows"]

            self.assertEqual("running", row["primary_state"])
            self.assertEqual("awaiting-reply", row["phase"])
            self.assertEqual(question.id, row["question"]["question_id"])
            self.assertEqual("waiting", row["question"]["context_kind"])
            self.assertEqual("live", row["question"]["waiting_status"])
            self.assertIn("question.reply", row["actions"])
            self.assertEqual(
                intervention.action_token(
                    intervention.inspect_run(plan_dir, state_dir=corpus.state_root)
                ),
                row["action_token"],
            )

    def test_active_worker_wait_projects_between_input_wait_heartbeats(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir = corpus.running_plan()
            question, _ = mailbox.append_wait_question(
                plan_dir,
                QUESTION_BODY,
                state_dir=corpus.state_root,
                run_id="20260619T150000Z-1",
                session_id="2",
                turn_index=3,
                waiter_origin=mailbox.WAITER_ORIGIN_WORKER,
            )

            (row,) = corpus.snapshot()["rows"]

            self.assertEqual("running", row["primary_state"])
            self.assertEqual("awaiting-reply", row["phase"])
            self.assertEqual(question.id, row["question"]["question_id"])
            self.assertEqual("waiting", row["question"]["context_kind"])
            self.assertEqual("live", row["question"]["waiting_status"])
            self.assertIn("question.reply", row["actions"])
            self.assertEqual(
                intervention.action_token(
                    intervention.inspect_run(plan_dir, state_dir=corpus.state_root)
                ),
                row["action_token"],
            )

    def test_worker_wait_from_another_turn_does_not_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir = corpus.running_plan()
            mailbox.append_wait_question(
                plan_dir,
                QUESTION_BODY,
                state_dir=corpus.state_root,
                run_id="20260619T150000Z-1",
                session_id="2",
                turn_index=2,
                waiter_origin=mailbox.WAITER_ORIGIN_WORKER,
            )

            (row,) = corpus.snapshot()["rows"]

            self.assertEqual("running", row["primary_state"])
            self.assertEqual("running", row["phase"])
            self.assertIsNone(row["question"])

    def test_snapshot_warns_and_survives_a_plan_that_disappears(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            alive = corpus.paused_plan()
            ghost = corpus.paused_plan("plan-ghost")
            for path in sorted(ghost.rglob("*"), reverse=True):
                path.unlink() if path.is_file() else path.rmdir()
            ghost.rmdir()

            snapshot = corpus.snapshot()
            by_plan = {row["plan_dir"]: row for row in snapshot["rows"]}

            self.assertEqual(2, snapshot["row_count"])
            self.assertEqual("paused-for-reply", by_plan[str(alive)]["primary_state"])
            self.assertEqual("invalid", by_plan[str(ghost)]["primary_state"])

    def test_digest_ignores_nothing_but_itself(self) -> None:
        snapshot = {
            "protocol": 1,
            "rows": [],
            "row_count": 0,
            "warnings": [],
            "truncated": False,
        }
        digest = bridge.snapshot_digest(snapshot)

        self.assertEqual(digest, bridge.snapshot_digest({**snapshot, "digest": digest}))
        self.assertNotEqual(
            digest, bridge.snapshot_digest({**snapshot, "row_count": 1})
        )


class ObservationalReadTests(unittest.TestCase):
    def test_snapshot_does_not_reclaim_a_dead_local_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir = _make_workbench_plan(corpus.plans_root)
            lock_path = _write_lock_file(
                plan_dir,
                corpus.state_root,
                pid=_WORKBENCH_DEAD_PID,
                hostname=socket.gethostname(),
            )

            snapshot = corpus.snapshot()

            self.assertTrue(lock_path.exists(), "bridge observation removed a lock")
            self.assertEqual(
                ["stale"], [row["primary_state"] for row in snapshot["rows"]]
            )

    def test_summary_builder_reclaims_only_when_asked_to(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_root = tmp_path / "state"
            plan_dir = _make_workbench_plan(tmp_path)
            lock_path = _write_lock_file(
                plan_dir,
                state_root,
                pid=_WORKBENCH_DEAD_PID,
                hostname=socket.gethostname(),
            )

            workbench.build_workbench_summary(
                _discovered(plan_dir), state_dir=state_root, reclaim=False
            )
            self.assertTrue(lock_path.exists())

            workbench.build_workbench_summary(
                _discovered(plan_dir), state_dir=state_root
            )
            self.assertFalse(lock_path.exists())


class BridgeSessionTests(unittest.TestCase):
    def _session(self, snapshot: dict[str, Any] | None = None) -> bridge.BridgeSession:
        payload = snapshot or {
            "protocol": 1,
            "rows": [],
            "row_count": 0,
            "warnings": [],
            "truncated": False,
            "digest": "seed",
        }
        return bridge.BridgeSession(snapshot_builder=lambda: payload)

    def test_runs_snapshot_request_returns_a_correlated_response(self) -> None:
        response = self._session().handle_frame(_request("req-1", "runs.snapshot"))

        self.assertEqual("response", response["type"])
        self.assertEqual("req-1", response["id"])
        self.assertTrue(response["ok"])
        self.assertEqual(0, response["result"]["row_count"])
        self.assertNotIn("error", response)

    def test_unknown_method_returns_a_stable_error_without_ending_the_loop(
        self,
    ) -> None:
        session = self._session()

        response = session.handle_frame(_request("req-1", "runs.explode"))

        self.assertEqual("req-1", response["id"])
        self.assertFalse(response["ok"])
        self.assertEqual("unknown-method", response["error"]["code"])
        self.assertFalse(response["error"]["retryable"])
        self.assertEqual("runs.explode", response["error"]["details"]["method"])
        self.assertTrue(
            session.handle_frame(_request("req-2", "runs.snapshot"))["ok"],
            "server rejected a valid request after an unknown method",
        )

    def test_invalid_protocol_version_is_rejected_by_code(self) -> None:
        frame = json.dumps(
            {
                "type": "request",
                "protocol": 99,
                "id": "req-1",
                "method": "runs.snapshot",
                "params": {},
            }
        ).encode("utf-8")

        response = self._session().handle_frame(frame)

        self.assertFalse(response["ok"])
        self.assertEqual("unsupported-protocol", response["error"]["code"])
        self.assertEqual("2", response["error"]["details"]["supported"])

    def test_malformed_json_is_answered_without_a_correlation_id(self) -> None:
        response = self._session().handle_frame(b"{not json")

        self.assertIsNone(response["id"])
        self.assertFalse(response["ok"])
        self.assertEqual("malformed-json", response["error"]["code"])

    def test_missing_request_id_and_method_are_invalid_requests(self) -> None:
        session = self._session()
        no_id = json.dumps(
            {"type": "request", "protocol": 2, "method": "runs.snapshot"}
        ).encode("utf-8")
        no_method = json.dumps(
            {"type": "request", "protocol": 2, "id": "req-1"}
        ).encode("utf-8")
        bad_params = json.dumps(
            {
                "type": "request",
                "protocol": 2,
                "id": "req-1",
                "method": "runs.snapshot",
                "params": [],
            }
        ).encode("utf-8")

        for frame in (no_id, no_method, bad_params):
            response = session.handle_frame(frame)
            self.assertFalse(response["ok"])
            self.assertEqual("invalid-request", response["error"]["code"])

    def test_a_failing_projection_is_a_retryable_internal_error(self) -> None:
        def explode() -> dict[str, Any]:
            raise RuntimeError("state root vanished")

        session = bridge.BridgeSession(snapshot_builder=explode)

        response = session.handle_frame(_request("req-1", "runs.snapshot"))

        self.assertFalse(response["ok"])
        self.assertEqual("internal-error", response["error"]["code"])
        self.assertTrue(response["error"]["retryable"])


class RunsChangedEventTests(unittest.TestCase):
    def test_unchanged_digest_never_emits_a_duplicate_event(self) -> None:
        payload = {
            "protocol": 1,
            "rows": [],
            "row_count": 0,
            "warnings": [],
            "truncated": False,
            "digest": "same",
        }
        session = bridge.BridgeSession(snapshot_builder=lambda: payload)

        first = session.poll()
        second = session.poll()
        third = session.poll()

        self.assertEqual("runs.changed", first["event"])
        self.assertEqual(1, first["seq"])
        self.assertIsNone(second)
        self.assertIsNone(third)

    def test_event_sequence_is_monotonic_across_changes(self) -> None:
        digests = iter(["a", "b", "b", "c"])
        session = bridge.BridgeSession(
            snapshot_builder=lambda: {
                "protocol": 1,
                "rows": [],
                "row_count": 0,
                "warnings": [],
                "truncated": False,
                "digest": next(digests),
            }
        )

        events = [event for event in (session.poll() for _ in range(4)) if event]
        seqs = [event["seq"] for event in events]

        self.assertEqual([1, 2, 3], seqs)

    def test_a_served_snapshot_suppresses_the_next_unchanged_event(self) -> None:
        payload = {
            "protocol": 1,
            "rows": [],
            "row_count": 0,
            "warnings": [],
            "truncated": False,
            "digest": "same",
        }
        session = bridge.BridgeSession(snapshot_builder=lambda: payload)

        session.handle_frame(_request("req-1", "runs.snapshot"))

        self.assertIsNone(session.poll())
        self.assertEqual(0, session.seq)


class FrameBoundTests(unittest.TestCase):
    def test_oversize_inbound_frame_is_rejected_and_the_line_is_drained(self) -> None:
        oversize = b"x" * (bridge.MAX_FRAME_BYTES + 10) + b"\n"
        reader = io.BytesIO(oversize + b'{"type":"request"}\n')

        with self.assertRaises(bridge._FrameTooLarge):
            bridge.read_frame(reader)

        self.assertEqual(b'{"type":"request"}', bridge.read_frame(reader))

    def test_frame_of_exactly_the_ceiling_is_accepted(self) -> None:
        payload = b"y" * bridge.MAX_FRAME_BYTES
        reader = io.BytesIO(payload + b"\n")

        self.assertEqual(payload, bridge.read_frame(reader))

    def test_end_of_stream_reads_as_none(self) -> None:
        self.assertIsNone(bridge.read_frame(io.BytesIO(b"")))

    def test_per_field_bounds_keep_corrupt_state_inside_the_frame(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir = _make_workbench_plan(corpus.plans_root)
            corpus.write_state(
                plan_dir,
                {
                    "ok": False,
                    "paused": True,
                    "reason": "z" * 100_000,
                    "pause_reason": "z" * 100_000,
                    "run_id": "r" * 100_000,
                    "harness": "h" * 100_000,
                    "turns": [{"session_id": "1", "turn_index": 1}],
                },
            )

            snapshot = corpus.snapshot()
            (row,) = snapshot["rows"]

            self.assertLessEqual(len(row["pause_reason"]), bridge.MAX_TEXT_CHARS)
            self.assertLessEqual(len(row["run_id"]), bridge.MAX_ID_CHARS)
            self.assertLessEqual(len(row["harness"]), bridge.MAX_ID_CHARS)
            self.assertLessEqual(
                len(bridge.encode_frame(snapshot)), bridge.MAX_FRAME_BYTES
            )

    def test_aggregate_row_bound_is_enforced_before_serialization(self) -> None:
        self.assertEqual(200, bridge.MAX_ROWS)
        rows = [
            {
                "urgency_rank": 10,
                "plan_dir": f"/plans/{index:04d}",
                "label": None,
                "last_activity_at": None,
            }
            for index in range(bridge.MAX_ROWS + 5)
        ]
        ordered = sorted(rows, key=bridge._row_sort_key)

        self.assertEqual(rows, ordered)

    def test_backlog_cannot_crowd_live_work_out_of_capped_snapshot(self) -> None:
        backlog = [
            {
                "primary_state": "invalid",
                "urgency_rank": 40,
                "plan_dir": f"/plans/{index:04d}",
                "last_activity_at": None,
            }
            for index in range(bridge.MAX_ROWS + 5)
        ]
        live = [
            {
                "primary_state": status,
                "urgency_rank": 60,
                "plan_dir": f"/plans/{status}",
                "last_activity_at": "2026-09-17T17:00:00+00:00",
            }
            for status in ("running", "verifying")
        ]
        recent = {
            "primary_state": "paused-for-reply",
            "urgency_rank": 10,
            "plan_dir": "/plans/recent",
            "last_activity_at": "2026-09-17T16:00:00+00:00",
        }
        discovered = workbench.DiscoveryResult(plans=tuple(range(len(backlog) + 3)))
        summaries = [
            mock.Mock(primary_state=row["primary_state"])
            for row in [*backlog, *live, recent]
        ]
        with (
            mock.patch.object(workbench, "discover_plans", return_value=discovered),
            mock.patch.object(
                workbench, "build_workbench_summary", side_effect=summaries
            ),
            mock.patch.object(
                bridge, "project_row", side_effect=[*backlog, *live, recent]
            ),
        ):
            snapshot = bridge.build_runs_snapshot()
        self.assertTrue(snapshot["truncated"])
        self.assertEqual(bridge.MAX_ROWS, snapshot["row_count"])
        for row in [*live, recent]:
            self.assertIn(row, snapshot["rows"])
        self.assertEqual(
            sorted(snapshot["rows"], key=bridge._row_sort_key), snapshot["rows"]
        )


class _PrimedProjectionWorker(bridge.ProjectionWorker):
    """A worker whose first walk has landed before the loop reads a frame.

    A cold `runs.snapshot` is refused retryably by design, and a client answers
    that by asking again. These tests are about framing and dispatch over a real
    pipe, and a one-shot stream cannot re-ask; walking the corpus once on the
    loop thread before the loop starts is what makes them assert a projection
    instead of a race with the worker.
    """

    def start(self) -> None:
        self.cycle()
        super().start()


class BridgeServeStdioTests(unittest.TestCase):
    def _serve(self, request_frames: bytes, corpus: _Corpus) -> list[dict[str, Any]]:
        read_fd, write_fd = os.pipe()
        os.write(write_fd, request_frames)
        os.close(write_fd)
        writer = io.StringIO()
        with (
            mock.patch.object(bridge, "ProjectionWorker", _PrimedProjectionWorker),
            os.fdopen(read_fd, "rb") as reader,
        ):
            code = bridge.serve_stdio(
                reader=reader,
                writer=writer,
                diagnostics=io.StringIO(),
                state_dir=corpus.state_root,
                plan_roots=(corpus.plans_root,),
                include_default_dirs=False,
                poll_interval=3600.0,
            )
        self.assertEqual(0, code)
        return [
            json.loads(line) for line in writer.getvalue().splitlines() if line.strip()
        ]

    def test_server_speaks_hello_before_reading_and_exits_cleanly_at_eof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            frames = self._serve(b"", _Corpus(Path(tmp)))

            self.assertEqual(1, len(frames))
            self.assertEqual("hello", frames[0]["type"])

    def test_server_answers_a_snapshot_request_over_the_pipe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir = corpus.paused_plan()

            frames = self._serve(_request("req-1", "runs.snapshot") + b"\n", corpus)

            self.assertEqual("hello", frames[0]["type"])
            response = frames[1]
            self.assertEqual("response", response["type"])
            self.assertEqual("req-1", response["id"])
            self.assertEqual(
                [str(plan_dir)],
                [row["plan_dir"] for row in response["result"]["rows"]],
            )

    def test_server_survives_malformed_and_unknown_frames_in_one_stream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            corpus.paused_plan()

            frames = self._serve(
                b"{not json\n"
                + _request("req-1", "runs.nope")
                + b"\n"
                + _request("req-2", "runs.snapshot")
                + b"\n",
                corpus,
            )

            self.assertEqual(
                ["malformed-json", "unknown-method"],
                [frame["error"]["code"] for frame in frames[1:3]],
            )
            self.assertTrue(frames[3]["ok"])

    def test_broken_stdout_stops_the_loop_without_raising(self) -> None:
        class BrokenWriter(io.StringIO):
            def write(self, _text: str) -> int:
                raise BrokenPipeError

        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            state_root.mkdir()
            read_fd, write_fd = os.pipe()
            os.close(write_fd)

            with os.fdopen(read_fd, "rb") as reader:
                code = bridge.serve_stdio(
                    reader=reader,
                    writer=BrokenWriter(),
                    diagnostics=io.StringIO(),
                    state_dir=state_root,
                )

            self.assertEqual(0, code)


class BridgeCliTests(unittest.TestCase):
    def test_bridge_serve_parses_the_documented_invocation(self) -> None:
        args = cli.build_parser().parse_args(
            ["bridge", "serve", "--stdio", "--protocol", "1"]
        )

        self.assertEqual("bridge", args.command)
        self.assertEqual("serve", args.bridge_command)
        self.assertTrue(args.stdio)
        self.assertEqual(1, args.protocol)
        self.assertEqual("bridge:bridge_serve_from_args", args.handler_ref)

    def test_bridge_serve_accepts_state_dir_and_repeated_plan_roots(self) -> None:
        args = cli.build_parser().parse_args(
            [
                "bridge",
                "serve",
                "--stdio",
                "--state-dir",
                "/tmp/state",
                "--plan-root",
                "/tmp/a",
                "--plan-root",
                "/tmp/b",
            ]
        )

        self.assertEqual("/tmp/state", args.state_dir)
        self.assertEqual(["/tmp/a", "/tmp/b"], args.plan_root)

    def test_the_retired_protocol_cannot_be_selected(self) -> None:
        """Protocol 1 fails before a byte of protocol is written."""
        args = cli.build_parser().parse_args(
            ["bridge", "serve", "--stdio", "--protocol", "1"]
        )

        self.assertEqual(2, bridge.bridge_serve_from_args(args))


if __name__ == "__main__":
    unittest.main()
