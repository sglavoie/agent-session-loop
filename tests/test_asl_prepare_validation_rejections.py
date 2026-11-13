"""Legacy repository-ambiguity reports and retained rejected validator output."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr
from unittest import mock

from agent_session_loop import (
    plans_errors,
    prepare_models,
    prepare_packet,
    prepare_results,
    prepare_validation_rejections,
    prepare_validator,
)
from tests.support import _prepare_pipeline_payload, _recording_validator
from tests.support_asl_prepare import PrepareEndToEndFixtures


def _good_payload(packet: prepare_packet.ValidationTaskPacket) -> dict[str, object]:
    return _prepare_pipeline_payload(
        packet.codebase_root,
        packet.session_ids,
        version=packet.version,
        repositories=packet.repositories,
        session_repositories=packet.session_read_repositories,
    )


class LegacyRepositoryAmbiguityTests(PrepareEndToEndFixtures):
    def test_legacy_packet_asks_only_about_other_git_repositories(self) -> None:
        calls: list[prepare_packet.ValidationTaskPacket] = []
        result = self._prepare(self._write_source(), _recording_validator(calls))

        self.assertTrue(result.ok, result)
        rendered = calls[0].render()
        self.assertEqual(
            prepare_models.LEGACY_PREPARE_VALIDATION_VERSION, calls[0].version
        )
        self.assertIn(
            "only when it resolves inside a Git repository other than that root",
            rendered,
        )
        self.assertIn("Paths on another host", rendered)
        self.assertNotIn("Report any path that resolves outside that root", rendered)

    def test_single_repository_plan_prepares_despite_a_runtime_path_ambiguity_report(
        self,
    ) -> None:
        # The 2026-11-13 failure: validators flagged runtime paths on another
        # host as repository ambiguity, and prepare refused a one-repo plan.
        summary = "`/var/lib/pi-monitoring/controller.json` is outside the root"

        def runtime_path_validator(
            packet: prepare_packet.ValidationTaskPacket,
        ) -> object:
            payload = _good_payload(packet)
            payload["sessions"][0]["issues"] = [  # type: ignore[index]
                {
                    "issue_type": prepare_models.REPOSITORY_AMBIGUITY_ISSUE,
                    "summary": summary,
                    "session_ref": "`/var/lib/pi-monitoring/controller.json`",
                    "actual": "the file lives on another host",
                    "fix": "declare a `## Repositories` registry",
                }
            ]
            return payload

        result = self._prepare(self._write_source(), runtime_path_validator)

        self.assertTrue(result.ok, result)
        self.assertTrue(result.marked_loop_ready)
        assert result.plan_dir is not None
        notes = "".join(
            path.read_text(encoding="utf-8")
            for path in sorted(result.plan_dir.glob("*.md"))
        )
        self.assertIn(summary, notes)


class RejectedValidatorOutputTests(PrepareEndToEndFixtures):
    def test_rejected_batch_output_is_saved_and_named_in_the_blocker(self) -> None:
        def wrong_root_validator(packet: prepare_packet.ValidationTaskPacket) -> object:
            payload = _good_payload(packet)
            payload["codebase_root"] = "/somewhere/else"
            return payload

        with redirect_stderr(io.StringIO()) as stderr:
            failed = self._prepare(self._write_source(), wrong_root_validator)

        self.assertFalse(failed.ok)
        self.assertEqual(
            prepare_results.PREPARE_EXIT_REASON_VALIDATION_FAILED, failed.reason
        )
        assert failed.plan_dir is not None
        path = prepare_validation_rejections.prepare_validation_rejected_path(
            failed.plan_dir
        )
        document = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(1, document["version"])
        [rejection] = document["rejections"]
        self.assertEqual("/somewhere/else", rejection["output"]["codebase_root"])
        self.assertIn("does not match", rejection["error"])
        self.assertIn(str(path), failed.blockers[0].message)
        self.assertIn(f"(rejected output: {path})", stderr.getvalue())

        resumed = self._prepare(
            None, _recording_validator([]), resume_plan_dir=failed.plan_dir
        )

        self.assertTrue(resumed.ok, resumed)
        self.assertFalse(path.exists())

    def test_a_worker_failure_without_output_records_nothing(self) -> None:
        def broken_validator(packet: prepare_packet.ValidationTaskPacket) -> object:
            raise plans_errors.PlanError("validator service interrupted")

        with redirect_stderr(io.StringIO()):
            failed = self._prepare(self._write_source(), broken_validator)

        self.assertFalse(failed.ok)
        assert failed.plan_dir is not None
        self.assertFalse(
            prepare_validation_rejections.prepare_validation_rejected_path(
                failed.plan_dir
            ).exists()
        )
        self.assertEqual("validator service interrupted", failed.blockers[0].message)

    def test_launcher_carries_the_last_unparseable_answer(self) -> None:
        calls: list[prepare_packet.ValidationTaskPacket] = []
        self._prepare(self._write_source(), _recording_validator(calls))
        packet = calls[0]

        with (
            mock.patch.object(
                prepare_validator, "_run_validator_turn", return_value="not json"
            ) as turn,
            redirect_stderr(io.StringIO()),
            self.assertRaises(
                prepare_validation_rejections.ValidatorOutputRejected
            ) as caught,
        ):
            prepare_validator._launch_validator("codex", packet)

        self.assertEqual(
            prepare_validator.VALIDATOR_REPAIR_ATTEMPTS + 1, turn.call_count
        )
        self.assertEqual("not json", caught.exception.output)
        self.assertIsInstance(caught.exception, plans_errors.PlanError)
