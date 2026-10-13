"""Selective Spark routing, cooldown, and compatibility tests."""

from __future__ import annotations

import argparse
import io
import json
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    cli,
    config,
    model_availability,
    plans,
    prepare_commands,
    prepare_expansion,
    prepare_models,
    prepare_parsing,
    prepare_validation_store,
    retry_hints,
    review_guard,
    run_entry,
    run_flags,
    run_inputs,
    run_selection,
    runner,
    spark_status,
    state,
)
from tests.support import (
    _validator_payload,
    _write_prepare_plan,
    init_git_repo,
    install_fake_codex,
    patched_env,
    write_plan,
)


class SparkFlagTests(unittest.TestCase):
    def test_run_flags_are_opt_in_and_no_spark_overrides(self) -> None:
        parser = cli.build_parser()
        self.assertFalse(parser.parse_args(["run", "/tmp/plan"]).spark)
        self.assertTrue(parser.parse_args(["run", "/tmp/plan", "--spark"]).spark)
        self.assertFalse(parser.parse_args(["run", "/tmp/plan", "--no-spark"]).spark)

    def test_non_codex_spark_is_rejected_by_shared_validation(self) -> None:
        namespace = argparse.Namespace(
            harness="claude",
            spark=True,
            review_harness=None,
            review_harness_model=None,
            audit_harness=None,
        )
        errors = run_flags.validate_run_namespace(namespace)
        self.assertIn("requires the initial --harness", errors[0].message)

    def test_prepare_commands_forward_spark(self) -> None:
        command = prepare_commands._format_run_command(
            Path("/tmp/plan"),
            cwd=Path("/tmp/repo"),
            harness_name="codex",
            harness_model="gpt-baseline",
            spark=True,
            review_harness=None,
            review_harness_model=None,
        )
        self.assertIn("--spark", command.split())

    def test_resume_restores_spark_and_explicit_no_spark_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_dir = root / "plan"
            plan_dir.mkdir()
            plan_state = state.plan_state_dir(plan_dir, root / "state")
            plan_state.mkdir(parents=True)
            (plan_state / "state.json").write_text(
                json.dumps({"run_config": {"spark": True}}), encoding="utf-8"
            )
            parser = cli.build_parser()

            restored = parser.parse_args(
                ["run", str(plan_dir), "--state-dir", str(root / "state"), "--resume"]
            )
            run_entry._apply_resume_config(
                restored,
                ["run", str(plan_dir), "--state-dir", str(root / "state"), "--resume"],
            )
            self.assertTrue(restored.spark)

            disabled_argv = [
                "run",
                str(plan_dir),
                "--state-dir",
                str(root / "state"),
                "--resume",
                "--no-spark",
            ]
            disabled = parser.parse_args(disabled_argv)
            run_entry._apply_resume_config(disabled, disabled_argv)
            self.assertFalse(disabled.spark)

    def test_spark_requires_resolved_distinct_baseline(self) -> None:
        parser = cli.build_parser()
        with tempfile.TemporaryDirectory() as tmp:
            unresolved = parser.parse_args(
                ["run", "/tmp/plan", "--cwd", tmp, "--spark"]
            )
            with (
                mock.patch.object(
                    config,
                    "native_model_payload",
                    return_value={
                        "model": config.NATIVE_MODEL_DEFAULT,
                        "model_source": config.MODEL_SOURCE_UNRESOLVED,
                    },
                ),
                self.assertRaisesRegex(runner.RunError, "resolved baseline"),
            ):
                run_inputs._validate_run_inputs(
                    unresolved,
                    resolve_review_guard_excludes=review_guard._review_guard_excludes,
                )

            same_model = parser.parse_args(
                [
                    "run",
                    "/tmp/plan",
                    "--cwd",
                    tmp,
                    "--spark",
                    "--harness-model",
                    run_selection.SPARK_MODEL,
                ]
            )
            with self.assertRaisesRegex(runner.RunError, "must differ"):
                run_inputs._validate_run_inputs(
                    same_model,
                    resolve_review_guard_excludes=review_guard._review_guard_excludes,
                )


class SparkSuitabilityTests(unittest.TestCase):
    def test_valid_marker_round_trips_and_renders(self) -> None:
        payload = _validator_payload()
        payload["sessions"][0]["spark"] = {
            "eligible": True,
            "reason": "small explicit change with a settled contract",
        }
        validation = prepare_parsing.parse_validator_output(payload)
        session = validation.sessions[0]
        self.assertTrue(session.spark.eligible)
        self.assertEqual(
            validation,
            prepare_parsing.parse_validator_output(validation.to_dict()),
        )
        self.assertIn(
            "**Spark eligible**: yes",
            prepare_expansion.render_expansion(session, codebase_root="/repo"),
        )

    def test_missing_and_malformed_markers_use_baseline(self) -> None:
        missing = prepare_parsing.parse_validator_output(_validator_payload()).sessions[
            0
        ]
        self.assertFalse(missing.spark.eligible)
        self.assertIn("missing", missing.spark.reason)

        malformed_payload = _validator_payload()
        malformed_payload["sessions"][0]["spark"] = {"eligible": "yes"}
        malformed = prepare_parsing.parse_validator_output(malformed_payload).sessions[
            0
        ]
        self.assertFalse(malformed.spark.eligible)
        self.assertIn("malformed", malformed.spark.reason)


class SparkExpandIngestTests(unittest.TestCase):
    def _ingest(self, plan_dir: Path, batch: Path) -> dict:
        args = cli.build_parser().parse_args(
            ["expand-ingest", str(plan_dir), "--batch-file", str(batch), "--json"]
        )
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = cli.resolve_handler(args)(args)
        self.assertEqual(0, code)
        return json.loads(stdout.getvalue())

    def test_marker_reaches_the_stored_aggregate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = tmp_path / "plan"
            _write_prepare_plan(plan_dir, ("1",))
            payload = _validator_payload(
                spark={"eligible": True, "reason": "single-file change, settled scope"}
            )
            batch = tmp_path / "batch.json"
            batch.write_text(json.dumps(payload), encoding="utf-8")

            body = self._ingest(plan_dir, batch)

            self.assertEqual([], body["spark_missing"])
            stored = prepare_validation_store.load_prepare_validation(plan_dir)
            assert stored is not None
            session = stored.session("1")
            assert session is not None
            self.assertTrue(session.spark.eligible)

    def test_dropped_marker_is_reported_per_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = tmp_path / "plan"
            _write_prepare_plan(plan_dir, ("1",))
            batch = tmp_path / "batch.json"
            batch.write_text(json.dumps(_validator_payload()), encoding="utf-8")

            body = self._ingest(plan_dir, batch)

            self.assertEqual(["1"], body["spark_missing"])


class SparkRoutingTests(unittest.TestCase):
    @staticmethod
    def _args(**overrides: object) -> argparse.Namespace:
        values = {
            "harness": "codex",
            "harness_model": "gpt-baseline",
            "review_harness_model": None,
            "effort": "high",
            "spark": True,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    @mock.patch.object(
        run_selection,
        "_session_spark_suitability",
        return_value=(True, "small explicit session"),
    )
    def test_eligible_codex_routes_spark(self, _suitability: mock.Mock) -> None:
        model, selected, route = run_selection._spark_turn_route(
            self._args(),
            plan_dir=Path("/tmp/plan"),
            session_path=Path("/tmp/plan/session-1.md"),
            session_id="1",
            harness="codex",
            is_review_turn=False,
            implementation_overrides={},
            spark_state={"available": True},
            run_suppression=None,
        )
        self.assertEqual(run_selection.SPARK_MODEL, model)
        self.assertEqual(config.MODEL_SOURCE_SPARK, selected["model_source"])
        self.assertEqual("spark", route["decision"])
        self.assertEqual("high", selected["effort"])

    @mock.patch.object(
        run_selection,
        "_session_spark_suitability",
        return_value=(True, "small explicit session"),
    )
    def test_override_and_cooldown_route_baseline(
        self, _suitability: mock.Mock
    ) -> None:
        for overrides, availability_state, suppression, cause in (
            (
                {"codex": {"model": "gpt-live"}},
                {"available": True},
                None,
                "live-set-model-override",
            ),
            (
                {},
                {"available": False, "reason": "quota-exhausted"},
                None,
                "quota-exhausted",
            ),
            (
                {},
                {"available": True},
                "quota-exhausted-for-current-run",
                "quota-exhausted-for-current-run",
            ),
        ):
            with self.subTest(cause=cause):
                model, _selected, route = run_selection._spark_turn_route(
                    self._args(),
                    plan_dir=Path("/tmp/plan"),
                    session_path=Path("/tmp/plan/session-1.md"),
                    session_id="1",
                    harness="codex",
                    is_review_turn=False,
                    implementation_overrides=overrides,
                    spark_state=availability_state,
                    run_suppression=suppression,
                )
                self.assertNotEqual(run_selection.SPARK_MODEL, model)
                self.assertEqual("baseline", route["decision"])
                self.assertEqual(cause, route["suppression_cause"])


class ModelAvailabilityTests(unittest.TestCase):
    def test_live_block_and_early_reset_reconcile_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model = run_selection.SPARK_MODEL
            blocked = spark_status.SparkStatus(
                "blocked", "window-exhausted", "2100-01-01T01:00:00Z"
            )
            result = model_availability.reconcile_live(
                root, model, blocked, run_id="run-1"
            )
            self.assertFalse(result.available)
            self.assertEqual(blocked.retry_at, result.retry_at)
            unknown = spark_status.SparkStatus("unknown", "rate-limit-read-failed")
            self.assertFalse(
                model_availability.reconcile_live(
                    root, model, unknown, run_id="run-1"
                ).available
            )
            self.assertTrue(
                model_availability.reconcile_live(
                    root / "fresh", model, unknown, run_id="run-2"
                ).available
            )
            available = spark_status.SparkStatus("available", "rate-limit-available")
            self.assertTrue(
                model_availability.reconcile_live(
                    root, model, available, run_id="run-1"
                ).available
            )
            self.assertTrue(model_availability.availability(root, model).available)

    def test_provider_dated_retry_hint_uses_local_timezone(self) -> None:
        hint = retry_hints.extract_retry_hint(
            "You've hit your usage limit. Try again at Sep 19th, 2026 3:33 PM."
        )
        self.assertEqual("Sep 19th, 2026 3:33 PM", hint)
        expected = (
            datetime(2026, 9, 19, 15, 33)
            .astimezone()
            .astimezone(UTC)
            .isoformat()
            .replace("+00:00", "Z")
        )
        self.assertEqual(expected, retry_hints.normalized_retry_at(hint))

    def test_cooldown_is_shared_and_expires(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = datetime(2026, 8, 14, 12, tzinfo=UTC)
            recorded = model_availability.suppress_until(
                root,
                run_selection.SPARK_MODEL,
                reason="quota-exhausted",
                retry_hint="3600",
                run_id="run-1",
                now=now,
            )
            self.assertIsNotNone(recorded)
            self.assertFalse(
                model_availability.availability(
                    root, run_selection.SPARK_MODEL, now=now + timedelta(minutes=30)
                ).available
            )
            self.assertTrue(
                model_availability.availability(
                    root, run_selection.SPARK_MODEL, now=now + timedelta(hours=2)
                ).available
            )

    def test_corrupt_ledger_routes_baseline_with_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / model_availability.LEDGER_FILENAME).write_text(
                "{", encoding="utf-8"
            )
            availability = model_availability.availability(
                root, run_selection.SPARK_MODEL
            )
            self.assertFalse(availability.available)
            self.assertEqual("ledger-unreadable", availability.reason)
            self.assertIsNotNone(availability.warning)

    def test_concurrent_updates_preserve_both_models(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = datetime(2026, 8, 14, 12, tzinfo=UTC)
            threads = [
                threading.Thread(
                    target=model_availability.suppress_until,
                    args=(root, model),
                    kwargs={
                        "reason": "quota-exhausted",
                        "retry_hint": "3600",
                        "run_id": model,
                        "now": now,
                    },
                )
                for model in ("spark-a", "spark-b")
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            payload = json.loads(
                (root / model_availability.LEDGER_FILENAME).read_text(encoding="utf-8")
            )
            self.assertEqual({"spark-a", "spark-b"}, set(payload["models"]))


class SparkFallbackIntegrationTests(unittest.TestCase):
    def test_live_early_reset_routes_next_eligible_turn_to_spark(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_git_repo(root)
            plan_dir = root / "plan"
            state_root = root / "state"
            write_plan(plan_dir, ("pending", "pending"))
            validation_payload = _validator_payload(
                spark={"eligible": True, "reason": "small explicit fixture session"}
            )
            second = dict(validation_payload["sessions"][0])
            second["session_id"] = "2"
            validation_payload["sessions"].append(second)
            (plan_dir / prepare_models.PREPARE_VALIDATION_FILENAME).write_text(
                json.dumps(validation_payload), encoding="utf-8"
            )
            install_fake_codex(root)
            argv = [
                "run",
                str(plan_dir),
                "--cwd",
                str(root),
                "--state-dir",
                str(state_root),
                "--harness-model",
                "gpt-baseline",
                "--spark",
                "--allow-not-loop-ready",
                "--no-supervisor-audit",
                "--no-mailbox",
                "--json",
            ]
            args = cli.build_parser().parse_args(argv)
            output = io.StringIO()
            live = [
                spark_status.SparkStatus(
                    "blocked", "window-exhausted", "2026-09-19T21:40:00Z"
                ),
                spark_status.SparkStatus("available", "rate-limit-available"),
            ]
            with (
                patched_env(
                    PATH=f"{root}:{os.environ.get('PATH', '')}",
                    FAKE_CODEX_MODE="success",
                ),
                mock.patch.object(
                    spark_status, "read_status", side_effect=live
                ) as probe,
                redirect_stdout(output),
            ):
                code = runner.run_from_args(args, run_argv=argv)
            payload = json.loads(output.getvalue())
            self.assertEqual(0, code)
            self.assertEqual(2, probe.call_count)
            self.assertEqual(
                ["gpt-baseline", run_selection.SPARK_MODEL],
                [turn["model"] for turn in payload["turns"]],
            )
            self.assertTrue(
                model_availability.availability(
                    state_root, run_selection.SPARK_MODEL
                ).available
            )

    def test_quota_failure_retries_same_pending_session_on_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_git_repo(root)
            plan_dir = root / "plan"
            state_root = root / "state"
            write_plan(plan_dir, ("pending",))
            validation_payload = _validator_payload()
            validation_payload["sessions"][0]["spark"] = {
                "eligible": True,
                "reason": "small explicit fixture session",
            }
            (plan_dir / prepare_models.PREPARE_VALIDATION_FILENAME).write_text(
                json.dumps(validation_payload), encoding="utf-8"
            )
            install_fake_codex(root)
            argv = [
                "run",
                str(plan_dir),
                "--cwd",
                str(root),
                "--state-dir",
                str(state_root),
                "--harness-model",
                "gpt-baseline",
                "--spark",
                "--allow-not-loop-ready",
                "--no-supervisor-audit",
                "--no-mailbox",
                "--json",
            ]
            args = cli.build_parser().parse_args(argv)
            output = io.StringIO()
            with (
                patched_env(
                    PATH=f"{root}:{os.environ.get('PATH', '')}",
                    FAKE_CODEX_MODE="success",
                    FAKE_CODEX_SPARK_FAILURE="quota",
                ),
                mock.patch.object(
                    spark_status,
                    "read_status",
                    return_value=spark_status.SparkStatus(
                        "available", "rate-limit-available"
                    ),
                ),
                redirect_stdout(output),
            ):
                code = runner.run_from_args(args, run_argv=argv)

            payload = json.loads(output.getvalue())
            self.assertEqual(0, code)
            self.assertEqual("all-complete", payload["reason"])
            self.assertEqual(2, len(payload["turns"]))
            self.assertEqual(run_selection.SPARK_MODEL, payload["turns"][0]["model"])
            self.assertEqual("gpt-baseline", payload["turns"][1]["model"])
            self.assertEqual(
                "quota-exhausted",
                payload["turns"][0]["spark_route"]["fallback_reason"],
            )
            status = state.status_payload(plans.inspect_plan(plan_dir), state_root)
            self.assertEqual("complete", status["sessions"][0]["status"])
