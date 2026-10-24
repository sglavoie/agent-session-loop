"""Run flag validation, resume gate, and repository contention gate tests."""

from __future__ import annotations

import argparse
import io
import json
import os
import socket
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest import mock

from agent_session_loop import (
    cli,
    config,
    events,
    harnesses,
    plans_files,
    plans_metadata,
    plans_models,
    plans_registry,
    preflight,
    preflight_models,
    readiness_gate,
    review_guard,
    run_entry_resume,
    run_inputs,
    runner,
    session_overview,
    state,
)
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    REPO_ROOT,
    _contract_block,
    _load_script_module,
    init_git_repo,
    init_multi_repo_workspace,
    install_fake_codex,
    patched_env,
    write_multi_repo_plan,
)

ASL_TEST_LANE = "integration"


class RunFlagValidationTests(unittest.TestCase):
    autopilot: ModuleType

    @classmethod
    def setUpClass(cls) -> None:
        cls.autopilot = _load_script_module(
            "asl_autopilot_run_gates",
            "skills",
            "asl",
            "scripts",
            "asl_autopilot.py",
        )

    def test_cycle_cap_above_ceiling_is_rejected_identically_by_runner_and_helper(
        self,
    ) -> None:
        message = "--max-review-fix-cycles 9 exceeds --max-review-fix-cycles-ceiling 8"
        args = argparse.Namespace(
            cwd=str(REPO_ROOT),
            max_review_fix_cycles=9,
            max_review_fix_cycles_ceiling=8,
        )

        with self.assertRaises(runner.RunError) as caught:
            run_inputs._validate_run_inputs(
                args, resolve_review_guard_excludes=review_guard._review_guard_excludes
            )
        self.assertEqual(message, str(caught.exception))

        result = self.autopilot.parse_command(
            "asl run /tmp/plan --max-review-fix-cycles 9 "
            "--max-review-fix-cycles-ceiling 8"
        )
        self.assertFalse(result["ok"])
        self.assertEqual("invalid-argument", result["reason"])
        self.assertEqual(message, result["error"])

    def test_helper_forwards_new_review_cap_flags(self) -> None:
        result = self.autopilot.parse_command(
            "asl run /tmp/plan --max-repair-group-attempts 3 "
            "--max-review-fix-cycles-ceiling 12"
        )

        self.assertTrue(result["ok"], result)
        args = result["run_args"]
        self.assertEqual(args[args.index("--max-repair-group-attempts") + 1], "3")
        self.assertEqual(args[args.index("--max-review-fix-cycles-ceiling") + 1], "12")

    def test_resume_overlay_restores_new_review_cap_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = tmp_path / "plan"
            plan_dir.mkdir()
            state_root = tmp_path / "state"
            plan_state_dir = state.plan_state_dir(plan_dir, state_root)
            plan_state_dir.mkdir(parents=True, exist_ok=True)
            (plan_state_dir / events.STATE_FILE_NAME).write_text(
                json.dumps(
                    {
                        "run_config": {
                            "max_repair_group_attempts": 3,
                            "max_review_fix_cycles_ceiling": 12,
                        }
                    }
                ),
                encoding="utf-8",
            )

            restored = run_entry_resume._apply_resume_config(
                argparse.Namespace(
                    plan_dir=str(plan_dir),
                    state_dir=str(state_root),
                    resume=True,
                ),
                ["run", str(plan_dir), "--state-dir", str(state_root), "--resume"],
            )

        self.assertEqual(3, restored.max_repair_group_attempts)
        self.assertEqual(12, restored.max_review_fix_cycles_ceiling)


class ResumeGateTests(unittest.TestCase):
    def _report(
        self,
        *,
        state: str | None,
        findings: tuple[preflight_models.PreflightFinding, ...] = (),
    ) -> preflight_models.PreflightReport:
        readiness = None
        if state is not None:
            readiness = plans_models.ReadinessMetadata(
                state=state,
                readiness_sha256="0" * 64,
                checked_at="2026-06-19T00:00:00Z",
            )
        return preflight_models.PreflightReport(
            plan_dir=Path("/plan"),
            findings=findings,
            readiness=readiness,
            expected_readiness_sha256="0" * 64,
        )

    def test_resume_gate_accepts_every_resumable_readiness_state(self) -> None:
        for readiness_state in preflight.RESUME_READINESS_STATES:
            with self.subTest(state=readiness_state):
                self.assertTrue(
                    readiness_gate._resume_gate_passed(
                        self._report(state=readiness_state)
                    )
                )

    def test_resume_gate_rejects_other_states_and_findings(self) -> None:
        for readiness_state in ("draft", "reviewed", "complete"):
            with self.subTest(state=readiness_state):
                self.assertFalse(
                    readiness_gate._resume_gate_passed(
                        self._report(state=readiness_state)
                    )
                )
        self.assertFalse(readiness_gate._resume_gate_passed(self._report(state=None)))
        for severity in ("red", "yellow"):
            with self.subTest(severity=severity):
                report = self._report(
                    state="running",
                    findings=(
                        preflight_models.PreflightFinding(
                            severity=severity,
                            category="env-harness",
                            message="broken",
                        ),
                    ),
                )
                self.assertFalse(readiness_gate._resume_gate_passed(report))

    def test_readiness_gate_still_requires_loop_ready(self) -> None:
        self.assertTrue(
            readiness_gate._readiness_gate_passed(self._report(state="loop-ready"))
        )
        self.assertFalse(
            readiness_gate._readiness_gate_passed(self._report(state="running"))
        )


class RunRepositoryContentionGateTests(unittest.TestCase):
    """Startup refusal when another live run already claims a registered repo."""

    DEAD_PID = 4_242_424

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.workspace_root = self.tmp_path / "workspace"
        self.repositories = init_multi_repo_workspace(
            self.workspace_root,
            DEFAULT_MULTI_REPO_SPECS[:2],
        )
        self.unregistered = self.workspace_root / "unrelated_repo"
        init_git_repo(self.unregistered)
        self.plan_dir = self.tmp_path / "plan"
        self.other_plan_dir = self.tmp_path / "other-plan"
        self.other_plan_dir.mkdir()
        self.state_dir = self.tmp_path / "state"
        self.config_home = self.tmp_path / "config"
        write_multi_repo_plan(
            self.plan_dir,
            ("pending",),
            specs=DEFAULT_MULTI_REPO_SPECS[:2],
            contracts_by_id={
                "1": _contract_block(
                    '{"type": "file_exists", "repo": "SVC", '
                    '"path": "README.md", "name": "svc-readme"}',
                    session="1",
                )
            },
        )
        registry = plans_registry.read_repository_registry(self.plan_dir)
        assert registry is not None
        plans_metadata.save_workspace_plan_metadata(
            self.plan_dir,
            self.workspace_root,
            registry.digest,
        )
        fake_codex = install_fake_codex(self.tmp_path)
        config_path = self.config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps({"codex": {"executable": str(fake_codex)}}),
            encoding="utf-8",
        )

    def _root(self, path: Path) -> str:
        return str(plans_files.canonicalize_path(path))

    def _publish_rival_claim(self, roots: tuple[Path, ...], *, pid: int) -> None:
        canonical = plans_files.canonicalize_path(self.other_plan_dir)
        rival_state = self.state_dir / plans_files.path_sha256(canonical)
        rival_state.mkdir(parents=True, exist_ok=True)
        (rival_state / session_overview.STATE_JSON_NAME).write_text(
            json.dumps(
                {
                    "plan_dir": str(canonical),
                    "run_id": "20260812T000000Z-1",
                    "harness": "codex",
                }
            ),
            encoding="utf-8",
        )
        (rival_state / session_overview.LOCK_JSON_NAME).write_text(
            json.dumps(
                {
                    "version": 2,
                    "plan_dir": str(canonical),
                    "pid": pid,
                    "hostname": socket.gethostname(),
                    "repositories": sorted(self._root(root) for root in roots),
                    "created_at": "2026-08-12T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )

    def _complete_session_one(
        self, *_args: object, **_kwargs: object
    ) -> harnesses.HarnessResult:
        session_path = self.plan_dir / "session-1.md"
        session_path.write_text(
            session_path.read_text(encoding="utf-8").replace(
                "- **Status**: pending", "- **Status**: complete", 1
            ),
            encoding="utf-8",
        )
        return harnesses.HarnessResult(
            name="codex",
            argv=["fake"],
            returncode=0,
            stdout="ok\n",
            stderr="",
        )

    def _run(self, harness_side_effect: object) -> tuple[int, dict[str, Any]]:
        argv = [
            "run",
            str(self.plan_dir),
            "--cwd",
            str(self.workspace_root),
            "--harness",
            "codex",
            "--state-dir",
            str(self.state_dir),
            "--json",
            "--allow-not-loop-ready",
        ]
        args = cli.build_parser().parse_args(argv)
        out, err = io.StringIO(), io.StringIO()
        with (
            patched_env(XDG_CONFIG_HOME=str(self.config_home)),
            mock.patch.object(
                harnesses,
                "run_harness",
                side_effect=harness_side_effect,
            ),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            code = runner.run_from_args(args, run_argv=argv)
        return code, json.loads(out.getvalue())

    def test_overlapping_live_claim_refuses_before_any_harness_launch(
        self,
    ) -> None:
        self._publish_rival_claim(
            (self.repositories["OAK"], self.unregistered), pid=os.getpid()
        )

        exit_code, payload = self._run(
            AssertionError("the refusal must stop before the harness")
        )

        self.assertEqual(1, exit_code)
        self.assertTrue(payload["paused"])
        self.assertEqual(
            events.PAUSE_REASON_WORKSPACE_REPOSITORY_CONTENTION,
            payload["pause_reason"],
        )
        self.assertEqual([], payload["turns"])
        contention = payload["pause_details"]["repository_contention"]
        self.assertEqual(
            [self._root(self.repositories["OAK"])], contention["repositories"]
        )
        self.assertEqual(1, len(contention["conflicts"]))
        conflict = contention["conflicts"][0]
        self.assertEqual(self._root(self.other_plan_dir), conflict["plan_dir"])
        self.assertEqual(os.getpid(), conflict["pid"])
        self.assertEqual(socket.gethostname(), conflict["hostname"])
        self.assertEqual("20260812T000000Z-1", conflict["run_id"])
        self.assertIn(self._root(self.other_plan_dir), payload["pause_message"])
        self.assertIn("there is no override", payload["pause_message"])
        self.assertIn(
            "- **Status**: pending",
            (self.plan_dir / "session-1.md").read_text(encoding="utf-8"),
        )
        # The refusal is a startup exit, not a run pause: no run state exists.
        self.assertFalse(
            (
                state.plan_state_dir(self.plan_dir, self.state_dir)
                / events.EVENTS_FILE_NAME
            ).exists()
        )

    def test_disjoint_live_claim_lets_the_run_start(self) -> None:
        self._publish_rival_claim((self.unregistered,), pid=os.getpid())

        exit_code, payload = self._run(self._complete_session_one)

        self.assertEqual(0, exit_code, payload)
        self.assertFalse(payload["paused"])
        self.assertEqual(1, len(payload["turns"]))
        self.assertIn(
            "- **Status**: complete",
            (self.plan_dir / "session-1.md").read_text(encoding="utf-8"),
        )

    def test_an_overlapping_claim_from_a_dead_pid_does_not_refuse(self) -> None:
        self._publish_rival_claim((self.repositories["SVC"],), pid=self.DEAD_PID)

        with mock.patch.object(
            state,
            "is_pid_running",
            side_effect=lambda pid: pid == os.getpid(),
        ):
            exit_code, payload = self._run(self._complete_session_one)

        self.assertEqual(0, exit_code, payload)
        self.assertFalse(payload["paused"])
        self.assertIn(
            "- **Status**: complete",
            (self.plan_dir / "session-1.md").read_text(encoding="utf-8"),
        )


if __name__ == "__main__":
    unittest.main()
