"""Prepare validation waves, workers, and readiness blockers."""

from __future__ import annotations

import io
import threading
import time
from contextlib import redirect_stderr
from unittest import mock

from agent_session_loop import (
    plans_errors,
    preflight,
    prepare,
    prepare_expand_ingest,
    prepare_models,
    prepare_packet,
    prepare_parsing,
    prepare_results,
    prepare_validation_cache,
    prepare_validation_stage,
)
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    _prepare_pipeline_payload,
    _recording_validator,
    init_multi_repo_workspace,
)
from tests.support_asl_prepare import PrepareEndToEndFixtures


class PrepareValidationWaveAndBlockerTests(PrepareEndToEndFixtures):
    def test_repository_change_during_validation_is_not_rekeyed_as_reusable(
        self,
    ) -> None:
        source = self._write_source()
        original_ingest = prepare_expand_ingest.ingest_expand_batch
        changed = False

        def mutate_repository_then_ingest(plan_dir, payload, **kwargs):
            nonlocal changed
            if not changed:
                changed = True
                (self.repo / "changed-during-validation.txt").write_text(
                    "changed after worker research\n",
                    encoding="utf-8",
                )
            return original_ingest(plan_dir, payload, **kwargs)

        with mock.patch.object(
            prepare_validation_stage,
            "ingest_expand_batch",
            side_effect=mutate_repository_then_ingest,
        ):
            first = self._prepare(source, _recording_validator([]))

        self.assertTrue(first.ok, first)
        assert first.plan_dir is not None
        calls: list[prepare_packet.ValidationTaskPacket] = []
        resumed = self._prepare(
            None,
            _recording_validator(calls),
            resume_plan_dir=first.plan_dir,
        )

        self.assertTrue(resumed.ok, resumed)
        self.assertEqual([("1", "2")], [packet.session_ids for packet in calls])

    def test_registered_scope_change_revalidates_only_that_session(self) -> None:
        specs = DEFAULT_MULTI_REPO_SPECS[:2]
        workspace = self.tmp_path / "workspace"
        init_multi_repo_workspace(workspace, specs)
        source = self._write_registered_source(specs)
        first = self._prepare(
            source,
            _recording_validator([]),
            cwd=workspace,
        )

        self.assertTrue(first.ok, first)
        assert first.plan_dir is not None
        first_alias = specs[0][0]
        aliases = ", ".join(spec[0] for spec in specs)
        session_path = first.plan_dir / "session-1.md"
        session_path.write_text(
            session_path.read_text(encoding="utf-8")
            .replace(
                f"**Reads repositories**: {aliases}",
                f"**Reads repositories**: {first_alias}",
            )
            .replace(
                f"**Writes repositories**: {aliases}",
                f"**Writes repositories**: {first_alias}",
            ),
            encoding="utf-8",
        )
        calls: list[prepare_packet.ValidationTaskPacket] = []
        resumed = self._prepare(
            None,
            _recording_validator(calls),
            cwd=workspace,
            resume_plan_dir=first.plan_dir,
        )

        self.assertTrue(resumed.ok, resumed)
        self.assertEqual([("1",)], [packet.session_ids for packet in calls])

    def test_partial_validation_wave_keeps_successes_for_resume(self) -> None:
        source = self._write_source(session_count=4)
        first_calls: list[tuple[str, ...]] = []
        first_started = threading.Event()
        second_failed = threading.Event()

        def partly_failing_validator(
            packet: prepare_packet.ValidationTaskPacket,
        ) -> object:
            first_calls.append(packet.session_ids)
            if packet.session_ids == ("1",):
                first_started.set()
                self.assertTrue(second_failed.wait(timeout=5))
            if packet.session_ids == ("2",):
                self.assertTrue(first_started.wait(timeout=5))
                second_failed.set()
                raise plans_errors.PlanError("validator service interrupted")
            return _prepare_pipeline_payload(
                packet.codebase_root,
                packet.session_ids,
                version=packet.version,
                repositories=packet.repositories,
                session_repositories=packet.session_read_repositories,
            )

        failed = self._prepare(
            source,
            partly_failing_validator,
            batch_size=1,
            validator_jobs=2,
        )

        self.assertFalse(failed.ok)
        self.assertEqual(
            prepare_results.PREPARE_EXIT_REASON_VALIDATION_FAILED, failed.reason
        )
        self.assertEqual([("1",), ("2",)], first_calls)
        assert failed.plan_dir is not None
        cache = prepare_validation_cache.load_prepare_validation_cache(failed.plan_dir)
        self.assertIsNotNone(cache.entry("1"))
        self.assertIsNone(cache.entry("2"))
        self.assertFalse(
            (failed.plan_dir / prepare_models.PREPARE_VALIDATION_FILENAME).exists()
        )

        resumed_calls: list[prepare_packet.ValidationTaskPacket] = []
        resumed = self._prepare(
            None,
            _recording_validator(resumed_calls),
            resume_plan_dir=failed.plan_dir,
            batch_size=1,
            validator_jobs=1,
        )

        self.assertTrue(resumed.ok, resumed)
        self.assertEqual(
            [("2",), ("3",), ("4",)],
            [packet.session_ids for packet in resumed_calls],
        )

    def test_validator_workers_are_bounded_and_ingested_in_batch_order(self) -> None:
        source = self._write_source(session_count=4)
        active = 0
        max_active = 0
        lock = threading.Lock()
        first_started = threading.Event()
        second_returned = threading.Event()
        worker_order: list[str] = []
        ingest_order: list[tuple[str, ...]] = []

        def out_of_order_validator(
            packet: prepare_packet.ValidationTaskPacket,
        ) -> object:
            nonlocal active, max_active
            session_id = packet.session_ids[0]
            with lock:
                active += 1
                max_active = max(max_active, active)
                worker_order.append(session_id)
            try:
                if session_id == "1":
                    first_started.set()
                    self.assertTrue(second_returned.wait(timeout=5))
                elif session_id == "2":
                    self.assertTrue(first_started.wait(timeout=5))
                    second_returned.set()
                return _prepare_pipeline_payload(
                    packet.codebase_root,
                    packet.session_ids,
                    version=packet.version,
                    repositories=packet.repositories,
                    session_repositories=packet.session_read_repositories,
                )
            finally:
                with lock:
                    active -= 1

        original_ingest = prepare_expand_ingest.ingest_expand_batch

        def recording_ingest(plan_dir, payload, **kwargs):
            ingest_order.append(
                tuple(prepare_parsing.parse_validator_output(payload).session_ids)
            )
            return original_ingest(plan_dir, payload, **kwargs)

        stderr = io.StringIO()
        with (
            mock.patch.object(
                prepare_validation_stage,
                "ingest_expand_batch",
                side_effect=recording_ingest,
            ),
            redirect_stderr(stderr),
        ):
            result = self._prepare(
                source,
                out_of_order_validator,
                batch_size=1,
                validator_jobs=2,
                validator_heartbeat=1,
            )

        self.assertTrue(result.ok, result)
        self.assertLessEqual(max_active, 2)
        self.assertEqual({"1", "2"}, set(worker_order[:2]))
        self.assertEqual(
            [("1",), ("2",), ("3",), ("4",)],
            ingest_order,
        )
        self.assertIn("validator batch 1/4 started", stderr.getvalue())

    def test_validator_heartbeats_stay_on_stderr(self) -> None:
        source = self._write_source()

        def slow_validator(packet: prepare_packet.ValidationTaskPacket) -> object:
            time.sleep(1.1)
            return _prepare_pipeline_payload(
                packet.codebase_root,
                packet.session_ids,
                version=packet.version,
                repositories=packet.repositories,
                session_repositories=packet.session_read_repositories,
            )

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            result = self._prepare(
                source,
                slow_validator,
                validator_heartbeat=1,
            )

        self.assertTrue(result.ok, result)
        self.assertIn("validator heartbeat", stderr.getvalue())
        self.assertIn("in-flight", stderr.getvalue())
        self.assertIn("elapsed", stderr.getvalue())

    def test_contract_problems_block_baseline_and_preflight_until_revalidated(
        self,
    ) -> None:
        source = self._write_source()

        def contract_problem_validator(
            packet: prepare_packet.ValidationTaskPacket,
        ) -> object:
            payload = _prepare_pipeline_payload(
                packet.codebase_root,
                packet.session_ids,
                version=packet.version,
                repositories=packet.repositories,
                session_repositories=packet.session_read_repositories,
            )
            payload["sessions"][0]["contract_problems"] = [
                {
                    "message": "baseline command has an invalid expected failure",
                    "suggested_fix": "repair the completion contract command",
                }
            ]
            return payload

        with (
            mock.patch.object(prepare, "_run_baseline_stage") as baseline_stage,
            mock.patch.object(preflight, "run_preflight") as run_preflight,
        ):
            failed = self._prepare(source, contract_problem_validator)

        self.assertFalse(failed.ok)
        self.assertEqual(
            prepare_results.PREPARE_EXIT_REASON_VALIDATION_FAILED, failed.reason
        )
        self.assertEqual(
            prepare_results.PREPARE_STAGE_VALIDATION, failed.blockers[0].stage
        )
        self.assertEqual("1", failed.blockers[0].session_id)
        self.assertEqual(
            prepare_models.CONTRACT_PROBLEM_CATEGORY, failed.blockers[0].category
        )
        baseline_stage.assert_not_called()
        run_preflight.assert_not_called()
        assert failed.plan_dir is not None

        session_path = failed.plan_dir / "session-1.md"
        session_path.write_text(
            session_path.read_text(encoding="utf-8").replace(
                "### Verify",
                "- Contract repair evidence is now explicit.\n\n### Verify",
                1,
            ),
            encoding="utf-8",
        )
        calls: list[prepare_packet.ValidationTaskPacket] = []
        resumed = self._prepare(
            None,
            _recording_validator(calls),
            resume_plan_dir=failed.plan_dir,
        )

        self.assertTrue(resumed.ok, resumed)
        self.assertEqual([("1",)], [packet.session_ids for packet in calls])

    def test_unsatisfied_baseline_blocks_and_never_claims_readiness(self) -> None:
        check = {
            "name": "expected-pass",
            "type": "command",
            "argv": ["python3", "-c", "raise SystemExit(1)"],
            "timeout_seconds": 30,
            "baseline_expectation": "pass",
        }
        source = self._write_source(check_json_by_id={"1": check})

        result = self._prepare(source, _recording_validator([]))

        self.assertFalse(result.ok)
        self.assertEqual("baseline-unsatisfied", result.reason)
        self.assertFalse(result.marked_loop_ready)
        self.assertEqual("baseline", result.blockers[0].stage)
        self.assertIn("expected-pass", result.blockers[0].message)
        assert result.plan_dir is not None
        manifest = (result.plan_dir / "manifest.md").read_text(encoding="utf-8")
        self.assertIn("**Readiness**: draft", manifest)
        assert result.resume_command is not None
        self.assertIn("--resume", result.resume_command)

    def test_preflight_blockers_prevent_loop_ready(self) -> None:
        check = {
            "name": "missing-tool",
            "type": "command",
            "argv": ["asl-prepare-missing-executable-xyz"],
            "timeout_seconds": 30,
        }
        source = self._write_source(check_json_by_id={"1": check})

        result = self._prepare(source, _recording_validator([]))

        self.assertFalse(result.ok)
        self.assertEqual("not-loop-ready", result.reason)
        self.assertFalse(result.marked_loop_ready)
        categories = {blocker.category for blocker in result.blockers}
        self.assertIn("command-not-runnable", categories)
        assert result.plan_dir is not None
        manifest = (result.plan_dir / "manifest.md").read_text(encoding="utf-8")
        self.assertIn("**Readiness**: draft", manifest)
