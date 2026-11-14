"""The guarded contract-repair loop inside ``asl prepare``."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr
from pathlib import Path

from agent_session_loop import (
    cli,
    prepare_contract_repair,
    prepare_models,
    prepare_packet,
    prepare_results,
)
from tests.support import _prepare_pipeline_payload
from tests.support_asl_prepare import PrepareEndToEndFixtures

MARKER = "- The retry interval is pinned by a named constant."
PROBLEM = {
    "message": "the retry interval is not specified",
    "suggested_fix": "name the interval and test it",
}


def _problem_validator(calls: list[tuple[str, ...]]):
    """Report PROBLEM on session 1 until its file carries MARKER."""

    def validator(packet: prepare_packet.ValidationTaskPacket) -> object:
        calls.append(packet.session_ids)
        payload = _prepare_pipeline_payload(
            packet.codebase_root,
            packet.session_ids,
            version=packet.version,
            repositories=packet.repositories,
            session_repositories=packet.session_read_repositories,
        )
        text = (Path(packet.plan_dir) / "session-1.md").read_text(encoding="utf-8")
        if "1" in packet.session_ids and MARKER not in text:
            index = list(packet.session_ids).index("1")
            payload["sessions"][index]["contract_problems"] = [dict(PROBLEM)]
        return payload

    return validator


def _add_marker(packet: prepare_contract_repair.ContractRepairPacket) -> None:
    path = packet.plan_dir / "session-1.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "### Verify", f"{MARKER}\n\n### Verify", 1
        ),
        encoding="utf-8",
    )


class ContractRepairLoopTests(PrepareEndToEndFixtures):
    def _run(self, repairer, *, repair_rounds: int = 2, calls=None):
        calls = [] if calls is None else calls
        source = self._write_source()
        source_text = source.read_text(encoding="utf-8")
        with redirect_stderr(io.StringIO()):
            result = self._prepare(
                source,
                _problem_validator(calls),
                contract_repairer=repairer,
                repair_rounds=repair_rounds,
                force_new=True,
            )
        self.assertEqual(source_text, source.read_text(encoding="utf-8"))
        return result

    def _repair_steps(self, result: prepare_results.PrepareResult):
        return [
            step
            for step in result.steps
            if step.name == prepare_results.PREPARE_STAGE_REPAIR
        ]

    def test_a_repaired_contract_is_revalidated_and_the_plan_becomes_loop_ready(
        self,
    ) -> None:
        packets: list[prepare_contract_repair.ContractRepairPacket] = []
        calls: list[tuple[str, ...]] = []

        def repairer(packet):
            packets.append(packet)
            _add_marker(packet)
            return {"repaired": ["1"], "needs_decision": []}

        result = self._run(repairer, calls=calls)

        self.assertTrue(result.ok, result)
        self.assertTrue(result.marked_loop_ready)
        self.assertEqual(1, len(packets))
        self.assertIn(PROBLEM["message"], packets[0].render())
        self.assertIn("session-1.md", packets[0].allowed_files)
        [step] = self._repair_steps(result)
        self.assertEqual(prepare_results.PREPARE_STEP_OK, step.status)
        self.assertIn("session-1.md", step.detail)
        self.assertIn("source plan is unchanged", step.detail)
        # Only the repaired session is validated again.
        self.assertEqual([("1", "2"), ("1",)], calls)
        assert result.plan_dir is not None
        record = json.loads(
            (
                result.plan_dir
                / prepare_contract_repair.PREPARE_CONTRACT_REPAIRS_FILENAME
            ).read_text(encoding="utf-8")
        )
        [round_record] = record["rounds"]
        self.assertEqual(1, round_record["round"])
        self.assertEqual(PROBLEM["message"], round_record["problems"][0]["message"])
        [changed] = round_record["changed_files"]
        self.assertEqual("session-1.md", changed["path"])
        self.assertNotEqual(changed["before_sha256"], changed["after_sha256"])

    def test_zero_rounds_keeps_the_stop_and_never_calls_the_repairer(self) -> None:
        def repairer(packet):
            raise AssertionError("repair must not run")

        result = self._run(repairer, repair_rounds=0)

        self.assertFalse(result.ok)
        self.assertEqual(
            prepare_models.CONTRACT_PROBLEM_CATEGORY, result.blockers[0].category
        )
        self.assertEqual([], self._repair_steps(result))

    def test_an_edit_outside_the_allowed_files_is_restored_and_refused(self) -> None:
        def repairer(packet):
            _add_marker(packet)
            manifest = packet.plan_dir / "manifest.md"
            manifest.write_text("tampered\n", encoding="utf-8")
            (packet.plan_dir / "stray.md").write_text("new\n", encoding="utf-8")
            return {"repaired": ["1"]}

        result = self._run(repairer)

        self.assertFalse(result.ok)
        [step] = self._repair_steps(result)
        self.assertEqual(prepare_results.PREPARE_STEP_FAILED, step.status)
        self.assertIn("manifest.md", step.detail)
        assert result.plan_dir is not None
        self.assertNotIn(
            "tampered", (result.plan_dir / "manifest.md").read_text(encoding="utf-8")
        )
        self.assertFalse((result.plan_dir / "stray.md").exists())
        self.assertNotIn(
            MARKER, (result.plan_dir / "session-1.md").read_text(encoding="utf-8")
        )
        self.assertEqual(
            prepare_models.CONTRACT_PROBLEM_CATEGORY, result.blockers[0].category
        )

    def test_a_codebase_change_is_refused(self) -> None:
        def repairer(packet):
            _add_marker(packet)
            (packet.codebase_root / "touched.txt").write_text("x\n", encoding="utf-8")
            return {"repaired": ["1"]}

        result = self._run(repairer)

        self.assertFalse(result.ok)
        [step] = self._repair_steps(result)
        self.assertIn("changed the codebase", step.detail)
        assert result.plan_dir is not None
        self.assertNotIn(
            MARKER, (result.plan_dir / "session-1.md").read_text(encoding="utf-8")
        )

    def test_a_changed_handoff_or_broken_contract_is_refused(self) -> None:
        for mutation, expected in (
            (
                lambda text: text.replace("**Status**: pending", "**Status**: done"),
                "Handoff",
            ),
            (lambda text: text.replace("```json", "```text", 1), "completion contract"),
        ):
            with self.subTest(expected=expected):

                def repairer(packet, mutation=mutation):
                    path = packet.plan_dir / "session-1.md"
                    path.write_text(
                        mutation(path.read_text(encoding="utf-8")), encoding="utf-8"
                    )
                    return {"repaired": ["1"]}

                result = self._run(repairer)

                self.assertFalse(result.ok)
                [step] = self._repair_steps(result)
                self.assertIn(expected, step.detail)

    def test_a_question_for_the_owner_stops_with_that_question(self) -> None:
        def repairer(packet):
            return {
                "repaired": [],
                "needs_decision": [
                    {"session_id": "1", "question": "Should retries back off?"}
                ],
            }

        result = self._run(repairer)

        self.assertFalse(result.ok)
        [blocker] = result.blockers
        self.assertEqual(
            prepare_models.CONTRACT_REPAIR_DECISION_CATEGORY, blocker.category
        )
        self.assertEqual("Should retries back off?", blocker.message)
        self.assertEqual("1", blocker.session_id)

    def test_a_repair_that_leaves_the_same_problems_stops_after_one_round(
        self,
    ) -> None:
        packets: list[prepare_contract_repair.ContractRepairPacket] = []

        def repairer(packet):
            packets.append(packet)
            path = packet.plan_dir / "session-1.md"
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    "### Verify", f"- Round {len(packets)} note.\n\n### Verify", 1
                ),
                encoding="utf-8",
            )
            return {"repaired": ["1"]}

        result = self._run(repairer, repair_rounds=3)

        self.assertFalse(result.ok)
        self.assertEqual(1, len(packets))
        steps = self._repair_steps(result)
        self.assertEqual(
            [prepare_results.PREPARE_STEP_OK, prepare_results.PREPARE_STEP_FAILED],
            [step.status for step in steps],
        )
        self.assertIn("same contract problems", steps[-1].detail)

    def test_a_repair_that_changes_nothing_stops(self) -> None:
        result = self._run(lambda packet: {"repaired": []})

        self.assertFalse(result.ok)
        [step] = self._repair_steps(result)
        self.assertIn("changed no file", step.detail)


class RepairRoundsCliTests(PrepareEndToEndFixtures):
    def test_the_flag_defaults_to_two_and_the_resume_command_keeps_other_values(
        self,
    ) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(["prepare", "plan.md"])
        self.assertEqual(
            prepare_contract_repair.DEFAULT_CONTRACT_REPAIR_ROUNDS, args.repair_rounds
        )
        source = self._write_source()
        with redirect_stderr(io.StringIO()):
            result = self._prepare(
                source,
                _problem_validator([]),
                contract_repairer=lambda packet: {"repaired": []},
                repair_rounds=1,
            )
        assert result.resume_command is not None
        self.assertIn("--repair-rounds 1", result.resume_command)
        with redirect_stderr(io.StringIO()):
            default = self._prepare(
                None, _problem_validator([]), resume_plan_dir=result.plan_dir
            )
        assert default.resume_command is not None
        self.assertNotIn("--repair-rounds", default.resume_command)
