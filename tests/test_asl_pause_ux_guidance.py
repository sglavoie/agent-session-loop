"""Test asl pause ux guidance."""

from __future__ import annotations

import json
import shlex
import tempfile
import unittest
from pathlib import Path

from agent_session_loop import (
    events,
    pause_policy,
    qna,
    state,
    state_payloads,
    task_packet,
)
from tests import support_asl_pause_ux as fixtures
from tests.support import (
    write_plan,
)


class ArtifactSafetyAndPauseUxTests(fixtures.ArtifactSafetyAndPauseUxCase):
    def test_questions_payload_carries_pause_policy_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir, state_dir, _ = self._seed_paused_plan(Path(tmp))
            context = qna.load_qna_context(str(plan_dir), state_dir=str(state_dir))
            payload = qna.questions_payload(context)
            self.assertIn("pause_policy", payload)
            self.assertEqual(
                pause_policy.classify_pause(payload["pause_reason"]),
                payload["pause_policy"],
            )

    def test_runner_state_payload_classifies_paused_reason(self) -> None:
        # An older paused state file with no persisted pause_policy block still
        # surfaces one, classified from its reason (fallback path).
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir, state_dir, _ = self._seed_paused_plan(
                Path(tmp), statuses=("pending",)
            )
            plan_state = state.plan_state_dir(plan_dir, state_dir)
            state_path = plan_state / events.STATE_FILE_NAME
            runner_state = state_payloads._runner_state_payload(
                state_path,
                lock_info=None,
            )
            self.assertEqual(
                pause_policy.classify_pause("runner-pause"),
                runner_state["pause_policy"],
            )

    def test_runner_state_payload_prefers_persisted_pause_policy(self) -> None:
        # When the state file already carries a pause_policy block (written by
        # the runner), status echoes it verbatim rather than reclassifying.
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir, state_dir, _ = self._seed_paused_plan(
                Path(tmp), statuses=("pending",)
            )
            plan_state = state.plan_state_dir(plan_dir, state_dir)
            state_path = plan_state / events.STATE_FILE_NAME
            raw = json.loads(state_path.read_text(encoding="utf-8"))
            persisted = {
                "reason": "runner-pause",
                "recoverable": False,
                "requires_user": True,
                "max_retries": 7,
                "backoff_seconds": [1.0],
            }
            raw["pause_policy"] = persisted
            state_path.write_text(json.dumps(raw), encoding="utf-8")
            runner_state = state_payloads._runner_state_payload(
                state_path,
                lock_info=None,
            )
            self.assertEqual(persisted, runner_state["pause_policy"])

    def test_runner_state_payload_omits_pause_policy_when_not_paused(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_state = tmp_dir / "state"
            plan_state.mkdir(parents=True)
            state_path = plan_state / events.STATE_FILE_NAME
            state_path.write_text(
                json.dumps({"ok": True, "paused": False, "reason": None}),
                encoding="utf-8",
            )
            runner_state = state_payloads._runner_state_payload(
                state_path,
                lock_info=None,
            )
            self.assertIsNone(runner_state["pause_policy"])

    def test_questions_command_text_includes_state_dir_when_provided(self) -> None:
        self.assertEqual(
            "asl questions /tmp/plan",
            task_packet._questions_command_text("/tmp/plan", None),
        )
        self.assertEqual(
            "asl questions /tmp/plan --state-dir /tmp/state",
            task_packet._questions_command_text("/tmp/plan", "/tmp/state"),
        )

    def test_reply_command_text_includes_state_dir_when_provided(self) -> None:
        self.assertEqual(
            "asl run /tmp/plan --resume --reply TEXT",
            task_packet._reply_command_text("/tmp/plan", None),
        )
        self.assertEqual(
            "asl run /tmp/plan --resume --state-dir /tmp/state --reply TEXT",
            task_packet._reply_command_text("/tmp/plan", "/tmp/state"),
        )

    def test_interactive_help_commands_shell_quote_dynamic_paths(self) -> None:
        questions = task_packet._questions_command_text(
            "/tmp/plan dir",
            "/tmp/state dir",
        )
        reply = task_packet._reply_command_text(
            "/tmp/plan dir",
            "/tmp/state dir",
        )

        self.assertEqual(
            "asl questions '/tmp/plan dir' --state-dir '/tmp/state dir'",
            questions,
        )
        self.assertEqual(
            "asl run '/tmp/plan dir' --resume "
            "--state-dir '/tmp/state dir' --reply TEXT",
            reply,
        )
        self.assertEqual(
            [
                "asl",
                "questions",
                "/tmp/plan dir",
                "--state-dir",
                "/tmp/state dir",
            ],
            shlex.split(questions),
        )
        self.assertEqual(
            [
                "asl",
                "run",
                "/tmp/plan dir",
                "--resume",
                "--state-dir",
                "/tmp/state dir",
                "--reply",
                "TEXT",
            ],
            shlex.split(reply),
        )

    def test_attach_interactive_help_populates_payload_when_qna_loads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir, state_dir, last_message_path = self._seed_paused_plan(Path(tmp))
            args = self._paused_args(plan_dir, state_dir)
            payload: dict[str, object] = {
                "paused": True,
                "plan_dir": str(plan_dir),
            }

            task_packet._attach_interactive_help(args, payload)

            help_payload = payload["interactive_help"]
            self.assertIsInstance(help_payload, dict)
            self.assertEqual(
                task_packet._questions_command_text(str(plan_dir), str(state_dir)),
                help_payload["questions_command"],
            )
            self.assertEqual(
                task_packet._reply_command_text(str(plan_dir), str(state_dir)),
                help_payload["reply_command"],
            )
            self.assertEqual("1", help_payload["session_id"])
            self.assertEqual("1", help_payload["next_pending_id"])
            self.assertEqual(2, help_payload["turn_index"])
            self.assertEqual(
                str(last_message_path),
                help_payload["artifact_paths"]["last_message_path"],
            )
            self.assertIn(
                "# Paused Agent Session - Resolver Prompt",
                help_payload["rendered_prompt"],
            )

    def test_attach_interactive_help_omits_when_no_paused_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            write_plan(plan_dir, ("pending",))
            plan_state = state.plan_state_dir(plan_dir, state_dir)
            plan_state.mkdir(parents=True)
            args = self._paused_args(plan_dir, state_dir)
            payload: dict[str, object] = {
                "paused": True,
                "plan_dir": str(plan_dir),
            }

            task_packet._attach_interactive_help(args, payload)

            self.assertNotIn("interactive_help", payload)

    def test_attach_interactive_help_skips_non_paused_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir, state_dir, _ = self._seed_paused_plan(Path(tmp))
            args = self._paused_args(plan_dir, state_dir)
            payload: dict[str, object] = {
                "paused": False,
                "plan_dir": str(plan_dir),
            }

            task_packet._attach_interactive_help(args, payload)

            self.assertNotIn("interactive_help", payload)

    def test_a_lost_conversation_prompt_never_asks_for_an_answer(self) -> None:
        """The one pause with no question in it must not be read as one.

        Every other paused context ends with "ask the user the question the
        previous agent needed answered". Here there is no question, so that
        instruction produces an invented one and a reply that no session can
        consume.
        """
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir, state_dir = self._seed_lost_conversation(Path(tmp))
            context = qna.load_qna_context(plan_dir, state_dir=state_dir)

            prompt = qna.render_resolver_prompt(context)

            self.assertTrue(qna.requires_conversation_recovery(context))
            self.assertIn("# Unresumable Conversation - Recovery Prompt", prompt)
            self.assertNotIn("Ask the user the question", prompt)
            self.assertNotIn("--reply TEXT", prompt)

    def test_a_lost_conversation_prompt_names_the_one_use_relaunch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir, state_dir = self._seed_lost_conversation(Path(tmp))
            context = qna.load_qna_context(plan_dir, state_dir=state_dir)

            prompt = qna.render_resolver_prompt(context)

            self.assertIn(qna.recovery_command_text(context), prompt)
            self.assertIn("--fresh-conversation", prompt)
            self.assertIn("one-use", prompt)
            self.assertIn("asl control recover-conversation", prompt)

    def test_the_recovery_command_resumes_rather_than_starting_over(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir, state_dir = self._seed_lost_conversation(Path(tmp))
            context = qna.load_qna_context(plan_dir, state_dir=state_dir)

            parts = shlex.split(qna.recovery_command_text(context))

            # ``--resume`` is what rehydrates the harness, model, and effort the
            # run was using; a fresh conversation must not also mean a fresh
            # harness selection.
            self.assertEqual(
                ["asl", "run", str(context.plan_dir), "--resume"], parts[:4]
            )
            self.assertEqual("--fresh-conversation", parts[-1])
            # Same resume base as the reply route, differing only in the final
            # flag: the two commands must not drift into resuming differently.
            self.assertEqual(
                shlex.split(qna.resume_command_text(context))[:-2], parts[:-1]
            )

    def test_questions_payload_swaps_reply_guidance_for_the_recovery_route(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir, state_dir = self._seed_lost_conversation(Path(tmp))
            context = qna.load_qna_context(plan_dir, state_dir=state_dir)

            payload = qna.questions_payload(context)

            self.assertTrue(payload["recovery_required"])
            self.assertEqual(
                qna.recovery_command_text(context), payload["recovery_command"]
            )
            # A consumer that prints ``resume_command`` without reading
            # ``recovery_required`` prints nothing rather than a `--reply` for a
            # question that does not exist.
            self.assertIsNone(payload["resume_command"])
            self.assertIsNone(payload["reply_stdin_command"])

    def test_an_ordinary_pause_keeps_its_reply_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir, state_dir, _ = self._seed_paused_plan(Path(tmp))
            context = qna.load_qna_context(plan_dir, state_dir=state_dir)

            payload = qna.questions_payload(context)

            self.assertFalse(payload["recovery_required"])
            self.assertIsNone(payload["recovery_command"])
            self.assertIn("--reply", payload["resume_command"])

    def test_interactive_help_carries_recovery_instead_of_a_reply_command(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir, state_dir = self._seed_lost_conversation(Path(tmp))
            args = self._paused_args(plan_dir, state_dir)
            payload: dict[str, object] = {
                "paused": True,
                "plan_dir": str(plan_dir),
            }

            task_packet._attach_interactive_help(args, payload)

            help_payload = payload["interactive_help"]
            self.assertTrue(help_payload["recovery_required"])
            self.assertIn("--fresh-conversation", help_payload["recovery_command"])
            self.assertIsNone(help_payload["reply_command"])
            self.assertIn(
                "# Unresumable Conversation - Recovery Prompt",
                help_payload["rendered_prompt"],
            )


if __name__ == "__main__":
    unittest.main()
