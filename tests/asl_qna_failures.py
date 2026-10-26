# ruff: noqa: F403, F405, I001
from .asl_qna_context_support import _QnAContextFixture
from .test_asl_qna_shared import *
from .support_base import load_section_tests


class QnAFailureTests(_QnAContextFixture):
    def test_load_qna_context_raises_no_paused_run_when_state_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, _state_path = self._seed_plan(tmp_dir)

            with self.assertRaises(qna.QnAError) as ctx:
                qna.load_qna_context(plan_dir, state_dir=state_dir)

            self.assertEqual(qna.REASON_NO_PAUSED_RUN, ctx.exception.reason)
            self.assertIn("state", ctx.exception.error.lower())

    def test_load_qna_context_raises_no_paused_run_when_state_invalid_json(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path = self._seed_plan(tmp_dir)
            state_path.write_text("not json", encoding="utf-8")

            with self.assertRaises(qna.QnAError) as ctx:
                qna.load_qna_context(plan_dir, state_dir=state_dir)

            self.assertEqual(qna.REASON_NO_PAUSED_RUN, ctx.exception.reason)
            self.assertEqual(str(state_path), ctx.exception.details["state_path"])

    def test_load_qna_context_raises_no_paused_run_when_state_undecodable(
        self,
    ) -> None:
        for label, content in (
            ("non-utf8", b"\xff" + json.dumps({"paused": True}).encode()),
            # Deep enough that the decoder raises RecursionError instead.
            ("deeply nested", b"[" * 1_000_000),
        ):
            with self.subTest(label), tempfile.TemporaryDirectory() as tmp:
                plan_dir, state_dir, state_path = self._seed_plan(Path(tmp))
                state_path.write_bytes(content)

                with self.assertRaises(qna.QnAError) as ctx:
                    qna.load_qna_context(plan_dir, state_dir=state_dir)

                self.assertEqual(qna.REASON_NO_PAUSED_RUN, ctx.exception.reason)
                self.assertEqual(str(state_path), ctx.exception.details["state_path"])

    def test_load_qna_context_raises_no_paused_run_when_not_paused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path = self._seed_plan(tmp_dir)
            last_message_path = str(tmp_dir / "last-message.txt")
            self._write_last_message(last_message_path)
            self._write_state(
                state_path,
                paused=False,
                last_message_path=last_message_path,
            )

            with self.assertRaises(qna.QnAError) as ctx:
                qna.load_qna_context(plan_dir, state_dir=state_dir)

            self.assertEqual(qna.REASON_NO_PAUSED_RUN, ctx.exception.reason)
            self.assertFalse(ctx.exception.details["paused"])

    def test_load_qna_context_raises_no_paused_run_when_no_turns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path = self._seed_plan(tmp_dir)
            self._write_state(state_path, turns=[])

            with self.assertRaises(qna.QnAError) as ctx:
                qna.load_qna_context(plan_dir, state_dir=state_dir)

            self.assertEqual(qna.REASON_NO_PAUSED_RUN, ctx.exception.reason)
            self.assertIn("turn", ctx.exception.error.lower())

    def test_load_qna_context_raises_missing_question_context_when_artifact_absent(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path = self._seed_plan(tmp_dir)
            last_message_path = str(tmp_dir / "missing.txt")
            self._write_state(state_path, last_message_path=last_message_path)

            with self.assertRaises(qna.QnAError) as ctx:
                qna.load_qna_context(plan_dir, state_dir=state_dir)

            self.assertEqual(qna.REASON_MISSING_QUESTION_CONTEXT, ctx.exception.reason)
            self.assertEqual(
                last_message_path, ctx.exception.details["last_message_path"]
            )

    def test_load_qna_context_raises_missing_question_context_when_artifact_empty(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path = self._seed_plan(tmp_dir)
            last_message_path = str(tmp_dir / "empty.txt")
            self._write_last_message(last_message_path, "   \n")
            self._write_state(state_path, last_message_path=last_message_path)

            with self.assertRaises(qna.QnAError) as ctx:
                qna.load_qna_context(plan_dir, state_dir=state_dir)

            self.assertEqual(qna.REASON_MISSING_QUESTION_CONTEXT, ctx.exception.reason)

    def test_load_qna_context_raises_missing_question_context_when_artifact_undecodable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path = self._seed_plan(tmp_dir)
            last_message_path = tmp_dir / "binary.txt"
            last_message_path.write_bytes(b"Should we \xff\xfe use 1a?\n")
            self._write_state(state_path, last_message_path=str(last_message_path))

            with self.assertRaises(qna.QnAError) as ctx:
                qna.load_qna_context(plan_dir, state_dir=state_dir)

            self.assertEqual(qna.REASON_MISSING_QUESTION_CONTEXT, ctx.exception.reason)
            self.assertEqual(
                str(last_message_path), ctx.exception.details["last_message_path"]
            )

    def test_load_qna_context_raises_missing_question_context_when_path_unset(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path = self._seed_plan(tmp_dir)
            self._write_state(
                state_path,
                turn_overrides={"last_message_path": ""},
            )

            with self.assertRaises(qna.QnAError) as ctx:
                qna.load_qna_context(plan_dir, state_dir=state_dir)

            self.assertEqual(qna.REASON_MISSING_QUESTION_CONTEXT, ctx.exception.reason)

    def test_load_qna_context_raises_reply_context_mismatch_when_session_advanced(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            # Session 1 already complete; pending is now session 2.
            write_plan(plan_dir, ("complete", "pending"))
            plan_state = state.plan_state_dir(plan_dir, state_dir)
            plan_state.mkdir(parents=True)
            state_path = plan_state / events.STATE_FILE_NAME
            last_message_path = str(tmp_dir / "last-message.txt")
            self._write_last_message(last_message_path)
            self._write_state(
                state_path,
                session_id="1",
                last_message_path=last_message_path,
            )

            with self.assertRaises(qna.QnAError) as ctx:
                qna.load_qna_context(plan_dir, state_dir=state_dir)

            self.assertEqual(qna.REASON_REPLY_CONTEXT_MISMATCH, ctx.exception.reason)
            self.assertEqual("1", ctx.exception.details["paused_session_id"])
            self.assertEqual("2", ctx.exception.details["next_pending_id"])

    def test_load_qna_context_raises_no_paused_run_when_no_pending_session(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            write_plan(plan_dir, ("complete", "complete"))
            plan_state = state.plan_state_dir(plan_dir, state_dir)
            plan_state.mkdir(parents=True)
            state_path = plan_state / events.STATE_FILE_NAME
            last_message_path = str(tmp_dir / "last-message.txt")
            self._write_last_message(last_message_path)
            self._write_state(state_path, last_message_path=last_message_path)

            with self.assertRaises(qna.QnAError) as ctx:
                qna.load_qna_context(plan_dir, state_dir=state_dir)

            self.assertEqual(qna.REASON_NO_PAUSED_RUN, ctx.exception.reason)

    def test_qna_error_payload_exposes_stable_keys(self) -> None:
        error = qna.QnAError(
            qna.REASON_REPLY_CONTEXT_MISMATCH,
            "mismatch",
            details={"paused_session_id": "1", "next_pending_id": "2"},
        )

        payload = qna.qna_error_payload(error)

        self.assertEqual(
            {
                "reason": "reply-context-mismatch",
                "error": "mismatch",
                "details": {"paused_session_id": "1", "next_pending_id": "2"},
            },
            payload,
        )


def load_tests(
    loader: unittest.TestLoader, tests: unittest.TestSuite, pattern: str | None
) -> unittest.TestSuite:
    """Load only test cases defined in this focused section."""
    return load_section_tests(loader, __name__)
