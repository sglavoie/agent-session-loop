# ruff: noqa: F403, F405, I001
from .asl_qna_context_support import _QnAContextFixture
from .test_asl_qna_shared import *


class QnALiveContextTests(_QnAContextFixture):
    def test_load_qna_context_returns_structured_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path = self._seed_plan(tmp_dir)
            last_message_path = str(tmp_dir / "last-message.txt")
            self._write_state(state_path, last_message_path=last_message_path)
            self._write_last_message(last_message_path, "Which option, 1a or 1b?\n")

            context = qna.load_qna_context(plan_dir, state_dir=state_dir)

            self.assertEqual("1", context.session_id)
            self.assertEqual("1", context.next_pending_id)
            self.assertEqual("run-1", context.run_id)
            self.assertEqual("runner-pause", context.pause_reason)
            self.assertEqual("agent asked a question", context.pause_message)
            self.assertEqual({"session": 1}, context.pause_details)
            self.assertEqual("fake", context.harness)
            self.assertEqual("test-model", context.model)
            self.assertEqual("medium", context.effort)
            self.assertEqual(1, context.turn_index)
            self.assertEqual(Path(last_message_path), context.last_message_path)
            self.assertEqual("Which option, 1a or 1b?\n", context.last_message)
            self.assertEqual(state_path, context.state_path)
            self.assertEqual(plans_files.canonicalize_path(plan_dir), context.plan_dir)

    def test_load_qna_context_accepts_live_waiting_question(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path = self._seed_plan(tmp_dir)
            last_message_path = str(tmp_dir / "last-message.txt")
            waiting = {
                "question_id": "question-1",
                "session": 1,
                "session_id": "1",
                "session_number": 1,
                "turn_index": 1,
                "started_at": "2999-07-24T12:00:00+00:00",
                "base_deadline": "2999-07-24T12:05:00+00:00",
                "hard_deadline": "2999-07-24T12:15:00+00:00",
                "last_lease_activity_at": "2999-07-24T12:01:00+00:00",
            }
            self._write_state(
                state_path,
                paused=False,
                last_message_path=last_message_path,
                payload_overrides={
                    "waiting_for_reply": waiting,
                    "run_config": {"state_dir_override": str(state_dir)},
                },
            )
            self._write_last_message(last_message_path, "Choose blue or green?\n")
            state_locks.acquire_lock(plan_dir, state_dir)

            try:
                context = qna.load_qna_context(plan_dir, state_dir=state_dir)
                payload = qna.questions_payload(context)
            finally:
                state_locks.release_lock(plan_dir, state_dir)

            self.assertEqual("waiting", context.context_kind)
            self.assertEqual("question-1", context.question_id)
            self.assertEqual(waiting, context.waiting_for_reply)
            self.assertIsNone(context.pause_reason)
            self.assertIsNone(context.pause_message)
            self.assertEqual({}, context.pause_details)
            self.assertEqual("waiting", payload["context_kind"])
            self.assertEqual("question-1", payload["question_id"])
            self.assertEqual(waiting, payload["waiting_for_reply"])
            self.assertNotIn("resume_command", payload)
            self.assertNotIn("pause_reason", payload)
            plan = str(plans_files.canonicalize_path(plan_dir))
            self.assertEqual(
                f"asl inbox reply {plan} --to question-1 "
                f"--state-dir {state_dir} --text TEXT "
                "--wait-consumed --wait-timeout 300",
                payload["reply_command"],
            )
            self.assertEqual(
                f"asl inbox reply {plan} --to question-1 "
                f"--state-dir {state_dir} --text-stdin "
                "--wait-consumed --wait-timeout 300",
                payload["reply_stdin_command"],
            )
            self.assertEqual(
                f"asl inbox resolve {plan} --to question-1 "
                f"--action continue --state-dir {state_dir} "
                "--wait-consumed --wait-timeout 300",
                payload["continue_command"],
            )
            self.assertEqual(
                f"asl inbox resolve {plan} --to question-1 "
                f"--action pause --state-dir {state_dir} "
                "--wait-consumed --wait-timeout 300",
                payload["pause_command"],
            )
            self.assertIn(
                "# Live Agent Session - Resolver Prompt",
                payload["rendered_prompt"],
            )
            self.assertIn("## Resolution Choices", payload["rendered_prompt"])
            self.assertIn(payload["reply_command"], payload["rendered_prompt"])
            self.assertIn(payload["continue_command"], payload["rendered_prompt"])
            self.assertIn(payload["pause_command"], payload["rendered_prompt"])
            self.assertIn("Choose blue or green?", payload["rendered_prompt"])
            self.assertIn(
                "- Hard deadline: 2999-07-24T12:15:00+00:00",
                payload["rendered_prompt"],
            )
            self.assertNotIn("Unsupervised", payload["rendered_prompt"])

    def test_supervised_wait_past_its_anchor_still_offers_waiting_path(
        self,
    ) -> None:
        # The persisted anchor lapsed long ago; only the lease activity and
        # the ceiling span keep this wait live.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path = self._seed_plan(tmp_dir)
            last_message_path = str(tmp_dir / "last-message.txt")
            waiting = {
                "question_id": "question-1",
                "session": 1,
                "session_id": "1",
                "session_number": 1,
                "turn_index": 1,
                "started_at": "2026-07-24T12:00:00+00:00",
                "base_deadline": "2026-07-24T12:05:00+00:00",
                "unsupervised_deadline_at": "2026-07-24T12:30:00+00:00",
                "unsupervised_ceiling_seconds": 1800.0,
                "last_lease_activity_at": "2999-07-24T12:01:00+00:00",
            }
            self._write_state(
                state_path,
                paused=False,
                last_message_path=last_message_path,
                payload_overrides={
                    "waiting_for_reply": waiting,
                    "run_config": {"state_dir_override": str(state_dir)},
                },
            )
            self._write_last_message(last_message_path, "Choose blue or green?\n")
            state_locks.acquire_lock(plan_dir, state_dir)

            try:
                context = qna.load_qna_context(plan_dir, state_dir=state_dir)
                payload = qna.questions_payload(context)
                status_payload = state_status.status_payload(
                    plans_snapshot.inspect_plan(plan_dir),
                    state_dir,
                )
            finally:
                state_locks.release_lock(plan_dir, state_dir)

            self.assertEqual("waiting", context.context_kind)
            self.assertEqual("question-1", context.question_id)
            self.assertEqual(waiting, payload["waiting_for_reply"])
            self.assertEqual(
                state_payloads.WAITING_FOR_REPLY_STATUS_LIVE,
                status_payload["waiting_for_reply_status"],
            )
            self.assertIn(
                "- Unsupervised deadline: 2026-07-24T12:30:00+00:00 "
                "(rolls forward with every lease that proves a supervisor "
                "alive)",
                payload["rendered_prompt"],
            )
            self.assertIn(
                "- Unsupervised ceiling: 1800.0s",
                payload["rendered_prompt"],
            )
            self.assertNotIn("Hard deadline", payload["rendered_prompt"])

    def test_unwatched_wait_past_its_ceiling_recovers_as_expired(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path = self._seed_plan(tmp_dir)
            last_message_path = str(tmp_dir / "last-message.txt")
            waiting = {
                "question_id": "question-1",
                "session": 1,
                "session_id": "1",
                "session_number": 1,
                "turn_index": 1,
                "started_at": "2026-07-24T12:00:00+00:00",
                "base_deadline": "2026-07-24T12:05:00+00:00",
                "unsupervised_deadline_at": "2026-07-24T12:30:00+00:00",
                "unsupervised_ceiling_seconds": 1800.0,
                "last_lease_activity_at": None,
            }
            self._write_state(
                state_path,
                paused=False,
                last_message_path=last_message_path,
                payload_overrides={
                    "waiting_for_reply": waiting,
                    "run_config": {"state_dir_override": str(state_dir)},
                },
            )
            self._write_last_message(last_message_path, "Choose blue or green?\n")
            state_locks.acquire_lock(plan_dir, state_dir)

            try:
                context = qna.load_qna_context(plan_dir, state_dir=state_dir)
            finally:
                state_locks.release_lock(plan_dir, state_dir)

            self.assertEqual("pause", context.context_kind)
            self.assertEqual(
                qna.EXPIRED_WAIT_PAUSE_REASON,
                context.pause_reason,
            )
            self.assertEqual(
                {
                    "question_id": "question-1",
                    "unsupervised_deadline_at": "2026-07-24T12:30:00+00:00",
                    "unsupervised_ceiling_seconds": 1800.0,
                    "last_lease_activity_at": None,
                },
                context.pause_details,
            )

    def test_rebased_wait_after_suspend_still_offers_waiting_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path = self._seed_plan(tmp_dir)
            last_message_path = str(tmp_dir / "last-message.txt")
            # The wait started long ago and its original deadlines are far in
            # the past; a detected suspend re-anchored only the deadlines.
            raw_payload = {
                "waiting_for_reply": {
                    "question_id": "question-1",
                    "session": 1,
                    "session_id": "1",
                    "session_number": 1,
                    "turn_index": 1,
                    "started_at": "2026-07-24T12:00:00+00:00",
                    "base_deadline": "2026-07-24T12:05:00+00:00",
                    "unsupervised_deadline_at": "2026-07-24T12:30:00+00:00",
                    "unsupervised_ceiling_seconds": 1800.0,
                    "last_lease_activity_at": None,
                }
            }
            run_coordination_wait_state.rebase_waiting_deadlines(
                raw_payload,
                question_id="question-1",
                base_deadline=datetime(2999, 7, 24, 20, 3, 0, tzinfo=UTC),
                unsupervised_deadline_at=datetime(2999, 7, 24, 20, 28, 0, tzinfo=UTC),
                persistence=run_persistence._RunPersistence(write_state=False),
            )
            waiting = raw_payload["waiting_for_reply"]
            self.assertEqual(
                "2999-07-24T20:03:00+00:00",
                waiting["base_deadline"],
            )
            self.assertEqual(
                "2999-07-24T20:28:00+00:00",
                waiting["unsupervised_deadline_at"],
            )
            self._write_state(
                state_path,
                paused=False,
                last_message_path=last_message_path,
                payload_overrides={
                    "waiting_for_reply": waiting,
                    "run_config": {"state_dir_override": str(state_dir)},
                },
            )
            self._write_last_message(last_message_path, "Choose blue or green?\n")
            state_locks.acquire_lock(plan_dir, state_dir)

            try:
                context = qna.load_qna_context(plan_dir, state_dir=state_dir)
            finally:
                state_locks.release_lock(plan_dir, state_dir)

            self.assertEqual("waiting", context.context_kind)
            self.assertEqual("question-1", context.question_id)
            self.assertEqual(waiting, context.waiting_for_reply)
            self.assertIsNone(context.pause_reason)

    def test_live_waiting_identity_must_match_latest_completed_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path = self._seed_plan(tmp_dir)
            last_message_path = str(tmp_dir / "last-message.txt")
            self._write_last_message(last_message_path)
            self._write_state(
                state_path,
                paused=False,
                last_message_path=last_message_path,
                payload_overrides={
                    "waiting_for_reply": {
                        "question_id": "question-1",
                        "session": 1,
                        "session_id": "1",
                        "turn_index": 2,
                        "started_at": "2999-07-24T12:00:00+00:00",
                        "base_deadline": "2999-07-24T12:05:00+00:00",
                        "hard_deadline": "2999-07-24T12:15:00+00:00",
                        "last_lease_activity_at": None,
                    }
                },
            )
            state_locks.acquire_lock(plan_dir, state_dir)

            try:
                with self.assertRaises(qna.QnAError) as ctx:
                    qna.load_qna_context(plan_dir, state_dir=state_dir)
            finally:
                state_locks.release_lock(plan_dir, state_dir)

            self.assertEqual(
                qna.REASON_REPLY_CONTEXT_MISMATCH,
                ctx.exception.reason,
            )
            self.assertEqual(2, ctx.exception.details["waiting_turn_index"])
            self.assertEqual(1, ctx.exception.details["turn_index"])


def load_tests(
    loader: unittest.TestLoader, tests: unittest.TestSuite, pattern: str | None
) -> unittest.TestSuite:
    """Load only test cases defined in this focused section."""
    suite = unittest.TestSuite()
    cases = sorted(
        (
            value
            for value in globals().values()
            if isinstance(value, type)
            and issubclass(value, unittest.TestCase)
            and value.__module__ == __name__
        ),
        key=lambda case: case.__name__,
    )
    for case in cases:
        suite.addTests(loader.loadTestsFromTestCase(case))
    return suite
