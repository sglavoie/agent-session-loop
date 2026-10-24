"""Asl turn preparation prompt regressions."""

from __future__ import annotations

import unittest
from typing import Any
from unittest import mock

from agent_session_loop import (
    events,
    mailbox_vocabulary,
    run_prompts,
    task_packet,
    turn_preparation,
)

from .support_asl_turn_preparation import PreparationTestCase

ASL_TEST_PORTABLE = True


class TurnPromptTests(PreparationTestCase):
    """One of three prompt shapes, then the gate that judges what it reaches."""

    def prepare(self, **overrides: Any) -> turn_preparation.TurnPrompt:
        snapshot = overrides.pop("snapshot", None) or self.snapshot()
        inputs = overrides.pop("inputs", None) or turn_preparation.PacketInputs(
            worker_mode=task_packet.WORKER_MODE_PACKET,
            predecessor_id=None,
            has_expansion=False,
            has_progress=False,
            repair_references=(),
            pending_guidance=(),
        )
        kwargs: dict[str, Any] = {
            "classification": self.classification(),
            "inputs": inputs,
            "args": self.args(),
            "cwd": self.tmp_dir,
            "harness": "codex",
            "session_id": "1",
            "session_label": "Session 1",
            "session_details": {"session_id": "1"},
            "session_scope": None,
            "workspace": None,
            "mailbox_enabled": False,
            "reply": None,
            "resolution": None,
        }
        kwargs.update(overrides)
        return turn_preparation.prepare_turn_prompt(
            self.ctx, snapshot=snapshot, **kwargs
        )

    def reply(self) -> run_prompts._PendingReply:
        return run_prompts._PendingReply(
            text="do the thing",
            context=self.qna_context(),
            origin=run_prompts.REPLY_ORIGIN_ARGV,
            process_start=False,
            question_id="q1",
        )

    def resolution(self) -> run_prompts._PendingResolution:
        return run_prompts._PendingResolution(
            action=mailbox_vocabulary.RESOLUTION_ACTIONS[0],
            note="a note",
            context=self.qna_context(),
            question_id="q1",
            resolution_message_id="m1",
            resolution_seq=4,
        )

    def test_a_fresh_start_persists_the_prompt_it_sends(self) -> None:
        result = self.prepare()

        self.assertEqual("session", result.prompt_kind)
        self.assertEqual(result.prompt, result.persisted_prompt)
        self.assertIsNone(result.reply_source)
        self.assertIsNone(result.resolution_source)
        self.assertFalse(result.paused)

    def test_a_reply_persists_a_placeholder_rather_than_the_words(self) -> None:
        reply = self.reply()

        result = self.prepare(reply=reply)

        self.assertEqual("reply", result.prompt_kind)
        self.assertNotIn(reply.text, result.persisted_prompt)
        self.assertIn(reply.text, result.prompt)
        assert result.reply_source is not None
        self.assertEqual(len(reply.text), result.reply_source["reply_length"])
        self.assertEqual("q1", result.reply_source["question_id"])
        self.assertNotIn("reply_message_id", result.reply_source)
        self.assertIsNone(result.resolution_source)

    def test_a_resolution_persists_a_placeholder_and_a_body_free_source(self) -> None:
        resolution = self.resolution()

        result = self.prepare(resolution=resolution)

        self.assertEqual("resolution", result.prompt_kind)
        assert resolution.note is not None
        self.assertNotIn(resolution.note, result.persisted_prompt)
        assert result.resolution_source is not None
        self.assertEqual(len(resolution.note), result.resolution_source["note_length"])
        self.assertNotIn("note", result.resolution_source)
        self.assertIsNone(result.reply_source)

    def test_a_reply_and_a_resolution_together_are_a_programming_error(self) -> None:
        with self.assertRaises(RuntimeError) as caught:
            self.prepare(reply=self.reply(), resolution=self.resolution())

        self.assertIn("mutually exclusive", str(caught.exception))

    def test_skill_mode_renders_through_the_legacy_builder(self) -> None:
        inputs = turn_preparation.PacketInputs(
            worker_mode=task_packet.WORKER_MODE_SKILL,
            predecessor_id=None,
            has_expansion=False,
            has_progress=False,
            repair_references=(),
            pending_guidance=(),
        )
        with (
            mock.patch.object(
                turn_preparation.run_prompts,
                "build_session_prompt",
                return_value="skill prompt",
            ) as legacy,
            mock.patch.object(turn_preparation.task_packet, "build_task_packet") as pkt,
        ):
            result = self.prepare(inputs=inputs)

        self.assertEqual("skill prompt", result.prompt)
        legacy.assert_called_once()
        pkt.assert_not_called()

    def test_the_intent_gate_reads_the_rendered_prompt(self) -> None:
        with mock.patch.object(
            turn_preparation.run_inputs,
            "_task_packet_repository_intent_report",
            return_value={"registry_required": True},
        ) as report:
            result = self.prepare()

        self.assertTrue(result.paused)
        self.assertEqual(
            result.prompt,
            report.call_args.args[0],
            "the gate must judge the text the worker receives, not the plan "
            "the text was built from",
        )
        (pause,) = self.ctx.pauses
        self.assertEqual(
            events.PAUSE_REASON_REPOSITORY_REGISTRY_REQUIRED, pause["reason"]
        )
        self.assertIn("Session 1", pause["message"])
        self.assertEqual(
            {"registry_required": True}, pause["details"]["repository_intent"]
        )

    def test_a_registered_workspace_skips_the_gate_entirely(self) -> None:
        with (
            mock.patch.object(
                turn_preparation.run_inputs,
                "_task_packet_repository_intent_report",
            ) as report,
            mock.patch.object(turn_preparation.task_packet, "build_task_packet"),
        ):
            result = self.prepare(workspace=mock.Mock(), session_scope=mock.Mock())

        self.assertFalse(result.paused)
        report.assert_not_called()

    def test_state_root_gate_applies_to_every_prompt_shape_and_workspace_mode(
        self,
    ) -> None:
        for mode in (task_packet.WORKER_MODE_PACKET, task_packet.WORKER_MODE_SKILL):
            for shape in (
                {},
                {"reply": self.reply()},
                {"resolution": self.resolution()},
            ):
                for registered in (False, True):
                    with self.subTest(mode=mode, shape=shape, registered=registered):
                        self.ctx.pauses.clear()
                        inputs = turn_preparation.PacketInputs(
                            worker_mode=mode,
                            predecessor_id=None,
                            has_expansion=False,
                            has_progress=False,
                            repair_references=(),
                            pending_guidance=(),
                        )
                        report = {"required_state_roots": ["/external/store"]}
                        with (
                            mock.patch.object(
                                turn_preparation.run_inputs,
                                "_session_plan_store_state_root_report",
                                return_value=report,
                            ) as gate,
                            mock.patch.object(
                                turn_preparation.task_packet, "build_task_packet"
                            ),
                            mock.patch.object(
                                turn_preparation.run_prompts, "build_session_prompt"
                            ),
                            mock.patch.object(
                                turn_preparation.run_prompts, "build_reply_prompt"
                            ),
                            mock.patch.object(
                                turn_preparation.run_prompts, "build_resolution_prompt"
                            ),
                            mock.patch.object(
                                turn_preparation.run_inputs,
                                "_task_packet_repository_intent_report",
                                return_value=None,
                            ),
                        ):
                            result = self.prepare(
                                inputs=inputs,
                                workspace=mock.Mock() if registered else None,
                                session_scope=mock.Mock() if registered else None,
                                **shape,
                            )
                        self.assertTrue(result.paused)
                        gate.assert_called_once()
                        (pause,) = self.ctx.pauses
                        self.assertEqual(
                            events.PAUSE_REASON_WORKSPACE_WRITE_GUARD_UNAVAILABLE,
                            pause["reason"],
                        )
                        self.assertEqual(
                            report, pause["details"]["plan_store_state_root"]
                        )
                        self.assertIn("no worker was launched", pause["message"])


if __name__ == "__main__":
    unittest.main()
