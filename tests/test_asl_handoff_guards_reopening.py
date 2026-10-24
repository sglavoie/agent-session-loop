# ruff: noqa: F403, F405
"""Handoff reopening and artifact-adjudication tests."""

from __future__ import annotations

from agent_session_loop import handoff_guard_mailbox

# ruff: noqa: F403, F405
from tests.asl_handoff_guards_test_support import *


class RefusalReopeningTruthTests(_PlanCase):
    """A refusal reports a reopening only when the status actually moved (ASI-30).

    Session 1 of the fixture plan is ``complete`` and session 2 ``pending``;
    each reverting guard is driven against both.
    """

    #: Modules whose guards revert a session and then record the rejection.
    CALLER_MODULES = (
        "handoff_guards",
        "session_verification",
        "write_guards",
        "write_guards_legacy",
        "write_guards_protected",
    )

    def setUp(self) -> None:
        super().setUp()
        self.event_path = self.tmp_dir / "events.jsonl"
        self.result_path = self.tmp_dir / "result.json"
        self.persistence = run_persistence._RunPersistence()
        self.turn_payload: dict[str, Any] = {"turn_index": 1}
        self.payload: dict[str, Any] = {"run_id": "run-1"}

    def events_named(self, name: str) -> list[dict[str, Any]]:
        if not self.event_path.exists():
            return []
        records = [
            json.loads(line)
            for line in self.event_path.read_text(encoding="utf-8").splitlines()
        ]
        return [record["payload"] for record in records if record["name"] == name]

    def plan_artifact_guard(self, session_id: str) -> str:
        other = "2" if session_id == "1" else "1"
        digests_before = plans_content_hash.session_artifact_digests(
            self.plan_dir, exclude_session_id=session_id
        )
        path = self.session_path(other)
        path.write_text(
            path.read_text(encoding="utf-8") + "\n- **Note**: rewritten\n",
            encoding="utf-8",
        )
        before = self.snapshot()
        self.assertTrue(
            handoff_guards._enforce_plan_artifact_guard(
                event_path=self.event_path,
                payload=self.payload,
                persistence=self.persistence,
                before=before,
                after=before,
                digests_before=digests_before,
                runner_written=(),
                session_id=session_id,
                session_label=f"session {session_id}",
                session_details={"session_id": session_id},
                turn_payload=self.turn_payload,
                paths={"result": self.result_path},
                plan_cache=plans_snapshot.PlanSnapshotCache(),
                append_handoff_note=self.appender,
                reinspect=session_verification._reinspect_after_revert,
            )
        )
        return "plan_artifact_guard"

    def mailbox_completion_guard(self, session_id: str) -> str:
        with mock.patch.object(
            handoff_guard_mailbox,
            "_mailbox_completion_guard_evidence",
            return_value={"question_id": "q-1"},
        ):
            self.assertTrue(
                handoff_guards._enforce_mailbox_completion_guard(
                    event_path=self.event_path,
                    payload=self.payload,
                    persistence=self.persistence,
                    after=self.snapshot(),
                    session_id=session_id,
                    session_label=f"session {session_id}",
                    session_details={"session_id": session_id},
                    turn_index=1,
                    state_dir_arg=None,
                    turn_payload=self.turn_payload,
                    paths={"result": self.result_path},
                    plan_cache=plans_snapshot.PlanSnapshotCache(),
                    append_handoff_note=self.appender,
                    reinspect=session_verification._reinspect_after_revert,
                )
            )
        return "mailbox_completion_guard"

    def test_only_a_session_that_left_pending_is_reported_reopened(self) -> None:
        for guard in ("plan_artifact_guard", "mailbox_completion_guard"):
            for session_id, from_status in (("1", "complete"), ("2", "pending")):
                with self.subTest(guard=guard, session=session_id):
                    self.setUp()
                    record_key = getattr(self, guard)(session_id)
                    reopened = from_status != "pending"
                    self.assertEqual(
                        reopened,
                        self.turn_payload[record_key]["reverted_to_pending"],
                    )
                    self.assertEqual(
                        "pending",
                        self.snapshot().sessions[int(session_id) - 1].status,
                    )
                    # The failure note is appended either way.
                    self.assertEqual(1, len(self.appender.calls))
                    rejected = self.events_named(events.TURN_REJECTED_EVENT)
                    self.assertEqual([reopened], [r["reopened"] for r in rejected])
                    reopenings = self.events_named(events.SESSION_REOPENED_EVENT)
                    if reopened:
                        self.assertEqual(
                            ["complete"], [r["from_status"] for r in reopenings]
                        )
                    else:
                        self.assertEqual([], reopenings)

    def test_a_revert_decides_the_reopening_and_its_from_status(self) -> None:
        for from_status, expected in (
            ("pending", []),
            ("complete", ["complete"]),
            ("blocked", ["blocked"]),
        ):
            with self.subTest(from_status=from_status):
                self.setUp()
                turn_execution_commit.record_turn_rejected(
                    event_path=self.event_path,
                    payload=self.payload,
                    persistence=self.persistence,
                    turn_payload=self.turn_payload,
                    session_id="1",
                    guard="test-guard",
                    reason="blocked",
                    revert=handoff_guards.SessionRevert(
                        path=self.session_path("1"), from_status=from_status
                    ),
                )
                self.assertEqual(
                    expected,
                    [
                        r["from_status"]
                        for r in self.events_named(events.SESSION_REOPENED_EVENT)
                    ],
                )

    def test_no_reverting_caller_infers_a_reopening_from_the_rewrite(self) -> None:
        # Every caller of the revert hands the result to record_turn_rejected
        # as ``revert=`` and never reads "a file was rewritten" as "the session
        # was reopened".
        for module_name in self.CALLER_MODULES:
            with self.subTest(module=module_name):
                module = importlib.import_module(f"agent_session_loop.{module_name}")
                tree = ast.parse(inspect.getsource(module))
                for function in ast.walk(tree):
                    if not isinstance(function, ast.FunctionDef):
                        continue
                    calls = called_names(function)
                    if "_revert_session_status_to_pending" not in calls:
                        continue
                    if "record_turn_rejected" not in calls:
                        continue
                    source = (
                        ast.get_source_segment(inspect.getsource(module), function)
                        or ""
                    )
                    self.assertNotIn("reverted is not None,", source, function.name)
                    self.assertIn("revert=reverted", source, function.name)


class PlanArtifactAdjudicationTests(_PlanCase):
    """What the artifact guard records for a turn something else already ended.

    A turn can breach its write scope and rewrite a packet it was never given in
    the same turn. The write guard pauses first, and before ASI-119 that ended
    the turn before this guard ran at all, leaving the changed packets in the
    plan with nothing anywhere saying they were changed. Adjudicating records
    the verdict without taking the outcome away from the guard that reached it
    first.
    """

    def setUp(self) -> None:
        super().setUp()
        self.event_path = self.tmp_dir / "events.jsonl"
        self.result_path = self.tmp_dir / "result.json"
        self.persistence = run_persistence._RunPersistence()
        self.turn_payload: dict[str, Any] = {"turn_index": 1}
        # The shape a paused write guard leaves behind: the run is already
        # stopped, under a reason an operator is about to act on.
        self.payload: dict[str, Any] = {
            "run_id": "run-1",
            "paused": True,
            "pause_reason": events.PAUSE_REASON_WORKSPACE_WRITE_GUARD_UNAVAILABLE,
            "pause_details": {"session_id": "1"},
        }

    def enforce(self, **overrides: Any) -> bool:
        before = self.snapshot()
        return handoff_guards._enforce_plan_artifact_guard(
            event_path=self.event_path,
            payload=self.payload,
            persistence=self.persistence,
            before=before,
            after=before,
            digests_before=overrides.pop(
                "digests_before",
                plans_content_hash.session_artifact_digests(
                    self.plan_dir, exclude_session_id="1"
                ),
            ),
            runner_written=(),
            session_id="1",
            session_label="session 1",
            session_details={"session_id": "1"},
            turn_payload=self.turn_payload,
            paths={"result": self.result_path},
            plan_cache=plans_snapshot.PlanSnapshotCache(),
            append_handoff_note=self.appender,
            reinspect=session_verification._reinspect_after_revert,
            adjudicate_only=True,
            **overrides,
        )

    def tamper(self) -> None:
        path = self.session_path("2")
        path.write_text(
            path.read_text(encoding="utf-8") + "\n- **Note**: rewritten\n",
            encoding="utf-8",
        )

    def test_an_unchanged_packet_set_still_records_that_it_was_checked(self) -> None:
        self.assertFalse(self.enforce())
        guard = self.turn_payload["plan_artifact_guard"]
        self.assertTrue(guard["passed"])
        self.assertTrue(guard["adjudicated_only"])

    def test_a_tampered_packet_is_convicted_without_ending_the_turn_again(
        self,
    ) -> None:
        digests_before = plans_content_hash.session_artifact_digests(
            self.plan_dir, exclude_session_id="1"
        )
        self.tamper()

        self.assertFalse(self.enforce(digests_before=digests_before))

        guard = self.turn_payload["plan_artifact_guard"]
        self.assertFalse(guard["passed"])
        self.assertEqual(["session-2.md"], guard["changed_artifacts"])
        self.assertTrue(guard["adjudicated_only"])

    def test_the_conviction_defers_to_the_pause_that_ended_the_turn(self) -> None:
        digests_before = plans_content_hash.session_artifact_digests(
            self.plan_dir, exclude_session_id="1"
        )
        self.tamper()

        self.enforce(digests_before=digests_before)

        guard = self.turn_payload["plan_artifact_guard"]
        # The reason an operator has to act on is the one the first guard
        # published; this verdict is recorded beside it, not over it.
        self.assertEqual(
            events.PAUSE_REASON_WORKSPACE_WRITE_GUARD_UNAVAILABLE,
            self.payload["pause_reason"],
        )
        self.assertEqual(
            events.PAUSE_REASON_WORKSPACE_WRITE_GUARD_UNAVAILABLE,
            guard["deferred_to_pause_reason"],
        )
        self.assertEqual(guard, self.payload["pause_details"]["plan_artifact_guard"])

    def test_a_deferred_conviction_does_not_revert_the_session_twice(self) -> None:
        digests_before = plans_content_hash.session_artifact_digests(
            self.plan_dir, exclude_session_id="1"
        )
        self.tamper()

        self.enforce(digests_before=digests_before)

        # Whatever ended the turn first already withdrew the session if it
        # needed withdrawing; a second revert would append a second failure
        # note for one turn.
        self.assertFalse(
            self.turn_payload["plan_artifact_guard"]["reverted_to_pending"]
        )
        self.assertEqual([], self.appender.calls)
        self.assertEqual(
            "complete", plans_snapshot.inspect_plan(self.plan_dir).status_by_id["1"]
        )

    def test_a_deferred_conviction_still_rejects_the_turn_in_the_event_log(
        self,
    ) -> None:
        digests_before = plans_content_hash.session_artifact_digests(
            self.plan_dir, exclude_session_id="1"
        )
        self.tamper()

        self.enforce(digests_before=digests_before)

        rejections = [
            json.loads(line)
            for line in self.event_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(1, len(rejections))
        self.assertEqual(events.TURN_REJECTED_EVENT, rejections[0]["name"])
        rejection = rejections[0]["payload"]
        self.assertEqual("plan-artifact-guard", rejection["guard"])
        self.assertFalse(rejection["reopened"])
        self.assertEqual(["session-2.md"], rejection["detail"]["changed_artifacts"])
