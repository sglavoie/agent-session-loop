# ruff: noqa: F403, F405, I001
from .asl_qna_context_support import _QnAContextFixture
from .test_asl_qna_shared import *
from .support_base import load_section_tests


class QnAPayloadTests(_QnAContextFixture):
    def test_qna_context_payload_is_json_serializable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path = self._seed_plan(tmp_dir)
            last_message_path = str(tmp_dir / "last-message.txt")
            self._write_state(state_path, last_message_path=last_message_path)
            self._write_last_message(last_message_path)

            context = qna.load_qna_context(plan_dir, state_dir=state_dir)
            payload = qna.qna_context_payload(context)
            # Round-trip through json to confirm no Path leakage.
            encoded = json.dumps(payload, sort_keys=True)
            decoded = json.loads(encoded)
            self.assertEqual("1", decoded["session_id"])
            self.assertEqual("1", decoded["next_pending_id"])
            self.assertEqual(last_message_path, decoded["last_message_path"])
            self.assertEqual(
                str(plans_files.canonicalize_path(plan_dir)), decoded["plan_dir"]
            )

    def test_questions_payload_carries_resume_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path = self._seed_plan(tmp_dir)
            last_message_path = str(tmp_dir / "last-message.txt")
            self._write_state(
                state_path,
                last_message_path=last_message_path,
                payload_overrides={
                    "run_config": {"state_dir_override": str(state_dir)}
                },
            )
            self._write_last_message(last_message_path)

            context = qna.load_qna_context(plan_dir, state_dir=state_dir)
            payload = qna.questions_payload(context)

            plan = str(plans_files.canonicalize_path(plan_dir))
            self.assertNotIn("context_kind", payload)
            self.assertNotIn("waiting_for_reply", payload)
            self.assertNotIn("question_id", payload)
            self.assertEqual(
                f"asl run {plan} --resume --state-dir {state_dir} --reply TEXT",
                payload["resume_command"],
            )
            self.assertEqual(
                f"asl run {plan} --resume --state-dir {state_dir} --reply-stdin",
                payload["reply_stdin_command"],
            )

    def test_questions_payload_resume_omits_state_dir_without_override(
        self,
    ) -> None:
        # Legacy state (or a default state root) records no state_dir_override,
        # so the resume guidance must not fabricate a --state-dir argument.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path = self._seed_plan(tmp_dir)
            last_message_path = str(tmp_dir / "last-message.txt")
            self._write_state(state_path, last_message_path=last_message_path)
            self._write_last_message(last_message_path)

            context = qna.load_qna_context(plan_dir, state_dir=state_dir)
            payload = qna.questions_payload(context)

            plan = str(plans_files.canonicalize_path(plan_dir))
            self.assertEqual(
                f"asl run {plan} --resume --reply TEXT",
                payload["resume_command"],
            )
            self.assertNotIn("--state-dir", payload["reply_stdin_command"])


def load_tests(
    loader: unittest.TestLoader, tests: unittest.TestSuite, pattern: str | None
) -> unittest.TestSuite:
    """Load only test cases defined in this focused section."""
    return load_section_tests(loader, __name__)
