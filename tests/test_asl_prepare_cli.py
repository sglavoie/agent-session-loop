"""`asl prepare` command tests."""

from __future__ import annotations

import io
import json
import shlex
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    cli,
    harnesses,
    plans_files,
    prepare,
    prepare_cli,
    prepare_models,
    prepare_results,
)
from tests.support import (
    _prepare_pipeline_payload,
    _write_prepare_source_plan,
    init_git_repo,
)


class PrepareCliTests(unittest.TestCase):
    """`asl prepare` CLI wiring and input validation."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.base_dir = self.tmp_path / "plans"
        self.state_dir = self.tmp_path / "state"
        self.state_dir.mkdir()
        self.repo = self.tmp_path / "repo"
        init_git_repo(self.repo)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_source(self, **kwargs: object) -> Path:
        return _write_prepare_source_plan(self.tmp_path / "demo-plan.md", **kwargs)

    def _run_cli(self, *argv: str) -> tuple[int, str, str]:
        args = cli.build_parser().parse_args(["prepare", *argv])
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.resolve_handler(args)(args)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_parser_defaults_and_handler_wiring(self) -> None:
        args = cli.build_parser().parse_args(["prepare", "plan.md"])

        self.assertIs(prepare_cli.prepare_from_args, cli.resolve_handler(args))
        self.assertEqual("plan.md", args.plan)
        self.assertFalse(args.resume)
        self.assertFalse(args.rebind_workspace)
        self.assertEqual(".", args.cwd)
        self.assertEqual("codex", args.harness)
        self.assertIsNone(args.harness_model)
        self.assertEqual("codex", args.validator_harness)
        self.assertIsNone(args.validator_model)
        self.assertIsNone(args.validator_effort)
        self.assertEqual(prepare_models.DEFAULT_VALIDATOR_JOBS, args.validator_jobs)
        self.assertEqual(
            prepare_models.DEFAULT_VALIDATOR_TIMEOUT_SECONDS, args.validator_timeout
        )
        self.assertEqual(
            prepare_models.DEFAULT_VALIDATOR_HEARTBEAT_SECONDS,
            args.validator_heartbeat,
        )
        self.assertIsNone(args.review_harness)
        self.assertIsNone(args.review_harness_model)
        self.assertIsNone(args.base_dir)
        self.assertIsNone(args.state_dir)
        self.assertTrue(args.unsafe_bypass)
        self.assertFalse(args.json)

    def test_review_harness_equal_to_harness_is_input_error(self) -> None:
        source = self._write_source()
        code, stdout, _stderr = self._run_cli(
            str(source),
            "--cwd",
            str(self.repo),
            "--harness",
            "codex",
            "--review-harness",
            "codex",
            "--json",
        )

        self.assertEqual(1, code)
        payload = json.loads(stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual("prepare", payload["command"])
        self.assertEqual("input-error", payload["reason"])
        self.assertIn("must differ", payload["error"])

    def test_rebind_workspace_requires_resume(self) -> None:
        source = self._write_source()
        code, stdout, _stderr = self._run_cli(
            str(source),
            "--cwd",
            str(self.repo),
            "--rebind-workspace",
            "--json",
        )

        self.assertEqual(1, code)
        payload = json.loads(stdout)
        self.assertEqual("input-error", payload["reason"])
        self.assertTrue(payload["rebind_workspace"])
        self.assertIn("requires --resume", payload["error"])

    def test_rebind_concurrency_error_has_distinct_reason_and_context(self) -> None:
        plan_dir = self.tmp_path / "plan"
        plan_dir.mkdir()
        (plan_dir / "manifest.md").write_text("# Plan\n", encoding="utf-8")
        (plan_dir / "context.md").write_text("# Context\n", encoding="utf-8")
        rebind_error = prepare_results._WorkspaceRebindConcurrencyError(
            "cannot lock plan for workspace rebind: busy",
            old_workspace={"workspace_root": "/old"},
            new_workspace={"workspace_root": "/new"},
            stage=prepare_results.PREPARE_STAGE_WORKSPACE,
        )

        with mock.patch.object(prepare_cli, "prepare_plan", side_effect=rebind_error):
            code, stdout, _stderr = self._run_cli(
                str(plan_dir),
                "--resume",
                "--rebind-workspace",
                "--cwd",
                str(self.repo),
                "--json",
            )

        self.assertEqual(1, code)
        payload = json.loads(stdout)
        self.assertEqual("workspace-rebind-failed", payload["reason"])
        self.assertTrue(payload["rebind_workspace"])
        self.assertEqual(
            prepare_results.PREPARE_STAGE_WORKSPACE,
            payload["workspace_rebind"]["failure_stage"],
        )
        self.assertEqual("/old", payload["workspace_rebind"]["old"]["workspace_root"])
        self.assertEqual("/new", payload["workspace_rebind"]["new"]["workspace_root"])

    def test_run_parser_rejects_rebind_workspace(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            cli.build_parser().parse_args(["run", "/tmp/plan", "--rebind-workspace"])
        self.assertEqual(2, raised.exception.code)
        self.assertIn("unrecognized arguments", stderr.getvalue())

    def test_review_harness_model_requires_review_harness(self) -> None:
        source = self._write_source()
        code, stdout, _stderr = self._run_cli(
            str(source),
            "--cwd",
            str(self.repo),
            "--review-harness-model",
            "claude-opus-4",
            "--json",
        )

        self.assertEqual(1, code)
        payload = json.loads(stdout)
        self.assertEqual("input-error", payload["reason"])
        self.assertEqual("claude-opus-4", payload["review_harness_model"])
        self.assertIn("requires --review-harness", payload["error"])

    def test_validator_numeric_controls_must_be_positive(self) -> None:
        source = self._write_source()
        for flag in (
            "--validator-jobs",
            "--validator-timeout",
            "--validator-heartbeat",
        ):
            with self.subTest(flag=flag):
                code, stdout, _stderr = self._run_cli(
                    str(source),
                    "--cwd",
                    str(self.repo),
                    flag,
                    "0",
                    "--json",
                )

                self.assertEqual(1, code)
                payload = json.loads(stdout)
                self.assertEqual("input-error", payload["reason"])
                self.assertIn("must be a positive integer", payload["error"])

    def test_missing_source_file_is_input_error(self) -> None:
        code, stdout, _stderr = self._run_cli(
            str(self.tmp_path / "missing.md"),
            "--cwd",
            str(self.repo),
            "--json",
        )

        self.assertEqual(1, code)
        payload = json.loads(stdout)
        self.assertEqual("input-error", payload["reason"])
        self.assertIn("source plan file does not exist", payload["error"])

    def test_missing_cwd_is_cwd_error(self) -> None:
        source = self._write_source()
        code, stdout, _stderr = self._run_cli(
            str(source),
            "--cwd",
            str(self.tmp_path / "no-such-repo"),
            "--json",
        )

        self.assertEqual(1, code)
        payload = json.loads(stdout)
        self.assertEqual("cwd-error", payload["reason"])

    def test_resume_requires_existing_plan_directory(self) -> None:
        empty_dir = self.tmp_path / "not-a-plan"
        empty_dir.mkdir()
        code, stdout, _stderr = self._run_cli(
            str(empty_dir),
            "--resume",
            "--cwd",
            str(self.repo),
            "--json",
        )

        self.assertEqual(1, code)
        payload = json.loads(stdout)
        self.assertEqual("input-error", payload["reason"])
        self.assertIn("manifest.md", payload["error"])

    def test_single_session_source_returns_needs_plan_iteration(self) -> None:
        source = self._write_source(session_count=1)
        code, stdout, _stderr = self._run_cli(
            str(source),
            "--cwd",
            str(self.repo),
            "--base-dir",
            str(self.base_dir),
            "--json",
        )

        self.assertEqual(1, code)
        payload = json.loads(stdout)
        self.assertEqual("needs-plan-iteration", payload["reason"])
        self.assertIsNone(payload["plan_dir"])
        message = payload["blockers"][0]["message"]
        self.assertIn("supported H2-H6", message)
        self.assertIn("'Session <id>' headings", message)
        self.assertNotIn("'## Session <id>'", message)
        # The structure gate fails before any output directory is created.
        self.assertFalse(self.base_dir.exists())

    def test_malformed_session_heading_fails_structure_gate_atomically(self) -> None:
        malformed = "#### Session 02 / Broken"
        source = self._write_source(heading_by_id={"2": malformed})
        source_lines = source.read_text(encoding="utf-8").splitlines()
        line_number = source_lines.index(malformed) + 1

        code, stdout, _stderr = self._run_cli(
            str(source),
            "--cwd",
            str(self.repo),
            "--base-dir",
            str(self.base_dir),
            "--json",
        )

        self.assertEqual(1, code)
        payload = json.loads(stdout)
        self.assertEqual("needs-plan-iteration", payload["reason"])
        self.assertIsNone(payload["plan_dir"])
        self.assertEqual(1, len(payload["blockers"]))
        blocker = payload["blockers"][0]
        self.assertEqual("structure", blocker["stage"])
        self.assertIn(f"line {line_number}", blocker["message"])
        self.assertIn("unsupported title separator", blocker["message"])
        self.assertIn(malformed, blocker["message"])
        self.assertFalse(self.base_dir.exists())

    def test_missing_contract_returns_needs_plan_iteration(self) -> None:
        source = self._write_source(omit_contract_for=("2",))
        code, stdout, _stderr = self._run_cli(
            str(source),
            "--cwd",
            str(self.repo),
            "--base-dir",
            str(self.base_dir),
            "--json",
        )

        self.assertEqual(1, code)
        payload = json.loads(stdout)
        self.assertEqual("needs-plan-iteration", payload["reason"])
        blockers = payload["blockers"]
        self.assertEqual(1, len(blockers))
        self.assertEqual("2", blockers[0]["session_id"])
        self.assertIn("Completion Contract", blockers[0]["message"])
        self.assertFalse(self.base_dir.exists())

    def test_prepare_success_via_default_validator_harness(self) -> None:
        source = self._write_source()
        canonical_repo = plans_files.canonicalize_path(self.repo)
        payload = _prepare_pipeline_payload(str(canonical_repo), ("1", "2"))

        def fake_run_harness(name: str, **kwargs: object) -> harnesses.HarnessResult:
            self.assertEqual("claude", name)
            self.assertTrue(
                str(kwargs["prompt"]).startswith(
                    prepare_models.VALIDATION_PACKET_HEADING_PREFIX
                )
            )
            self.assertEqual("claude-opus-validator", kwargs["model"])
            self.assertEqual("high", kwargs["effort"])
            self.assertEqual(17, kwargs["timeout"])
            self.assertFalse(kwargs["unsafe_bypass"])
            self.assertIsNone(kwargs["plan_dir"])
            access = kwargs["access"]
            assert isinstance(access, harnesses.AccessProfile)
            self.assertEqual("audit-read-only", access.name)
            return harnesses.HarnessResult(
                argv=["claude"],
                returncode=0,
                stdout="",
                stderr="",
                last_message=json.dumps(payload),
                name="claude",
            )

        with mock.patch.object(
            harnesses, "run_harness", side_effect=fake_run_harness
        ) as run_mock:
            code, stdout, stderr = self._run_cli(
                str(source),
                "--cwd",
                str(self.repo),
                "--harness",
                "codex",
                "--harness-model",
                "gpt-5",
                "--validator-harness",
                "claude",
                "--validator-model",
                "claude-opus-validator",
                "--validator-effort",
                "high",
                "--validator-jobs",
                "3",
                "--validator-timeout",
                "17",
                "--validator-heartbeat",
                "4",
                "--review-harness",
                "claude",
                "--review-harness-model",
                "claude-opus-4",
                "--base-dir",
                str(self.base_dir),
                "--state-dir",
                str(self.state_dir),
                "--json",
            )

        self.assertEqual(0, code, stderr)
        response = json.loads(stdout)
        self.assertTrue(response["ok"])
        self.assertEqual("ok", response["reason"])
        self.assertEqual("gpt-5", response["harness_model"])
        self.assertEqual("claude-opus-validator", response["validator_model"])
        self.assertEqual("high", response["validator_effort"])
        self.assertEqual(3, response["validator_jobs"])
        self.assertEqual(17, response["validator_timeout"])
        self.assertEqual(4, response["validator_heartbeat"])
        self.assertEqual("claude", response["validator"]["harness"])
        self.assertEqual("claude-opus-4", response["review_harness_model"])
        self.assertTrue(response["marked_loop_ready"])
        self.assertEqual(1, run_mock.call_count)
        plan_dir = Path(response["plan_dir"])
        self.assertTrue((plan_dir / "session-1.md").is_file())
        self.assertTrue((plan_dir / "session-1.expand.md").is_file())
        manifest = (plan_dir / "manifest.md").read_text(encoding="utf-8")
        self.assertIn("**Readiness**: loop-ready", manifest)
        self.assertEqual(
            shlex.join(
                [
                    "asl",
                    "run",
                    str(plan_dir),
                    "--cwd",
                    str(canonical_repo),
                    "--harness",
                    "codex",
                    "--harness-model",
                    "gpt-5",
                    "--review-harness",
                    "claude",
                    "--review-harness-model",
                    "claude-opus-4",
                ]
            ),
            response["run_command"],
        )
        self.assertIn("--harness-model gpt-5", response["resume_command"])
        self.assertIn(
            "--validator-model claude-opus-validator", response["resume_command"]
        )
        self.assertIn("--validator-effort high", response["resume_command"])
        self.assertIn("--validator-jobs 3", response["resume_command"])
        self.assertIn("--validator-timeout 17", response["resume_command"])
        self.assertIn("--validator-heartbeat 4", response["resume_command"])
        self.assertIn(
            "--review-harness-model claude-opus-4",
            response["resume_command"],
        )

    def test_omitted_validator_selection_uses_harness_native_defaults(self) -> None:
        source = self._write_source()
        canonical_repo = plans_files.canonicalize_path(self.repo)
        payload = _prepare_pipeline_payload(str(canonical_repo), ("1", "2"))

        def fake_run_harness(name: str, **kwargs: object) -> harnesses.HarnessResult:
            self.assertEqual("codex", name)
            self.assertIsNone(kwargs["model"])
            self.assertIsNone(kwargs["effort"])
            self.assertEqual(
                prepare_models.DEFAULT_VALIDATOR_TIMEOUT_SECONDS, kwargs["timeout"]
            )
            return harnesses.HarnessResult(
                argv=["codex"],
                returncode=0,
                stdout="",
                stderr="",
                last_message=json.dumps(payload),
                name="codex",
            )

        with mock.patch.object(harnesses, "run_harness", side_effect=fake_run_harness):
            code, stdout, stderr = self._run_cli(
                str(source),
                "--cwd",
                str(self.repo),
                "--base-dir",
                str(self.base_dir),
                "--state-dir",
                str(self.state_dir),
                "--json",
            )

        self.assertEqual(0, code, stderr)
        response = json.loads(stdout)
        self.assertIsNone(response["validator_model"])
        self.assertIsNone(response["validator_effort"])

    def test_validator_heartbeat_keeps_json_stdout_clean(self) -> None:
        source = self._write_source()
        canonical_repo = plans_files.canonicalize_path(self.repo)
        payload = _prepare_pipeline_payload(str(canonical_repo), ("1", "2"))

        def slow_launch(*_args: object, **_kwargs: object) -> object:
            time.sleep(1.1)
            return payload

        with mock.patch.object(prepare, "_launch_validator", side_effect=slow_launch):
            code, stdout, stderr = self._run_cli(
                str(source),
                "--cwd",
                str(self.repo),
                "--base-dir",
                str(self.base_dir),
                "--state-dir",
                str(self.state_dir),
                "--validator-heartbeat",
                "1",
                "--json",
            )

        self.assertEqual(0, code, stderr)
        self.assertTrue(json.loads(stdout)["ok"])
        self.assertIn("validator heartbeat", stderr)

    def test_text_output_reports_resume_command_on_validator_failure(self) -> None:
        source = self._write_source()

        def failing_run_harness(name: str, **kwargs: object) -> harnesses.HarnessResult:
            return harnesses.HarnessResult(
                argv=["codex"],
                returncode=1,
                stdout="",
                stderr="usage limit reached",
                name="codex",
            )

        with mock.patch.object(
            harnesses, "run_harness", side_effect=failing_run_harness
        ):
            code, stdout, stderr = self._run_cli(
                str(source),
                "--cwd",
                str(self.repo),
                "--harness-model",
                "gpt-5",
                "--review-harness",
                "claude",
                "--review-harness-model",
                "claude-opus-4",
                "--base-dir",
                str(self.base_dir),
                "--state-dir",
                str(self.state_dir),
            )

        self.assertEqual(1, code)
        self.assertIn("Resume with: asl prepare --resume", stdout)
        self.assertIn("--harness-model gpt-5", stdout)
        self.assertIn("--review-harness claude", stdout)
        self.assertIn("--review-harness-model claude-opus-4", stdout)
        self.assertIn("asl prepare:", stderr)
        # The failed preparation directory is preserved as draft.
        plan_dirs = list(self.base_dir.iterdir())
        self.assertEqual(1, len(plan_dirs))
        manifest = (plan_dirs[0] / "manifest.md").read_text(encoding="utf-8")
        self.assertIn("**Readiness**: draft", manifest)
