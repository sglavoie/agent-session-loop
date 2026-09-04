"""Run readiness, verification enforcement, and journal integration tests.

Split from tests/test_agent_session_loop.py."""

from __future__ import annotations

import argparse
import ast
import dataclasses
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import ModuleType
from typing import Any, cast
from unittest import mock

from agent_session_loop import (
    agent_mux_delivery,
    baseline,
    cli,
    commit_groups,
    config,
    control_queue,
    events,
    handoff_correction,
    handoff_guards,
    harnesses,
    intervention,
    plans,
    preflight,
    readiness_gate,
    repository_evidence,
    review,
    review_guard,
    review_repair,
    review_session_body,
    review_session_contract,
    run_entry,
    run_inputs,
    run_loop,
    run_selection,
    runner,
    session_overview,
    session_verification,
    state,
    turn_execution,
    turn_postflight,
    turn_preflight,
    turn_preparation,
    turn_settlement,
    verification,
    workspace_evidence,
    workspaces,
)
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    REPO_ROOT,
    _command_contract,
    _contract_block,
    _load_script_module,
    _structured_review_finding,
    agent_mux_notify_enabled,
    fake_commit_harness_env,
    git_stdout,
    init_git_repo,
    init_multi_repo_workspace,
    install_fake_amx,
    install_fake_codex,
    patched_env,
    read_jsonl,
    write_completed_review_plan,
    write_multi_repo_plan,
    write_plan,
)


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

            restored = run_entry._apply_resume_config(
                argparse.Namespace(
                    plan_dir=str(plan_dir),
                    state_dir=str(state_root),
                    resume=True,
                ),
                ["run", str(plan_dir), "--state-dir", str(state_root), "--resume"],
            )

        self.assertEqual(3, restored.max_repair_group_attempts)
        self.assertEqual(12, restored.max_review_fix_cycles_ceiling)


class RunReadinessGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.plan_dir = self.tmp_path / "plan"
        self.state_dir = self.tmp_path / "state"
        self.repo_root = self.tmp_path / "repo"
        self.config_home = self.tmp_path / "config"
        init_git_repo(self.repo_root)
        self.fake_codex = install_fake_codex(self.tmp_path)
        config_path = self.config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps({"codex": {"executable": str(self.fake_codex)}}),
            encoding="utf-8",
        )

    def _write_plan(
        self,
        statuses: tuple[str, ...] = ("pending",),
        *,
        readiness: str | None = "loop-ready",
        sha_override: str | None = None,
        contracts_by_id: dict[str, str] | None = None,
    ) -> plans.ReadinessMetadata | None:
        contracts = contracts_by_id or {
            str(index): _contract_block(
                '{"type": "file_exists", "path": "README.md", '
                f'"name": "readme-{index}"}}',
                session=str(index),
            )
            for index in range(1, len(statuses) + 1)
        }
        write_plan(self.plan_dir, statuses, contracts_by_id=contracts)
        if readiness is None:
            return None
        sha = sha_override or plans.stable_readiness_sha256(self.plan_dir)
        metadata = plans.ReadinessMetadata(
            state=readiness,
            readiness_sha256=sha,
            checked_at="2026-06-19T00:00:00Z",
        )
        plans.write_readiness_metadata(self.plan_dir, metadata)
        return metadata

    def _run(
        self,
        *,
        mode: str = "success",
        max_sessions: int | None = None,
        max_pending_sessions: int | None = None,
        allow_not_loop_ready: bool = False,
        turn_timeout: int | None = None,
        env_overrides: dict[str, str] | None = None,
        process_timeout: int = 20,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
        argv = [
            sys.executable,
            "-m",
            "agent_session_loop",
            "run",
            str(self.plan_dir),
            "--cwd",
            str(self.repo_root),
            "--harness",
            "codex",
            "--state-dir",
            str(self.state_dir),
            "--json",
        ]
        if allow_not_loop_ready:
            argv.append("--allow-not-loop-ready")
        if max_sessions is not None:
            argv.extend(["--max-sessions", str(max_sessions)])
        if max_pending_sessions is not None:
            argv.extend(["--max-pending-sessions", str(max_pending_sessions)])
        if turn_timeout is not None:
            argv.extend(["--timeout", str(turn_timeout)])
        env = os.environ.copy()
        env.update(
            {
                "XDG_CONFIG_HOME": str(self.config_home),
                "FAKE_CODEX_MODE": mode,
                "FAKE_CODEX_SLEEP": "5",
                "PYTHONPATH": str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", ""),
            }
        )
        env.update(env_overrides or {})
        completed = subprocess.run(
            argv,
            cwd=REPO_ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=process_timeout,
            check=False,
        )
        return completed, json.loads(completed.stdout)

    def _add_origin(self, repo: Path, origin: str) -> None:
        subprocess.run(
            ["git", "remote", "add", "origin", origin],
            cwd=repo,
            text=True,
            capture_output=True,
            check=True,
        )

    def _plan_dir_bytes(self) -> dict[str, bytes]:
        return {
            str(path.relative_to(self.plan_dir)): path.read_bytes()
            for path in sorted(self.plan_dir.rglob("*"))
            if path.is_file()
        }

    def _write_cross_repo_plan(self, statuses: tuple[str, ...] = ("pending",)) -> Path:
        """A legacy plan whose session 1 check lives in a second repository."""
        other_repo = self.tmp_path / "other"
        init_git_repo(other_repo)
        contracts_by_id = {
            "1": _contract_block(
                json.dumps(
                    {
                        "type": "file_exists",
                        "path": str(other_repo / "README.md"),
                        "name": "cross-repo",
                    }
                ),
                session="1",
            )
        }
        for index in range(2, len(statuses) + 1):
            contracts_by_id[str(index)] = _contract_block(
                json.dumps(
                    {
                        "type": "file_exists",
                        "path": "README.md",
                        "name": f"readme-{index}",
                    }
                ),
                session=str(index),
            )
        self._write_plan(statuses, contracts_by_id=contracts_by_id)
        return other_repo

    def test_run_auto_repairs_when_readiness_metadata_missing(self) -> None:
        self._write_plan(("pending",), readiness=None)

        completed, payload = self._run()

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])
        repair_payload = payload["readiness_gate_repair"]
        self.assertEqual(["manifest-readiness"], repair_payload["attempted"])
        self.assertTrue(repair_payload["ok"])
        self.assertEqual([], repair_payload["errors"])
        self.assertEqual(1, len(repair_payload["auto_repairs"]))
        repair = repair_payload["auto_repairs"][0]
        self.assertEqual("rewrite", repair["action"])
        self.assertEqual("manifest-readiness", repair["category"])
        self.assertEqual(str((self.plan_dir / "manifest.md").resolve()), repair["path"])
        self.assertEqual(0, repair["entries_written"])
        self.assertEqual([], repair["completed_session_ids"])
        self.assertFalse(repair["journal_replaced"])
        # Readiness lifecycle picks up from the repaired metadata.
        self.assertEqual("complete", payload["readiness_state_final"])
        metadata = plans.read_readiness_metadata(self.plan_dir)
        self.assertIsNotNone(metadata)
        self.assertEqual(
            plans.stable_readiness_sha256(self.plan_dir),
            metadata.readiness_sha256,
        )
        self.assertEqual(
            str(plans.canonicalize_path(self.repo_root)),
            plans.load_plan_metadata(self.plan_dir).cwd,
        )

    def test_run_auto_repairs_when_state_is_reviewed(self) -> None:
        self._write_plan(("pending",), readiness="reviewed")

        completed, payload = self._run()

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])
        self.assertEqual("reviewed", payload["readiness_state_initial"])
        repair_payload = payload["readiness_gate_repair"]
        self.assertEqual(["manifest-readiness"], repair_payload["attempted"])
        self.assertTrue(repair_payload["ok"])
        # Metadata was rewritten to ``loop-ready`` and then lifecycled to
        # ``complete`` once the only pending session finished.
        self.assertEqual("complete", plans.read_readiness_metadata(self.plan_dir).state)

    def test_run_auto_repairs_when_state_is_complete_on_pending_plan(self) -> None:
        self._write_plan(("pending",), readiness="complete")

        completed, payload = self._run()

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])
        self.assertEqual("complete", payload["readiness_state_initial"])
        repair_payload = payload["readiness_gate_repair"]
        self.assertEqual(["manifest-readiness"], repair_payload["attempted"])
        self.assertTrue(repair_payload["ok"])
        self.assertEqual([], repair_payload["errors"])
        self.assertEqual(1, len(repair_payload["auto_repairs"]))
        repair = repair_payload["auto_repairs"][0]
        self.assertEqual("manifest-readiness", repair["category"])
        self.assertEqual("loop-ready", repair["target_state"])
        self.assertEqual("complete", plans.read_readiness_metadata(self.plan_dir).state)

    def test_run_auto_repairs_when_state_is_running_on_pending_plan(self) -> None:
        self._write_plan(("pending",), readiness="running")

        completed, payload = self._run()

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])
        self.assertEqual("running", payload["readiness_state_initial"])
        repair_payload = payload["readiness_gate_repair"]
        self.assertEqual(["manifest-readiness"], repair_payload["attempted"])
        self.assertTrue(repair_payload["ok"])
        self.assertEqual([], repair_payload["errors"])
        self.assertEqual(1, len(repair_payload["auto_repairs"]))
        repair = repair_payload["auto_repairs"][0]
        self.assertEqual("manifest-readiness", repair["category"])
        self.assertEqual("loop-ready", repair["target_state"])
        self.assertEqual("complete", plans.read_readiness_metadata(self.plan_dir).state)

    def test_run_auto_repairs_when_recorded_sha_does_not_match(self) -> None:
        self._write_plan(
            ("pending",),
            readiness="loop-ready",
            sha_override="0" * 64,
        )

        completed, payload = self._run()

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])
        repair_payload = payload["readiness_gate_repair"]
        self.assertEqual(["manifest-readiness"], repair_payload["attempted"])
        self.assertTrue(repair_payload["ok"])
        metadata = plans.read_readiness_metadata(self.plan_dir)
        self.assertIsNotNone(metadata)
        self.assertNotEqual("0" * 64, metadata.readiness_sha256)
        self.assertEqual(
            plans.stable_readiness_sha256(self.plan_dir),
            metadata.readiness_sha256,
        )

    def _preflight_skipped_events(self) -> list[dict[str, Any]]:
        event_path = (
            state.plan_state_dir(self.plan_dir, self.state_dir)
            / events.EVENTS_FILE_NAME
        )
        return [
            record
            for record in read_jsonl(event_path)
            if record["name"] == events.PREFLIGHT_SKIPPED_EVENT
        ]

    def test_loop_ready_start_skips_core_preflight(self) -> None:
        self._write_plan(("pending",), readiness="loop-ready")
        expected_sha = plans.stable_readiness_sha256(self.plan_dir)

        completed, payload = self._run()

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["readiness_gate"]["core_skipped"])
        self.assertNotIn("readiness_gate_repair", payload)
        skipped = self._preflight_skipped_events()
        self.assertEqual(1, len(skipped))
        self.assertEqual(
            {"readiness_state": "loop-ready", "readiness_sha256": expected_sha},
            skipped[0]["payload"],
        )
        # Command runnability probes only run on the full preflight path.
        self.assertEqual(
            [],
            [
                finding
                for finding in payload["readiness_gate"]["findings"]
                if finding["category"] == "command-not-runnable"
            ],
        )

    _UNRUNNABLE_EXECUTABLE = "asl-missing-executable-for-probe-regression"

    def _write_plan_with_unrunnable_probe(
        self, *, readiness: str, sha_override: str | None = None
    ) -> None:
        """Write a 2-session plan whose second contract fails command probes.

        Only the full preflight probes command runnability, and the run never
        reaches session 2, so the finding appears if and only if the core
        preflight ran.
        """
        self._write_plan(
            ("pending", "pending"),
            readiness=readiness,
            sha_override=sha_override,
            contracts_by_id={
                "1": _contract_block(
                    '{"type": "file_exists", "path": "README.md", "name": "readme-1"}',
                    session="1",
                ),
                "2": _contract_block(
                    '{"type": "command", "argv": '
                    f'["{self._UNRUNNABLE_EXECUTABLE}"], "name": "probe-2"}}',
                    session="2",
                ),
            },
        )

    def _command_runnability_findings(
        self, payload: dict[str, Any]
    ) -> list[dict[str, Any]]:
        return [
            finding
            for finding in payload["readiness_gate"]["findings"]
            if finding["category"] == "command-not-runnable"
        ]

    def test_running_readiness_start_skips_core_preflight(self) -> None:
        self._write_plan_with_unrunnable_probe(readiness="running")
        expected_sha = plans.stable_readiness_sha256(self.plan_dir)

        completed, payload = self._run(max_sessions=1)

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual("running", payload["readiness_state_initial"])
        skipped = self._preflight_skipped_events()
        self.assertEqual(1, len(skipped))
        self.assertEqual(
            {"readiness_state": "running", "readiness_sha256": expected_sha},
            skipped[0]["payload"],
        )
        # `running` still fails the strict gate, so the readiness metadata is
        # normalized -- but the repair rerun stays on the reduced path.
        repair_payload = payload["readiness_gate_repair"]
        self.assertEqual(["manifest-readiness"], repair_payload["attempted"])
        self.assertTrue(repair_payload["ok"])
        self.assertTrue(repair_payload["core_skipped"])
        self.assertTrue(payload["readiness_gate"]["core_skipped"])
        self.assertEqual([], self._command_runnability_findings(payload))

    def test_running_readiness_with_stale_sha_probes_commands(self) -> None:
        self._write_plan_with_unrunnable_probe(
            readiness="running", sha_override="0" * 64
        )

        completed, payload = self._run(max_sessions=1)

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertEqual([], self._preflight_skipped_events())
        self.assertNotIn("core_skipped", payload["readiness_gate"])
        self.assertEqual("not-loop-ready", payload["reason"])
        findings = self._command_runnability_findings(payload)
        self.assertEqual(1, len(findings))
        self.assertEqual("red", findings[0]["severity"])
        self.assertEqual("2", findings[0]["session_id"])

    def test_session_edited_after_loop_ready_runs_full_preflight(self) -> None:
        self._write_plan(("pending",), readiness="loop-ready")
        session_path = self.plan_dir / "session-1.md"
        session_path.write_text(
            session_path.read_text(encoding="utf-8").replace(
                "Instructions.", "Instructions. Edited while paused."
            ),
            encoding="utf-8",
        )

        completed, payload = self._run()

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual([], self._preflight_skipped_events())
        self.assertNotIn("core_skipped", payload["readiness_gate"])
        self.assertEqual(
            ["manifest-readiness"],
            payload["readiness_gate_repair"]["attempted"],
        )

    def test_allow_not_loop_ready_lets_run_proceed_and_reports_findings(
        self,
    ) -> None:
        self._write_plan(("pending",), readiness=None)

        completed, payload = self._run(allow_not_loop_ready=True)

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])
        self.assertTrue(payload["allow_not_loop_ready"])
        gate = payload["readiness_gate"]
        self.assertFalse(gate["ok"])
        self.assertIsNone(gate["readiness_state"])
        self.assertTrue(
            any(
                finding["category"] == "manifest-readiness"
                for finding in gate["findings"]
            )
        )
        # No readiness existed, so the lifecycle never engages.
        self.assertIsNone(payload["readiness_state_initial"])
        self.assertIsNone(payload["readiness_state_final"])
        self.assertIsNone(plans.read_readiness_metadata(self.plan_dir))

    def test_allow_not_loop_ready_does_not_bypass_repository_footprint(
        self,
    ) -> None:
        # A target above the bound root but inside the same repository escapes
        # without adding a second repository, so the readiness gate — not the
        # hard intent gate — is what has to refuse it. A target owned by no
        # repository at all is released deliberately: no registry row could
        # describe it.
        escaping = self.repo_root / "README.md"
        self.repo_root = self.repo_root / "src"
        self.repo_root.mkdir()
        self._write_plan(
            ("pending",),
            contracts_by_id={
                "1": _contract_block(
                    json.dumps(
                        {
                            "type": "file_exists",
                            "path": str(escaping),
                            "name": "out-of-root",
                        }
                    ),
                    session="1",
                )
            },
        )

        completed, payload = self._run(allow_not_loop_ready=True)

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertTrue(payload["paused"])
        self.assertEqual("not-loop-ready", payload["pause_reason"])
        self.assertEqual(
            ["repository-footprint"],
            payload["pause_details"]["blocker"],
        )
        self.assertEqual([], payload["turns"])

    def test_allow_not_loop_ready_does_not_bypass_repository_intent(
        self,
    ) -> None:
        other_repo = self.tmp_path / "other"
        init_git_repo(other_repo)
        self._write_plan(
            ("pending",),
            contracts_by_id={
                "1": _contract_block(
                    json.dumps(
                        {
                            "type": "file_exists",
                            "path": str(other_repo / "README.md"),
                            "name": "cross-repo",
                        }
                    ),
                    session="1",
                )
            },
        )

        completed, payload = self._run(allow_not_loop_ready=True)

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertTrue(payload["paused"])
        self.assertEqual(
            events.PAUSE_REASON_REPOSITORY_REGISTRY_REQUIRED,
            payload["pause_reason"],
        )
        report = payload["pause_details"]["repository_intent"]
        self.assertEqual(2, report["git_root_count"])
        self.assertEqual(
            sorted(
                str(plans.canonicalize_path(path))
                for path in (self.repo_root, other_repo)
            ),
            sorted(report["git_roots"]),
        )
        self.assertEqual([], payload["turns"])
        self.assertFalse(payload["pause_policy"]["recoverable"])

    def test_hard_gate_rejects_sole_git_target_outside_non_git_root(self) -> None:
        target_repository = self.tmp_path / "target"
        init_git_repo(target_repository)
        self.repo_root = self.tmp_path / "operator-root"
        self.repo_root.mkdir()
        self._write_plan(
            ("pending",),
            contracts_by_id={
                "1": _contract_block(
                    json.dumps(
                        {
                            "type": "file_exists",
                            "path": str(target_repository / "README.md"),
                            "name": "outside-bound-root",
                        }
                    ),
                    session="1",
                )
            },
        )

        completed, payload = self._run()

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertEqual(
            events.PAUSE_REASON_REPOSITORY_REGISTRY_REQUIRED,
            payload["pause_reason"],
        )
        self.assertEqual([], payload["turns"])
        report = payload["pause_details"]["repository_intent"]
        self.assertIsNone(report["bound_git_root"])
        self.assertEqual(1, report["git_root_count"])
        self.assertEqual(
            [str(plans.canonicalize_path(target_repository))],
            report["git_roots"],
        )

    def test_hard_gate_applies_the_registry_before_the_first_session(
        self,
    ) -> None:
        self._add_origin(self.repo_root, "git@github.com:tundra/repo.git")
        other_repo = self._write_cross_repo_plan()
        self._add_origin(other_repo, "git@github.com:tundra/other.git")

        completed, payload = self._run()

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertTrue(payload["paused"])
        self.assertEqual(
            events.PAUSE_REASON_REPOSITORY_REGISTRY_REQUIRED,
            payload["pause_reason"],
        )
        self.assertEqual([], payload["turns"])
        registration = payload["pause_details"]["repository_registration"]
        self.assertEqual("applied", registration["status"])
        self.assertEqual(
            str(plans.canonicalize_path(self.tmp_path)),
            registration["workspace_root"],
        )
        self.assertEqual(2, len(registration["repositories"]))
        repairs = payload["pause_details"]["auto_repairs"]
        self.assertEqual(1, len(repairs))
        self.assertEqual("repository-registry", repairs[0]["action"])
        self.assertEqual("repository-registry-required", repairs[0]["category"])
        self.assertEqual(
            str(plans.canonicalize_path(self.plan_dir)), repairs[0]["path"]
        )
        self.assertEqual(len(registration["files"]), repairs[0]["entries_written"])
        # The files are migrated, so only the rebind and the re-run are left.
        manifest = (self.plan_dir / "manifest.md").read_text(encoding="utf-8")
        self.assertIn("## Repositories", manifest)
        session = (self.plan_dir / "session-1.md").read_text(encoding="utf-8")
        self.assertIn("**Writes repositories**:", session)
        self.assertEqual(2, len(registration["next_commands"]))
        for command in registration["next_commands"]:
            self.assertIn(command, payload["pause_message"])

    def test_hard_gate_refuses_to_migrate_a_completed_legacy_handoff(
        self,
    ) -> None:
        # A registry makes a completed handoff's unqualified `Files modified`
        # entries illegal, so the plan would stop parsing entirely. The gate
        # delegates that judgement to the migration itself, which refuses.
        self._add_origin(self.repo_root, "git@github.com:tundra/repo.git")
        other_repo = self._write_cross_repo_plan(("complete", "pending"))
        self._add_origin(other_repo, "git@github.com:tundra/other.git")
        session = self.plan_dir / "session-1.md"
        session.write_text(
            session.read_text(encoding="utf-8").replace(
                "- **Files modified**: (none)",
                "- **Files modified**: src/new.py",
            ),
            encoding="utf-8",
        )
        before = self._plan_dir_bytes()

        completed, payload = self._run()

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertTrue(payload["paused"])
        self.assertEqual(
            events.PAUSE_REASON_REPOSITORY_REGISTRY_REQUIRED,
            payload["pause_reason"],
        )
        self.assertEqual([], payload["turns"])
        registration = payload["pause_details"]["repository_registration"]
        self.assertEqual("refused", registration["status"])
        self.assertEqual("completed-handoff-paths", registration["missing_fact"])
        self.assertIn("session-1.md", registration["detail"])
        self.assertNotIn("auto_repairs", payload["pause_details"])
        self.assertIn("completed-handoff-paths", payload["pause_message"])
        self.assertEqual(before, self._plan_dir_bytes())

    def test_hard_gate_refuses_to_migrate_a_repository_without_an_origin(
        self,
    ) -> None:
        self._write_cross_repo_plan()
        before = self._plan_dir_bytes()

        completed, payload = self._run()

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertTrue(payload["paused"])
        self.assertEqual(
            events.PAUSE_REASON_REPOSITORY_REGISTRY_REQUIRED,
            payload["pause_reason"],
        )
        self.assertEqual([], payload["turns"])
        registration = payload["pause_details"]["repository_registration"]
        self.assertEqual("refused", registration["status"])
        self.assertEqual("repository-origin", registration["missing_fact"])
        self.assertNotIn("auto_repairs", payload["pause_details"])
        self.assertIn("repository-origin", payload["pause_message"])
        self.assertEqual(before, self._plan_dir_bytes())

    def test_hard_gate_refuses_a_loop_ready_plan_that_skips_preflight(
        self,
    ) -> None:
        # A loop-ready plan short-circuits core preflight, so the readiness
        # gate never re-derives the verdict. The run-level gate must.
        other_repo = self.tmp_path / "other"
        init_git_repo(other_repo)
        self._write_plan(("pending",))
        session = self.plan_dir / "session-1.md"
        session.write_text(
            session.read_text(encoding="utf-8").replace(
                "Instructions.",
                f"Instructions. Write the helper into {other_repo}/src/new.py.",
            ),
            encoding="utf-8",
        )
        sha = plans.stable_readiness_sha256(self.plan_dir)
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=sha,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )

        completed, payload = self._run()

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertEqual(
            events.PAUSE_REASON_REPOSITORY_REGISTRY_REQUIRED,
            payload["pause_reason"],
        )
        self.assertEqual([], payload["turns"])
        self.assertEqual(
            2, payload["pause_details"]["repository_intent"]["git_root_count"]
        )

    def test_hard_gate_exempts_the_runner_own_plan_and_state_trees(self) -> None:
        # An operator may keep the plan and the state root inside a checkout
        # unrelated to the work. That is ASL's bookkeeping, not a second
        # repository the run intends to reach.
        other_repo = self.tmp_path / "other"
        init_git_repo(other_repo)
        self.plan_dir = other_repo / "plans" / "current"
        self.state_dir = other_repo / "state"
        self._write_plan(("pending",))

        completed, payload = self._run()

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertFalse(payload["paused"])
        self.assertEqual("all-complete", payload["reason"])

    def test_task_packet_recheck_catches_a_redirect_only_expansion_notes_carry(
        self,
    ) -> None:
        # Expansion notes are outside the plan-level scan, so the packet
        # recheck is the only surface that sees this redirect.
        other_repo = self.tmp_path / "other"
        init_git_repo(other_repo)
        self._write_plan(("pending",))
        (self.plan_dir / "session-1.expand.md").write_text(
            f"# Session 1 Expansion\n\n- Corrected location: {other_repo}/src/new.py\n",
            encoding="utf-8",
        )

        completed, payload = self._run()

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertEqual(
            events.PAUSE_REASON_REPOSITORY_REGISTRY_REQUIRED,
            payload["pause_reason"],
        )
        self.assertEqual([], payload["turns"])
        report = payload["pause_details"]["repository_intent"]
        self.assertEqual(2, report["git_root_count"])
        self.assertEqual(1, payload["pause_details"]["session"])

    def _declare_source(self, source: Path, *, expansion_cites: str) -> None:
        """Point the manifest at ``source`` and cite a path in an expansion note.

        Readiness is rewritten last: both edits change the artifact digests the
        readiness SHA covers.
        """
        manifest = self.plan_dir / "manifest.md"
        manifest.write_text(
            manifest.read_text(encoding="utf-8") + f"\n**Split from**: `{source}`\n",
            encoding="utf-8",
        )
        (self.plan_dir / "session-1.expand.md").write_text(
            f"# Session 1 Expansion\n\n- Restated from {expansion_cites}\n",
            encoding="utf-8",
        )
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=plans.stable_readiness_sha256(self.plan_dir),
                checked_at="2026-06-19T00:00:00Z",
            ),
        )

    def test_hard_gate_exempts_the_document_the_plan_was_split_from(self) -> None:
        # The reported bug end to end: a plan citing where it came from, in
        # prose an `asl expand-ingest` regenerates, is not a plan that reaches
        # two repositories.
        other_repo = self.tmp_path / "other"
        init_git_repo(other_repo)
        source = other_repo / "plans" / "source.md"
        source.parent.mkdir(parents=True)
        source.write_text("# Source\n", encoding="utf-8")
        self._write_plan(("pending",))
        self._declare_source(source, expansion_cites=f"{source}:209")

        completed, payload = self._run()

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertFalse(payload["paused"])
        self.assertEqual("all-complete", payload["reason"])

    def test_hard_gate_still_pauses_on_a_redirect_beside_the_source_document(
        self,
    ) -> None:
        # The sharp pin: the exemption covers the source document and the
        # directory holding it, never the rest of that repository.
        other_repo = self.tmp_path / "other"
        init_git_repo(other_repo)
        source = other_repo / "plans" / "source.md"
        source.parent.mkdir(parents=True)
        source.write_text("# Source\n", encoding="utf-8")
        self._write_plan(("pending",))
        self._declare_source(source, expansion_cites=f"{other_repo}/src/new.py")

        completed, payload = self._run()

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertEqual(
            events.PAUSE_REASON_REPOSITORY_REGISTRY_REQUIRED,
            payload["pause_reason"],
        )
        report = payload["pause_details"]["repository_intent"]
        self.assertEqual(2, report["git_root_count"])

    def test_run_rejects_mismatched_prepared_root_without_overwriting_binding(
        self,
    ) -> None:
        self._write_plan(("pending",), readiness="loop-ready")
        previous = self.tmp_path / "previous-repo"
        previous.mkdir()
        plans.save_plan_metadata(self.plan_dir, previous)

        completed, payload = self._run()

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertFalse(payload["ok"])
        self.assertEqual("workspace-error", payload["reason"])
        metadata = plans.read_readiness_metadata(self.plan_dir)
        self.assertEqual("loop-ready", metadata.state)
        self.assertEqual(
            plans.stable_readiness_sha256(self.plan_dir),
            metadata.readiness_sha256,
        )
        self.assertEqual(
            str(plans.canonicalize_path(previous)),
            plans.load_plan_metadata(self.plan_dir).cwd,
        )

    def test_lifecycle_returns_to_loop_ready_when_max_sessions_leaves_pending(
        self,
    ) -> None:
        self._write_plan(("pending", "pending"), readiness="loop-ready")

        completed, payload = self._run(max_sessions=1)

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual("loop-ready", payload["readiness_state_initial"])
        self.assertEqual("loop-ready", payload["readiness_state_final"])
        metadata = plans.read_readiness_metadata(self.plan_dir)
        self.assertEqual("loop-ready", metadata.state)
        self.assertEqual(
            plans.stable_readiness_sha256(self.plan_dir),
            metadata.readiness_sha256,
        )

    def test_run_uses_max_pending_sessions_override_for_readiness_gate(
        self,
    ) -> None:
        pending_count = preflight.TURN_BUDGET_YELLOW_THRESHOLD + 5
        self._write_plan(tuple(["pending"] * pending_count), readiness="loop-ready")

        completed, payload = self._run(
            max_sessions=1,
            max_pending_sessions=pending_count,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual("max-sessions", payload["reason"])
        self.assertEqual(pending_count, payload["max_pending_sessions"])
        gate = payload["readiness_gate"]
        self.assertTrue(gate["ok"])
        budget_findings = [
            finding
            for finding in gate["findings"]
            if finding["category"] == "env-turn-budget"
        ]
        self.assertEqual(1, len(budget_findings))
        self.assertEqual("green", budget_findings[0]["severity"])

    def test_note_written_into_another_sessions_packet_pauses_the_run(self) -> None:
        self._write_plan(("pending", "pending"), readiness="loop-ready")

        completed, payload = self._run(mode="success-future-note")

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["paused"])
        self.assertEqual(
            events.PAUSE_REASON_PLAN_ARTIFACT_TAMPERED, payload["pause_reason"]
        )
        guard = payload["turns"][0]["plan_artifact_guard"]
        self.assertFalse(guard["passed"])
        self.assertEqual(["session-2.md"], guard["changed_artifacts"])
        self.assertTrue(guard["reverted_to_pending"])
        self.assertEqual(guard, payload["pause_details"]["plan_artifact_guard"])
        # The turn is refused whole, so the session it did select goes back to
        # pending rather than banking a completion earned against a plan it
        # rewrote underneath itself.
        self.assertEqual(
            "pending",
            plans.inspect_plan(self.plan_dir).status_by_id["1"],
        )

    def test_timeout_that_tampers_with_another_packet_pauses_on_the_guard(self) -> None:
        self._write_plan(("pending", "pending"), readiness="loop-ready")

        completed, payload = self._run(
            mode="timeout",
            # The deadline runs from the harness spawn, so it has to cover the
            # fake harness reaching its tamper: a freshly copied executable can
            # stall for seconds in Gatekeeper assessment on top of full-suite
            # parallel load, and a deadline that lands first leaves nothing for
            # the guard to see. The sleep stays far beyond the deadline so the
            # turn still times out.
            turn_timeout=30,
            env_overrides={
                "FAKE_CODEX_SLEEP": "120",
                "FAKE_CODEX_TAMPER_SESSION": "session-2.md",
            },
            process_timeout=90,
        )

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["turns"][0]["timed_out"])
        # A bare timeout is a recoverable pause, so reporting one here would
        # relaunch against the packet the timed-out turn rewrote.
        self.assertEqual(
            events.PAUSE_REASON_PLAN_ARTIFACT_TAMPERED, payload["pause_reason"]
        )
        self.assertFalse(payload["pause_policy"]["recoverable"])
        guard = payload["turns"][0]["plan_artifact_guard"]
        self.assertFalse(guard["passed"])
        self.assertEqual(["session-2.md"], guard["changed_artifacts"])
        self.assertEqual("timeout", guard["result_disposition"])
        self.assertEqual(guard, payload["pause_details"]["plan_artifact_guard"])

    def test_structured_pause_that_tampers_with_another_packet_hits_the_guard(
        self,
    ) -> None:
        self._write_plan(("pending", "pending"), readiness="loop-ready")

        completed, payload = self._run(
            mode="runner-pause",
            env_overrides={"FAKE_CODEX_TAMPER_SESSION": "session-2.md"},
        )

        self.assertEqual(1, completed.returncode)
        self.assertEqual(
            events.PAUSE_REASON_PLAN_ARTIFACT_TAMPERED, payload["pause_reason"]
        )
        self.assertFalse(payload["pause_policy"]["recoverable"])
        guard = payload["turns"][0]["plan_artifact_guard"]
        self.assertFalse(guard["passed"])
        self.assertEqual(["session-2.md"], guard["changed_artifacts"])
        self.assertEqual("runner-pause", guard["result_disposition"])
        self.assertEqual(guard, payload["pause_details"]["plan_artifact_guard"])

    def test_wrong_session_completion_keeps_its_status_diagnosis(self) -> None:
        self._write_plan(("pending", "pending"), readiness="loop-ready")

        completed, payload = self._run(mode="unexpected-progress")

        # Completing the wrong session also changes that session's artifact,
        # so the ordering below the status guards is what keeps this sharper
        # than a bare tamper report.
        self.assertEqual(1, completed.returncode)
        self.assertEqual("unexpected-status-change", payload["pause_reason"])
        self.assertNotIn("plan_artifact_guard", payload["turns"][0])

    def test_selected_session_completion_passes_the_plan_artifact_guard(self) -> None:
        self._write_plan(("pending", "pending"), readiness="loop-ready")

        completed, payload = self._run(max_sessions=1)

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        guard = payload["turns"][0]["plan_artifact_guard"]
        self.assertTrue(guard["passed"])
        self.assertEqual(1, guard["artifact_count"])

    def test_lifecycle_keeps_the_running_sha_in_sync_across_a_turn(self) -> None:
        self._write_plan(("pending", "pending"), readiness="loop-ready")

        completed, payload = self._run(max_sessions=1)

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual("max-sessions", payload["reason"])
        self.assertEqual("loop-ready", payload["readiness_state_final"])
        self.assertIsNone(payload.get("readiness_state_refresh_error"))
        self.assertIsNotNone(payload.get("readiness_state_refreshed_at"))
        refreshed_metadata = plans.read_readiness_metadata(self.plan_dir)
        self.assertEqual("loop-ready", refreshed_metadata.state)
        self.assertEqual(
            plans.stable_readiness_sha256(self.plan_dir),
            refreshed_metadata.readiness_sha256,
        )

    def test_lifecycle_marks_reviewed_when_run_pauses_on_failure(self) -> None:
        self._write_plan(("pending",), readiness="loop-ready")

        completed, payload = self._run(mode="failure")

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["paused"])
        self.assertEqual("loop-ready", payload["readiness_state_initial"])
        self.assertEqual("reviewed", payload["readiness_state_final"])
        metadata = plans.read_readiness_metadata(self.plan_dir)
        self.assertEqual("reviewed", metadata.state)

    def test_run_auto_repairs_missing_progress_journal_and_proceeds(self) -> None:
        self._write_plan(("complete", "pending"), readiness="loop-ready")
        progress_file = plans.progress_path(self.plan_dir)
        self.assertFalse(progress_file.exists())

        completed, payload = self._run()

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])
        self.assertEqual("all-complete", payload["reason"])

        # The readiness SHA matches, so only the unhashed-state check keeps the
        # loop-ready start from skipping the core preflight past this finding.
        self.assertEqual([], self._preflight_skipped_events())
        self.assertNotIn("core_skipped", payload["readiness_gate"])

        repair_payload = payload["readiness_gate_repair"]
        self.assertEqual(["progress-journal-missing"], repair_payload["attempted"])
        self.assertEqual([], repair_payload["errors"])
        self.assertTrue(repair_payload["ok"])
        self.assertEqual(1, len(repair_payload["auto_repairs"]))
        repair = repair_payload["auto_repairs"][0]
        self.assertEqual("rewrite", repair["action"])
        self.assertEqual("progress-journal-missing", repair["category"])
        self.assertEqual(str(progress_file), repair["path"])
        self.assertEqual(1, repair["entries_written"])
        self.assertEqual(["1"], repair["completed_session_ids"])
        self.assertFalse(repair["journal_replaced"])

        self.assertTrue(payload["readiness_gate"]["ok"])
        self.assertTrue(progress_file.exists())
        entries = plans.read_progress_entries(self.plan_dir)
        self.assertEqual(["1", "2"], [entry.session_id.value for entry in entries])

        event_path = (
            state.plan_state_dir(self.plan_dir, self.state_dir)
            / events.EVENTS_FILE_NAME
        )
        repair_events = [
            record
            for record in read_jsonl(event_path)
            if record["name"] == events.PREFLIGHT_AUTO_REPAIRED_EVENT
        ]
        self.assertEqual(1, len(repair_events))
        self.assertEqual(
            "progress-journal-missing", repair_events[0]["payload"]["category"]
        )

    def test_run_auto_repairs_progress_and_readiness_together(self) -> None:
        self._write_plan(("complete", "pending"), readiness=None)

        completed, payload = self._run()

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])

        repair_payload = payload["readiness_gate_repair"]
        self.assertEqual(
            ["progress-journal-missing", "manifest-readiness"],
            repair_payload["attempted"],
        )
        self.assertEqual([], repair_payload["errors"])
        self.assertTrue(repair_payload["ok"])
        categories = [repair["category"] for repair in repair_payload["auto_repairs"]]
        self.assertEqual(["progress-journal-missing", "manifest-readiness"], categories)
        self.assertTrue(plans.progress_path(self.plan_dir).exists())
        metadata = plans.read_readiness_metadata(self.plan_dir)
        self.assertIsNotNone(metadata)

    def test_allow_not_loop_ready_skips_auto_repair(self) -> None:
        self._write_plan(("pending",), readiness=None)

        completed, payload = self._run(allow_not_loop_ready=True)

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        # The bypass path never runs auto-repair, so the field is absent.
        self.assertNotIn("readiness_gate_repair", payload)
        # Readiness metadata was not synthesised on behalf of the bypass.
        self.assertIsNone(plans.read_readiness_metadata(self.plan_dir))

    def test_run_pauses_when_non_repairable_finding_remains(self) -> None:
        self._write_plan(("complete", "pending"), readiness="loop-ready")
        # Inject a yellow ambiguity sentinel so progress-journal-missing is not
        # the only non-readiness blocker. Refresh readiness SHA so the
        # manifest-readiness drift does not also fire.
        session_path = self.plan_dir / "session-2.md"
        text = session_path.read_text(encoding="utf-8")
        session_path.write_text(
            text.replace("Instructions.", "Instructions. TODO: revisit later"),
            encoding="utf-8",
        )
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=plans.stable_readiness_sha256(self.plan_dir),
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        progress_file = plans.progress_path(self.plan_dir)
        self.assertFalse(progress_file.exists())

        completed, payload = self._run()

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["paused"])
        self.assertEqual("not-loop-ready", payload["pause_reason"])

        repair_payload = payload["readiness_gate_repair"]
        # Progress repair was attempted (it is always attempted when the
        # category is present), but manifest-readiness was not (other blocker
        # remained after the progress repair).
        self.assertEqual(["progress-journal-missing"], repair_payload["attempted"])
        self.assertEqual([], repair_payload["errors"])
        self.assertFalse(repair_payload["ok"])
        self.assertEqual(1, len(repair_payload["auto_repairs"]))

        # The journal really was rebuilt even though the run paused.
        self.assertTrue(progress_file.exists())
        # The final gate payload still surfaces the unresolved sentinel.
        gate_findings = payload["readiness_gate"]["findings"]
        categories = {finding["category"] for finding in gate_findings}
        self.assertIn("ambiguity-sentinel", categories)
        self.assertNotIn("progress-journal-missing", categories)

    def _write_baseline_gated_plan(self) -> None:
        contract = _command_contract('["true"]', session="1", expectation="pass")
        write_plan(self.plan_dir, ("pending",), contracts_by_id={"1": contract})
        sha = plans.stable_readiness_sha256(self.plan_dir)
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=sha,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )

    def test_run_blocks_when_baseline_evidence_missing(self) -> None:
        self._write_baseline_gated_plan()

        completed, payload = self._run()

        self.assertEqual(1, completed.returncode)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["paused"])
        self.assertEqual("not-loop-ready", payload["pause_reason"])
        categories = {
            finding["category"] for finding in payload["readiness_gate"]["findings"]
        }
        self.assertIn("baseline-missing", categories)
        # Baseline evidence is outside the readiness SHA, so the loop-ready
        # start must not skip the core preflight past this finding.
        self.assertEqual([], self._preflight_skipped_events())
        self.assertNotIn("core_skipped", payload["readiness_gate"])

    def test_run_proceeds_when_baseline_evidence_present(self) -> None:
        self._write_baseline_gated_plan()
        # Record current baseline evidence before the run so the gate is
        # satisfied (the check passes and matches its expected-pass declaration).
        baseline.run_baseline(self.plan_dir, self.repo_root)

        completed, payload = self._run()

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])
        categories = {
            finding["category"] for finding in payload["readiness_gate"]["findings"]
        }
        self.assertNotIn("baseline-missing", categories)


#: The modules the session loop hands a step of the turn's finalization to. A
#: step that moved into one of these still runs where the loop calls it, so the
#: checks below read the step from the module and its position from that call.
#: Session 34 moved the gated plan-artifact guard into `turn_postflight`; this
#: list is what keeps the checks pointing at the guard through that move and
#: the next one.
DISPOSITION_DELEGATES = (turn_postflight,)


@dataclasses.dataclass(frozen=True)
class GuardSite:
    """Where the session loop has run the gated plan-artifact guard.

    ``loop_lineno``, ``call_end_lineno``, and ``loop_end_lineno`` are runner
    lines: the statement that reaches the guard, where that statement ends, and
    the last line of the delegation that carries the verdict back into the loop.
    ``owner`` is the function holding the guard when
    it no longer lives in the loop, ``owner_lineno`` the guard's line inside it,
    and ``result_name`` what the loop binds the delegate's verdict to -- which is
    how the stop the delegation propagates is told from an unguarded edge.
    """

    loop_lineno: int
    call_end_lineno: int
    loop_end_lineno: int
    col_offset: int
    owner: ast.FunctionDef | None
    owner_lineno: int
    result_name: str | None


class TerminatingDispositionSourceTests(unittest.TestCase):
    """The plan-artifact guard cannot be skipped by a new terminating branch.

    The guard runs once, gated on the disposition, ahead of every branch that
    can end the turn. These tests hold the dispatch to that disposition so a
    branch added later is either named there — and therefore guarded — or is
    unreachable.

    The guard and the dispatch no longer share a source file: the guard runs
    inside a delegate the loop calls, and only its verdict comes back. So the
    ordering is read across the two, in the loop's coordinates — a step the loop
    delegates sits where the call sits — and the delegation's own edges are held
    to propagating the delegate's stop and nothing else.
    """

    DISPOSITION_NAMES = (
        "interrupted",
        "timeout",
        "aborted",
        "malformed-plan",
        "runner-pause",
        "nonzero-exit",
    )

    RAW_CONDITIONS = (
        "result.interrupted",
        "result.timed_out",
        "result.aborted",
        "result.returncode",
        "after is None",
    )

    # A break or continue inside one of these binds to that construct, not to
    # the session loop, so it cannot end the turn.
    INNER_LOOPS = (
        ast.For,
        ast.AsyncFor,
        ast.While,
    )

    # Every edge inside one of these — including a return — belongs to that
    # callable rather than to run_locked.
    INNER_CALLABLES = (
        ast.FunctionDef,
        ast.AsyncFunctionDef,
        ast.Lambda,
    )

    def setUp(self) -> None:
        self.source = Path(run_loop.__file__ or "").read_text(encoding="utf-8")
        self.tree = ast.parse(self.source)
        # The dispatch lives in `run_loop`; the disposition it dispatches on
        # moved to `handoff_guards`, so the two are read from separate trees
        # and only the loop's is ever mutated by these tests.
        self.disposition_tree = ast.parse(
            Path(handoff_guards.__file__).read_text(encoding="utf-8")
        )

    def disposition_names(self) -> list[str]:
        functions = [
            node
            for node in ast.walk(self.disposition_tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "_terminating_result_disposition"
        ]
        self.assertEqual(1, len(functions), "expected one disposition function")
        return [
            node.value.value
            for node in ast.walk(functions[0])
            if isinstance(node, ast.Return)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ]

    def session_loop(self, tree: ast.Module) -> ast.While:
        loops = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.While)
            and any(
                isinstance(name, ast.Name) and name.id == "terminating_disposition"
                for statement in node.body
                for name in ast.walk(statement)
            )
        ]
        self.assertEqual(1, len(loops), "expected exactly one session loop")
        return loops[0]

    def gated_ifs(self, node: ast.AST) -> list[ast.If]:
        """Every branch under ``node`` that runs the guard on a disposition."""
        return [
            child
            for child in ast.walk(node)
            if isinstance(child, ast.If)
            and "_enforce_plan_artifact_guard(" in ast.unparse(child.test)
            and "terminating_disposition is not None" in ast.unparse(child.test)
        ]

    def enclosing_statement(self, root: ast.AST, target: ast.AST) -> ast.stmt:
        """The innermost statement under ``root`` that contains ``target``.

        A call's own column is wherever its callee starts, which for a delegated
        step is past the assignment that keeps its result. The statement is what
        carries the loop's indentation, so it is what the mutations below anchor
        to.
        """
        found: ast.stmt | None = None

        def visit(node: ast.AST, statement: ast.stmt | None) -> bool:
            nonlocal found
            if node is target:
                found = statement
                return True
            for child in ast.iter_child_nodes(node):
                inner = child if isinstance(child, ast.stmt) else statement
                if visit(child, inner):
                    return True
            return False

        visit(root, None)
        self.assertIsNotNone(found, "expected the call to sit inside a statement")
        return cast(ast.stmt, found)

    def gated_guard_site(self, tree: ast.Module) -> GuardSite:
        """Where the loop has run the gated guard, wherever the guard lives."""
        loop = self.session_loop(tree)
        in_the_loop = self.gated_ifs(loop)
        if in_the_loop:
            self.assertEqual(
                1,
                len(in_the_loop),
                "the terminating dispositions must reach the plan-artifact "
                "guard through exactly one gated call",
            )
            guard = in_the_loop[0]
            return GuardSite(
                loop_lineno=guard.lineno,
                call_end_lineno=cast(int, guard.end_lineno),
                loop_end_lineno=cast(int, guard.end_lineno),
                col_offset=guard.col_offset,
                owner=None,
                owner_lineno=-1,
                result_name=None,
            )

        owners = [
            (node, gated)
            for module in DISPOSITION_DELEGATES
            for node in ast.parse(
                Path(module.__file__ or "").read_text(encoding="utf-8")
            ).body
            if isinstance(node, ast.FunctionDef)
            for gated in [self.gated_ifs(node)]
            if gated
        ]
        self.assertEqual(
            1,
            len(owners),
            "the terminating dispositions must reach the plan-artifact guard "
            "through exactly one gated call, in the loop or in one delegate",
        )
        owner, gated_in_owner = owners[0]
        self.assertEqual(
            1,
            len(gated_in_owner),
            "the terminating dispositions must reach the plan-artifact guard "
            "through exactly one gated call",
        )

        calls = [
            node
            for node in ast.walk(loop)
            if isinstance(node, ast.Call)
            and (
                (isinstance(node.func, ast.Name) and node.func.id == owner.name)
                or (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == owner.name
                )
            )
        ]
        self.assertEqual(
            1,
            len(calls),
            f"expected the loop to reach {owner.name} exactly once, so the "
            "guard it runs has one place in the turn",
        )
        statement = self.enclosing_statement(loop, calls[0])
        self.assertIsInstance(
            statement,
            ast.Assign,
            "the loop must keep the delegate's verdict, since the guard's "
            "refusal is only visible through it",
        )
        target = cast(ast.Assign, statement).targets[0]
        self.assertIsInstance(target, ast.Name)
        result_name = cast(ast.Name, target).id

        # The delegation ends where the loop takes the disposition out of the
        # verdict: before that line the guard's answer is still in transit, and
        # nothing in the loop has branched on it.
        forwarded = [
            cast(int, node.end_lineno)
            for node in self.disposition_assignments(loop)
            if node.lineno > statement.lineno
        ]
        end = min(forwarded) if forwarded else statement.end_lineno
        return GuardSite(
            loop_lineno=statement.lineno,
            call_end_lineno=cast(int, statement.end_lineno),
            loop_end_lineno=cast(int, end),
            col_offset=statement.col_offset,
            owner=owner,
            owner_lineno=gated_in_owner[0].lineno,
            result_name=result_name,
        )

    def disposition_assignments(self, node: ast.AST) -> list[ast.Assign]:
        """Every assignment to the name the dispatch reads, under ``node``."""
        return [
            child
            for child in ast.walk(node)
            if isinstance(child, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "terminating_disposition"
                for target in child.targets
            )
        ]

    def stop_propagation_lines(self, loop: ast.While, result_name: str) -> set[int]:
        """Lines of the edges that carry the delegate's own refusal outwards."""
        lines: set[int] = set()
        for node in ast.walk(loop):
            if not isinstance(node, ast.If):
                continue
            if ast.unparse(node.test) != f"{result_name}.stopped":
                continue
            for edge in self.turn_edges(node):
                lines.add(edge.lineno)
        return lines

    def unconditional_guard_ends(self, loop: ast.While) -> list[int]:
        """Lines after which the plan-artifact guard has run on every path."""
        return [
            statement.end_lineno
            for statement in loop.body
            if isinstance(statement, ast.If)
            and "_enforce_plan_artifact_guard(" in ast.unparse(statement.test)
            and "terminating_disposition" not in ast.unparse(statement.test)
        ]

    def status_guard_line(self, loop: ast.While) -> int:
        """The line where the status guards take over the diagnosis."""
        # Match `name(...)` and `module.name(...)` alike, for the reason
        # `_call_lines` states below: the callee moved into a package module and
        # a Name-only match would stop seeing this gate the moment it did.
        calls = [
            node.lineno
            for node in ast.walk(loop)
            if isinstance(node, ast.Call)
            and (
                (
                    isinstance(node.func, ast.Name)
                    and node.func.id == "_repair_paused_handoff_status"
                )
                or (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "_repair_paused_handoff_status"
                )
            )
        ]
        self.assertEqual(
            1,
            len(calls),
            "expected one paused-status repair call to open the status guards",
        )
        return calls[0]

    def dispatch_branches(self, loop: ast.While) -> list[tuple[ast.If, str]]:
        branches: list[tuple[ast.If, str]] = []
        for node in ast.walk(loop):
            if not isinstance(node, ast.If):
                continue
            test = node.test
            if not isinstance(test, ast.Compare) or len(test.ops) != 1:
                continue
            if not isinstance(test.ops[0], ast.Eq):
                continue
            left = test.left
            if not isinstance(left, ast.Name) or left.id != "terminating_disposition":
                continue
            comparator = test.comparators[0]
            if not isinstance(comparator, ast.Constant):
                continue
            if not isinstance(comparator.value, str):
                continue
            branches.append((node, comparator.value))
        branches.sort(key=lambda branch: branch[0].lineno)
        return branches

    def turn_edges(
        self, node: ast.AST, *, in_inner_loop: bool = False
    ) -> list[ast.Break | ast.Continue | ast.Return]:
        """Every edge under ``node`` that ends or relaunches the current turn.

        A return leaves ``run_locked`` outright, so it ends the turn from
        inside a nested loop just as a top-level one does; only a nested
        callable takes ownership of it. A break or continue is claimed by the
        nearest enclosing loop instead, which for a nested loop's else suite is
        the loop around it: the suite runs once that nested loop is already
        finished. Callers that start below a nested loop must say so through
        ``in_inner_loop``, because the node alone cannot show what encloses it.
        """
        edges: list[ast.Break | ast.Continue | ast.Return] = []

        def visit(current: ast.AST, *, in_inner_loop: bool) -> None:
            if isinstance(current, self.INNER_CALLABLES):
                return
            if isinstance(current, ast.Return):
                edges.append(current)
                return
            if isinstance(current, ast.Break | ast.Continue):
                if not in_inner_loop:
                    edges.append(current)
                return
            if isinstance(current, self.INNER_LOOPS):
                for child in current.body:
                    visit(child, in_inner_loop=True)
                for child in current.orelse:
                    visit(child, in_inner_loop=in_inner_loop)
                return
            for child in ast.iter_child_nodes(current):
                visit(child, in_inner_loop=in_inner_loop)

        for child in ast.iter_child_nodes(node):
            visit(child, in_inner_loop=in_inner_loop)
        return edges

    def loop_ifs(self, loop: ast.While) -> list[tuple[ast.If, bool]]:
        """Every if in the loop that the turn itself runs, with its context.

        The flag says whether a nested loop owns the branch's break and
        continue edges, which the branch's own subtree cannot tell.
        """
        found: list[tuple[ast.If, bool]] = []

        def visit(current: ast.AST, *, in_inner_loop: bool) -> None:
            if isinstance(current, self.INNER_CALLABLES):
                return
            if isinstance(current, ast.If):
                found.append((current, in_inner_loop))
            if isinstance(current, self.INNER_LOOPS):
                for child in current.body:
                    visit(child, in_inner_loop=True)
                for child in current.orelse:
                    visit(child, in_inner_loop=in_inner_loop)
                return
            for child in ast.iter_child_nodes(current):
                visit(child, in_inner_loop=in_inner_loop)

        for child in ast.iter_child_nodes(loop):
            visit(child, in_inner_loop=False)
        return found

    def retested_conditions(self, test: ast.expr) -> list[str]:
        condition = ast.unparse(test)
        retested = [marker for marker in self.RAW_CONDITIONS if marker in condition]
        if not retested and any(
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "result"
            for node in ast.walk(test)
        ):
            retested.append("the raw harness result")
        return retested

    def check_every_terminating_branch_is_selected_by_the_disposition(
        self, tree: ast.Module
    ) -> None:
        names = self.disposition_names()
        self.assertEqual(
            list(self.DISPOSITION_NAMES),
            names,
            "the disposition names and their order are the dispatch's contract",
        )
        branches = self.dispatch_branches(self.session_loop(tree))
        self.assertEqual(
            names,
            [name for _, name in branches],
            "the terminating branches must be selected by the names "
            "_terminating_result_disposition returns, in the order it returns "
            "them: a branch it cannot name is unreachable, and a name it "
            "returns with no branch is a terminating condition the dispatch "
            "drops",
        )

    def check_the_plan_artifact_guard_runs_before_every_terminating_branch(
        self, tree: ast.Module
    ) -> None:
        loop = self.session_loop(tree)
        site = self.gated_guard_site(tree)
        scope: ast.AST = loop if site.owner is None else site.owner
        guard_line = site.loop_lineno if site.owner is None else site.owner_lineno
        # An assignment that calls something computes the disposition; one that
        # only reads an attribute forwards a disposition already computed. Only
        # the first may happen, and only once.
        computed = [
            node
            for node in self.disposition_assignments(scope)
            if any(isinstance(child, ast.Call) for child in ast.walk(node.value))
        ]

        self.assertEqual(
            1,
            len(computed),
            "the disposition must be computed once, so the guard and the "
            "dispatch cannot disagree about which branch ends the turn",
        )
        self.assertLess(computed[0].lineno, guard_line)
        if site.owner is not None:
            forwarded = self.disposition_assignments(loop)
            self.assertEqual(
                1,
                len(forwarded),
                "the loop must take the disposition from the delegate once",
            )
            value = forwarded[0].value
            self.assertTrue(
                isinstance(value, ast.Attribute)
                and value.attr == "terminating_disposition"
                and isinstance(value.value, ast.Name)
                and value.value.id == site.result_name,
                "the loop must dispatch on the disposition the guard was gated "
                "on rather than compute a second one the guard never saw",
            )
        branches = self.dispatch_branches(loop)
        self.assertTrue(branches, "expected the dispatch to name its branches")
        for node, name in branches:
            self.assertLess(
                site.loop_end_lineno,
                node.lineno,
                f"the {name} branch runs before the plan-artifact guard, so a "
                "turn that tampered with another session's packet would end "
                "there unguarded",
            )

    def check_the_dispatch_owns_every_edge_before_the_status_guards(
        self, tree: ast.Module
    ) -> None:
        loop = self.session_loop(tree)
        site = self.gated_guard_site(tree)
        status_line = self.status_guard_line(loop)
        self.assertLess(
            site.loop_end_lineno,
            status_line,
            "the gated guard must run before the status guards take over",
        )
        selected = [
            (node.lineno, node.end_lineno) for node, _ in self.dispatch_branches(loop)
        ]
        guarded_after = self.unconditional_guard_ends(loop)

        if site.result_name is not None:
            # The delegation runs the guard and hands back a verdict, so the
            # only turn it may end between those two lines is the one the guard
            # itself refused. Anything else there would leave before the loop
            # ever holds the disposition the guard was gated on.
            propagated = self.stop_propagation_lines(loop, site.result_name)
            for edge in self.turn_edges(loop):
                if not site.loop_lineno <= edge.lineno <= site.loop_end_lineno:
                    continue
                self.assertIn(
                    edge.lineno,
                    propagated,
                    f"a {type(edge).__name__.lower()} at agent_session_loop/"
                    f"run_loop.py:{edge.lineno} ends or relaunches the turn "
                    "inside the delegation that runs the plan-artifact guard, "
                    f"without being the refusal {site.result_name} reports",
                )

        for edge in self.turn_edges(loop):
            if not site.loop_end_lineno < edge.lineno < status_line:
                continue
            if any(start <= edge.lineno <= end for start, end in selected):
                continue
            if any(end < edge.lineno for end in guarded_after):
                continue
            self.fail(
                f"a {type(edge).__name__.lower()} at agent_session_loop/"
                f"run_loop.py:{edge.lineno} ends or relaunches the turn between "
                "the plan-artifact guard and the status guards without being "
                "selected by terminating_disposition; the guard is gated on a "
                "disposition this edge does not have, so a turn that tampered "
                "with another session's packet would leave here unguarded",
            )

    def check_no_terminating_branch_re_tests_the_raw_result(
        self, tree: ast.Module
    ) -> None:
        loop = self.session_loop(tree)
        guard_line = self.gated_guard_site(tree).loop_lineno

        for node, in_inner_loop in self.loop_ifs(loop):
            if node.lineno < guard_line:
                continue
            named = self.retested_conditions(node.test)
            ends_turn = bool(self.turn_edges(node, in_inner_loop=in_inner_loop))
            self.assertFalse(
                named and ends_turn,
                "a branch that ends the turn must be selected by "
                f"terminating_disposition rather than by re-testing "
                f"{', '.join(named)} (agent_session_loop/runner.py:"
                f"{node.lineno}); stating the condition twice is how a "
                "branch ends up outside the disposition and past the guard",
            )

    def mutated_tree(
        self, *, after_line: int, column: int, body: tuple[str, ...]
    ) -> ast.Module:
        lines = self.source.splitlines()
        lines[after_line:after_line] = [" " * column + line for line in body]
        return ast.parse("\n".join(lines))

    def test_every_terminating_branch_is_selected_by_the_disposition(self) -> None:
        self.check_every_terminating_branch_is_selected_by_the_disposition(self.tree)

    def test_the_plan_artifact_guard_runs_before_every_terminating_branch(self) -> None:
        self.check_the_plan_artifact_guard_runs_before_every_terminating_branch(
            self.tree
        )

    def test_the_dispatch_owns_every_edge_before_the_status_guards(self) -> None:
        self.check_the_dispatch_owns_every_edge_before_the_status_guards(self.tree)

    def test_no_terminating_branch_re_tests_the_raw_result(self) -> None:
        self.check_no_terminating_branch_re_tests_the_raw_result(self.tree)

    def test_a_top_level_turn_payload_relaunch_is_rejected(self) -> None:
        site = self.gated_guard_site(self.tree)

        for edge in ("continue", "break", "return"):
            with self.subTest(edge=edge):
                mutated = self.mutated_tree(
                    after_line=site.loop_end_lineno,
                    column=site.col_offset,
                    body=('if turn_payload.get("future_retry"):', f"    {edge}"),
                )

                with self.assertRaises(AssertionError):
                    self.check_the_dispatch_owns_every_edge_before_the_status_guards(
                        mutated
                    )

    def test_an_unguarded_edge_inside_the_delegation_is_rejected(self) -> None:
        site = self.gated_guard_site(self.tree)
        if site.result_name is None:
            self.skipTest("the guard runs in the loop, so nothing is delegated")
        # Between the call and the line that takes the disposition out of the
        # verdict, the guard has already run but the loop cannot yet say on
        # which disposition; only the delegate's own refusal may leave here.
        for edge in ("continue", "break", "return"):
            with self.subTest(edge=edge):
                mutated = self.mutated_tree(
                    after_line=site.call_end_lineno,
                    column=site.col_offset,
                    body=('if turn_payload.get("future_retry"):', f"    {edge}"),
                )

                with self.assertRaises(AssertionError):
                    self.check_the_dispatch_owns_every_edge_before_the_status_guards(
                        mutated
                    )

    def test_a_nested_raw_result_branch_is_rejected(self) -> None:
        loop = self.session_loop(self.tree)
        branch = next(
            node
            for node, name in self.dispatch_branches(loop)
            if name == "malformed-plan"
        )
        anchor = branch.body[0]

        for edge in ("break", "continue", "return"):
            with self.subTest(edge=edge):
                mutated = self.mutated_tree(
                    after_line=anchor.lineno - 1,
                    column=anchor.col_offset,
                    body=("if result.returncode:", f"    {edge}"),
                )

                with self.assertRaises(AssertionError):
                    self.check_no_terminating_branch_re_tests_the_raw_result(mutated)

    def test_a_return_is_owned_by_its_callable_not_by_its_loop(self) -> None:
        site = self.gated_guard_site(self.tree)
        # A helper defined here returns from itself, so neither check may claim
        # its edge; a loop body runs inside the turn, so its return still ends
        # the turn and both checks must.
        owned_by_a_callable = self.mutated_tree(
            after_line=site.loop_end_lineno,
            column=site.col_offset,
            body=(
                "def _future_helper() -> None:",
                "    if result.returncode:",
                "        return",
            ),
        )

        self.check_the_dispatch_owns_every_edge_before_the_status_guards(
            owned_by_a_callable
        )
        self.check_no_terminating_branch_re_tests_the_raw_result(owned_by_a_callable)

        owned_by_the_turn = self.mutated_tree(
            after_line=site.loop_end_lineno,
            column=site.col_offset,
            body=(
                "for _ in ():",
                '    if turn_payload.get("future_retry"):',
                "        return",
            ),
        )

        with self.assertRaises(AssertionError):
            self.check_the_dispatch_owns_every_edge_before_the_status_guards(
                owned_by_the_turn
            )

    def test_a_nested_loop_else_relaunch_is_rejected(self) -> None:
        site = self.gated_guard_site(self.tree)
        # The else suite runs after the nested loop has finished, so Python
        # binds these to the session loop and they end the turn past the guard.
        for edge in ("break", "continue"):
            with self.subTest(edge=edge):
                mutated = self.mutated_tree(
                    after_line=site.loop_end_lineno,
                    column=site.col_offset,
                    body=(
                        "for _ in ():",
                        "    pass",
                        "else:",
                        '    if turn_payload.get("future_retry"):',
                        f"        {edge}",
                    ),
                )

                with self.assertRaises(AssertionError):
                    self.check_the_dispatch_owns_every_edge_before_the_status_guards(
                        mutated
                    )

    def test_a_nested_loop_edge_belongs_to_the_suite_that_owns_it(self) -> None:
        loop = self.session_loop(self.tree)
        branch = next(
            node
            for node, name in self.dispatch_branches(loop)
            if name == "malformed-plan"
        )
        anchor = branch.body[0]

        for edge in ("break", "continue"):
            with self.subTest(edge=edge):
                owned_by_the_nested_loop = self.mutated_tree(
                    after_line=anchor.lineno - 1,
                    column=anchor.col_offset,
                    body=(
                        "for _ in ():",
                        "    if result.returncode:",
                        f"        {edge}",
                    ),
                )

                self.check_no_terminating_branch_re_tests_the_raw_result(
                    owned_by_the_nested_loop
                )

                owned_by_the_session_loop = self.mutated_tree(
                    after_line=anchor.lineno - 1,
                    column=anchor.col_offset,
                    body=(
                        "for _ in ():",
                        "    pass",
                        "else:",
                        "    if result.returncode:",
                        f"        {edge}",
                    ),
                )

                with self.assertRaises(AssertionError):
                    self.check_no_terminating_branch_re_tests_the_raw_result(
                        owned_by_the_session_loop
                    )


class ResumeGateTests(unittest.TestCase):
    def _report(
        self,
        *,
        state: str | None,
        findings: tuple[preflight.PreflightFinding, ...] = (),
    ) -> preflight.PreflightReport:
        readiness = None
        if state is not None:
            readiness = plans.ReadinessMetadata(
                state=state,
                readiness_sha256="0" * 64,
                checked_at="2026-06-19T00:00:00Z",
            )
        return preflight.PreflightReport(
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
                        preflight.PreflightFinding(
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


class RunVerificationEnforcementTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.plan_dir = self.tmp_path / "plan"
        self.state_dir = self.tmp_path / "state"
        self.repo_root = self.tmp_path / "repo"
        self.config_home = self.tmp_path / "config"
        init_git_repo(self.repo_root)
        self.fake_codex = install_fake_codex(self.tmp_path)
        config_path = self.config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps({"codex": {"executable": str(self.fake_codex)}}),
            encoding="utf-8",
        )

    def _write_plan(
        self,
        statuses: tuple[str, ...],
        *,
        contracts_by_id: dict[str, str] | None,
        readiness: str | None = "loop-ready",
    ) -> None:
        write_plan(self.plan_dir, statuses, contracts_by_id=contracts_by_id)
        if readiness is None:
            return
        sha = plans.stable_readiness_sha256(self.plan_dir)
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state=readiness,
                readiness_sha256=sha,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )

    def _run(
        self,
        *,
        allow_not_loop_ready: bool = True,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
        argv = [
            sys.executable,
            "-m",
            "agent_session_loop",
            "run",
            str(self.plan_dir),
            "--cwd",
            str(self.repo_root),
            "--harness",
            "codex",
            "--state-dir",
            str(self.state_dir),
            "--json",
        ]
        if allow_not_loop_ready:
            argv.append("--allow-not-loop-ready")
        env = os.environ.copy()
        env.update(
            {
                "XDG_CONFIG_HOME": str(self.config_home),
                "FAKE_CODEX_MODE": "success",
                "FAKE_CODEX_SLEEP": "5",
                "PYTHONPATH": str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", ""),
            }
        )
        completed = subprocess.run(
            argv,
            cwd=REPO_ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        return completed, json.loads(completed.stdout)

    def test_verify_passing_contract_allows_session_to_complete(self) -> None:
        contracts_by_id = {
            "1": _contract_block(
                '{"type": "file_exists", "path": "README.md", "name": "readme"}',
                session="1",
            )
        }
        self._write_plan(("pending",), contracts_by_id=contracts_by_id)

        completed, payload = self._run()

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])
        self.assertEqual("all-complete", payload["reason"])
        session_file = self.plan_dir / "session-1.md"
        text = session_file.read_text(encoding="utf-8")
        self.assertIn("- **Status**: complete", text)
        turns = payload["turns"]
        self.assertEqual(1, len(turns))
        verify_payload = turns[0]["verification"]
        self.assertEqual("ok", verify_payload["reason"])
        self.assertTrue(verify_payload["passed"])

    def test_verify_passing_contract_emits_verification_phase_events(
        self,
    ) -> None:
        contracts_by_id = {
            "1": _contract_block(
                '{"type": "file_exists", "path": "README.md", "name": "readme"}',
                session="1",
            )
        }
        self._write_plan(("pending",), contracts_by_id=contracts_by_id)

        completed, payload = self._run()

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        final_snapshot = payload["final_snapshot"]
        self.assertEqual(1, final_snapshot["counts"]["complete"])
        self.assertEqual(0, final_snapshot["counts"]["pending"])
        event_path = (
            state.plan_state_dir(self.plan_dir, self.state_dir)
            / events.EVENTS_FILE_NAME
        )
        event_records = read_jsonl(event_path)
        phase_events = [
            record["name"]
            for record in event_records
            if record["name"]
            in {
                events.VERIFICATION_STARTED_EVENT,
                events.VERIFICATION_CHECK_STARTED_EVENT,
                events.VERIFICATION_CHECK_FINISHED_EVENT,
                events.VERIFICATION_FINISHED_EVENT,
            }
        ]
        self.assertEqual(
            [
                events.VERIFICATION_STARTED_EVENT,
                events.VERIFICATION_CHECK_STARTED_EVENT,
                events.VERIFICATION_CHECK_FINISHED_EVENT,
                events.VERIFICATION_FINISHED_EVENT,
            ],
            phase_events,
        )
        started = next(
            record["payload"]
            for record in event_records
            if record["name"] == events.VERIFICATION_STARTED_EVENT
        )
        self.assertEqual(1, started["check_count"])
        self.assertEqual(["readme"], started["check_names"])
        check_started = next(
            record["payload"]
            for record in event_records
            if record["name"] == events.VERIFICATION_CHECK_STARTED_EVENT
        )
        self.assertEqual("readme", check_started["name"])
        self.assertEqual("file_exists", check_started["kind"])
        check_finished = next(
            record["payload"]
            for record in event_records
            if record["name"] == events.VERIFICATION_CHECK_FINISHED_EVENT
        )
        self.assertEqual("readme", check_finished["name"])
        self.assertEqual(verification.CHECK_ROLE_GATE, check_finished["role"])
        finished = next(
            record["payload"]
            for record in event_records
            if record["name"] == events.VERIFICATION_FINISHED_EVENT
        )
        self.assertTrue(finished["passed"])
        self.assertEqual(1, finished["passed_count"])
        self.assertEqual("ok", finished["reason"])

    def test_verify_failing_contract_pauses_and_reverts_status(self) -> None:
        contracts_by_id = {
            "1": _contract_block(
                '{"type": "file_exists", "path": "MISSING.md", "name": "missing"}',
                session="1",
            )
        }
        self._write_plan(("pending",), contracts_by_id=contracts_by_id)

        completed, payload = self._run()

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["paused"])
        self.assertEqual("verify-failed", payload["pause_reason"])
        self.assertIn("verification failed", payload["pause_message"])
        pause_details = payload["pause_details"]
        self.assertFalse(pause_details["passed"])
        self.assertEqual(["missing"], pause_details["recipe"]["names"])
        self.assertEqual(1, len(pause_details["failures"]))
        failure = pause_details["failures"][0]
        self.assertEqual("missing", failure["name"])
        self.assertEqual("file_exists", failure["kind"])
        self.assertEqual("file-missing", failure["failure_reason"])
        session_file = self.plan_dir / "session-1.md"
        text = session_file.read_text(encoding="utf-8")
        self.assertIn("- **Status**: pending", text)
        self.assertNotIn("- **Status**: complete", text)
        self.assertIn("verification failed", text)
        self.assertIn("file-missing", text)
        final_snapshot = payload["final_snapshot"]
        self.assertEqual(1, final_snapshot["counts"]["pending"])
        self.assertEqual(0, final_snapshot["counts"]["complete"])

    def test_verify_failing_contract_clears_active_phases_and_finishes_run(
        self,
    ) -> None:
        contracts_by_id = {
            "1": _contract_block(
                '{"type": "file_exists", "path": "MISSING.md", "name": "missing"}',
                session="1",
            )
        }
        self._write_plan(("pending",), contracts_by_id=contracts_by_id)

        completed, payload = self._run()

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["paused"])
        self.assertEqual("verify-failed", payload["pause_reason"])
        self.assertIsNone(payload["active_turn"])
        self.assertIsNone(payload["active_verification"])
        self.assertIsInstance(payload["run_finished_at"], str)
        self.assertNotEqual("", payload["run_finished_at"])

    def test_verify_parallel_contract_completes_and_marks_event(self) -> None:
        contracts_by_id = {
            "1": _contract_block(
                '{"type": "command", "name": "quick", '
                '"argv": ["python3", "-c", "pass"], "timeout_seconds": 60},'
                '{"type": "command", "name": "slow", '
                '"argv": ["python3", "-c", "import time; time.sleep(0.2)"], '
                '"timeout_seconds": 60},'
                '{"type": "file_exists", "path": "README.md", '
                '"name": "readme"}',
                session="1",
                parallel=True,
            )
        }
        self._write_plan(("pending",), contracts_by_id=contracts_by_id)

        completed, payload = self._run()

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual("all-complete", payload["reason"])
        verify_payload = payload["turns"][0]["verification"]
        self.assertEqual("ok", verify_payload["reason"])
        self.assertTrue(verify_payload["passed"])
        event_path = (
            state.plan_state_dir(self.plan_dir, self.state_dir)
            / events.EVENTS_FILE_NAME
        )
        event_records = read_jsonl(event_path)
        started = next(
            record["payload"]
            for record in event_records
            if record["name"] == events.VERIFICATION_STARTED_EVENT
        )
        self.assertTrue(started["parallel"])
        self.assertEqual(["quick", "slow", "readme"], started["check_names"])
        finished = next(
            record["payload"]
            for record in event_records
            if record["name"] == events.VERIFICATION_FINISHED_EVENT
        )
        self.assertTrue(finished["passed"])
        self.assertEqual(3, finished["passed_count"])

    def test_verify_parallel_contract_pauses_with_declared_order_details(
        self,
    ) -> None:
        contracts_by_id = {
            "1": _contract_block(
                '{"type": "command", "name": "alpha", '
                '"argv": ["python3", "-c", "import sys; sys.exit(3)"], '
                '"timeout_seconds": 60},'
                '{"type": "command", "name": "beta", '
                '"argv": ["python3", "-c", '
                '"import sys, time; time.sleep(0.2); sys.exit(4)"], '
                '"timeout_seconds": 60}',
                session="1",
                parallel=True,
            )
        }
        self._write_plan(("pending",), contracts_by_id=contracts_by_id)

        completed, payload = self._run()

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["paused"])
        self.assertEqual("verify-failed", payload["pause_reason"])
        pause_details = payload["pause_details"]
        self.assertFalse(pause_details["passed"])
        self.assertEqual(["alpha", "beta"], pause_details["recipe"]["names"])
        # `alpha` fails first in wall-clock order but must still be reported
        # in its declared position, ahead of the slower `beta`.
        self.assertEqual(
            ["alpha", "beta"],
            [failure["name"] for failure in pause_details["failures"]],
        )
        self.assertEqual(
            [3, 4], [failure["returncode"] for failure in pause_details["failures"]]
        )
        text = (self.plan_dir / "session-1.md").read_text(encoding="utf-8")
        self.assertIn("- **Status**: pending", text)

    def test_verify_missing_contract_pauses_when_gate_enforced(self) -> None:
        self._write_plan(("pending",), contracts_by_id=None)

        completed, payload = self._run(allow_not_loop_ready=False)

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["paused"])
        # Preflight gate fails first because contracts are required for
        # loop-ready plans, so the runner pauses on `not-loop-ready` before
        # verification runs.
        self.assertEqual("not-loop-ready", payload["pause_reason"])

    def test_verify_missing_contract_is_skipped_when_gate_bypassed(self) -> None:
        self._write_plan(("pending",), contracts_by_id=None, readiness=None)

        completed, payload = self._run(allow_not_loop_ready=True)

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])
        verify_payload = payload["turns"][0]["verification"]
        self.assertEqual("no-contract", verify_payload["reason"])
        self.assertFalse(verify_payload["enforced"])

    def test_verify_malformed_contract_pauses_with_malformed_contract(self) -> None:
        bad_contract = "\n".join(
            [
                "## Completion Contract",
                "",
                "### Done when",
                "- recipe runs",
                "",
                "### Verify",
                "",
                "```json",
                '{"session": "1", "checks": [{"type": "command", '
                '"argv": ["echo"], "timeout_seconds": -1}]}',
                "```",
            ]
        )
        self._write_plan(
            ("pending",),
            contracts_by_id={"1": bad_contract},
            readiness=None,
        )

        completed, payload = self._run(allow_not_loop_ready=True)

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["paused"])
        self.assertEqual("malformed-contract", payload["pause_reason"])
        session_file = self.plan_dir / "session-1.md"
        text = session_file.read_text(encoding="utf-8")
        self.assertIn("- **Status**: pending", text)
        self.assertIn("verification skipped", text)

    def test_verify_failed_pause_is_listed_in_pause_reasons(self) -> None:
        self.assertIn("verify-failed", events.PAUSE_REASONS)
        self.assertIn("no-contract", events.PAUSE_REASONS)
        self.assertIn("malformed-contract", events.PAUSE_REASONS)


class LegacyImplementationGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.repo_root = self.tmp_path / "repo"
        self.plan_dir = self.tmp_path / "plan"
        self.state_dir = self.tmp_path / "state"
        init_git_repo(self.repo_root)
        write_plan(
            self.plan_dir,
            ("pending",),
            contracts_by_id={
                "1": _contract_block(
                    '{"type": "file_exists", "path": "README.md", "name": "readme"}',
                    session="1",
                )
            },
        )
        self.context = verification.VerificationContext.legacy(self.repo_root)

    def _args(self) -> argparse.Namespace:
        return argparse.Namespace(
            plan_dir=str(self.plan_dir),
            cwd=str(self.repo_root),
            harness="codex",
            state_dir=str(self.state_dir),
            json=True,
            dry_run=False,
            unsafe_bypass=True,
            timeout=None,
            max_sessions=None,
            commit_mode=None,
            model=None,
            effort=None,
            note=None,
            allow_not_loop_ready=True,
        )

    def _invoke(self, run_id: str) -> tuple[int, dict[str, Any]]:
        lock_info = state.acquire_lock(self.plan_dir, self.state_dir)
        try:
            return run_loop.run_locked(
                args=self._args(),
                cwd=self.repo_root,
                timeout=None,
                max_sessions=None,
                commit_mode=None,
                lock_info=lock_info,
                run_id=run_id,
                verification_context=self.context,
            )
        finally:
            state.release_lock(self.plan_dir, self.state_dir)

    def _mark_complete(self) -> harnesses.HarnessResult:
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

    def test_commit_mode_none_records_worker_created_commit(self) -> None:
        def commit_then_complete(*_args: object, **_kwargs: object) -> Any:
            (self.repo_root / "README.md").write_text(
                "worker commit\n", encoding="utf-8"
            )
            subprocess.run(
                ["git", "commit", "-am", "worker commit"],
                cwd=self.repo_root,
                text=True,
                capture_output=True,
                check=True,
            )
            return self._mark_complete()

        with mock.patch.object(
            harnesses, "run_harness", side_effect=commit_then_complete
        ):
            exit_code, payload = self._invoke("legacy-worker-commit")

        self.assertEqual(0, exit_code)
        self.assertIsNone(payload["commit_mode"])
        self.assertEqual([], payload["commit_groups"])
        guard = payload["turns"][0]["workspace_write_guard"]
        self.assertTrue(guard["passed"])
        self.assertEqual("legacy-single-repository", guard["scope_mode"])
        transition = guard["changed"]["."]["head_transition"]
        self.assertEqual("fast-forward", transition["relation"])
        self.assertEqual(1, transition["commits_added"])
        self.assertFalse(transition["history_rewrite"])

    def test_commit_mode_none_records_worker_history_rewrite(self) -> None:
        readme = self.repo_root / "README.md"
        readme.write_text("old branch tip\n", encoding="utf-8")
        subprocess.run(
            ["git", "commit", "-am", "old branch tip"],
            cwd=self.repo_root,
            text=True,
            capture_output=True,
            check=True,
        )
        old_tip = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.repo_root,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()

        def rewrite_then_complete(*_args: object, **_kwargs: object) -> Any:
            subprocess.run(
                ["git", "branch", "backup/pre-rewrite", old_tip],
                cwd=self.repo_root,
                text=True,
                capture_output=True,
                check=True,
            )
            subprocess.run(
                ["git", "reset", "--hard", "HEAD~1"],
                cwd=self.repo_root,
                text=True,
                capture_output=True,
                check=True,
            )
            readme.write_text("rewritten branch tip\n", encoding="utf-8")
            subprocess.run(
                ["git", "commit", "-am", "rewritten branch tip"],
                cwd=self.repo_root,
                text=True,
                capture_output=True,
                check=True,
            )
            return self._mark_complete()

        with mock.patch.object(
            harnesses, "run_harness", side_effect=rewrite_then_complete
        ):
            exit_code, payload = self._invoke("legacy-history-rewrite")

        self.assertEqual(0, exit_code)
        changes = payload["turns"][0]["workspace_write_guard"]["changed"]["."]
        transition = changes["head_transition"]
        self.assertEqual("rewrite", transition["relation"])
        self.assertEqual(1, transition["commits_added"])
        self.assertEqual(1, transition["commits_removed"])
        self.assertTrue(transition["history_rewrite"])
        self.assertIn(
            "refs/heads/backup/pre-rewrite",
            [entry["ref"] for entry in changes["refs_added"]],
        )

    def test_a_write_into_an_unobserved_repository_fails_the_guard_closed(
        self,
    ) -> None:
        # ASI-19. The legacy snapshot fingerprints `cwd` and nothing else, so
        # this edit produces no change, no added or removed repository and
        # nothing unavailable -- the three inputs `passed` was computed from.
        # The turn used to be recorded as clean work.
        other_root = self.tmp_path / "other"
        init_git_repo(other_root)

        def edit_elsewhere(*_args: object, **_kwargs: object) -> Any:
            (other_root / "README.md").write_text("edited away\n", encoding="utf-8")
            result = self._mark_complete()
            return dataclasses.replace(
                result,
                last_message=f"Done. I edited {other_root / 'README.md'}.\n",
            )

        with mock.patch.object(harnesses, "run_harness", side_effect=edit_elsewhere):
            exit_code, payload = self._invoke("legacy-unobserved-repository")

        self.assertEqual(1, exit_code)
        self.assertEqual(
            events.PAUSE_REASON_WORKSPACE_WRITE_GUARD_UNAVAILABLE,
            payload["pause_reason"],
        )
        guard = payload["turns"][0]["workspace_write_guard"]
        self.assertFalse(guard["passed"])
        self.assertEqual("legacy-single-repository", guard["scope_mode"])
        entry = guard["unavailable"][
            str(workspaces.canonicalize_filesystem_path(other_root))
        ]
        self.assertEqual("repository-not-observed", entry["reason"])
        # Nothing inside `cwd` moved, which is exactly why the old guard passed.
        self.assertEqual({}, guard["changed"])
        self.assertEqual([], guard["violations"])
        # The session the turn claimed goes back to pending: a completion the
        # guard cannot vouch for is not a completion.
        self.assertTrue(guard["reverted_to_pending"])

    def test_non_git_legacy_scope_refuses_before_harness(self) -> None:
        shutil.rmtree(self.repo_root / ".git")

        with mock.patch.object(
            harnesses,
            "run_harness",
            side_effect=AssertionError("harness must not start without Git scope"),
        ):
            exit_code, payload = self._invoke("legacy-scope-unavailable")

        self.assertEqual(1, exit_code)
        self.assertEqual(
            events.PAUSE_REASON_WORKSPACE_WRITE_GUARD_UNAVAILABLE,
            payload["pause_reason"],
        )
        self.assertEqual([], payload["turns"])
        guard = payload["pause_details"]["workspace_write_guard"]
        self.assertEqual("legacy-single-repository", guard["scope_mode"])
        self.assertEqual("repository-unavailable", guard["unavailable"]["."]["reason"])
        # A refusal before the harness leaves nothing turn-shaped behind. An
        # empty turn directory reads downstream as a turn that ran and wrote no
        # result.json, which is a turn this run never spent.
        self.assertEqual([], sorted(self.state_dir.rglob("turn-*")))


class ProtectedPathGuardRunTests(unittest.TestCase):
    """End-to-end: a frozen path decides whether a finished turn is published.

    The guard is enqueued before the run rather than mid-turn because the
    external control queue is drained once before any turn starts, which makes
    "the guard is active for this turn" deterministic here. The mid-turn
    activation semantics -- a guard added late still compares against the
    turn-start snapshot -- are pinned by the controller and enforcement unit
    tests in tests/test_asl_guards.py.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.repo_root = self.tmp_path / "repo"
        self.plan_dir = self.tmp_path / "plan"
        self.state_dir = self.tmp_path / "state"
        init_git_repo(self.repo_root)
        self.protected = self.repo_root / "notes.md"
        self.protected.write_text("turn-start contents\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "notes.md"],
            cwd=self.repo_root,
            text=True,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "notes"],
            cwd=self.repo_root,
            text=True,
            capture_output=True,
            check=True,
        )
        write_plan(
            self.plan_dir,
            ("pending",),
            contracts_by_id={
                "1": _contract_block(
                    '{"type": "file_exists", "path": "README.md", "name": "readme"}',
                    session="1",
                )
            },
        )
        self.context = verification.VerificationContext.legacy(self.repo_root)
        self.cwd = self.repo_root
        self.review_harness: str | None = None

    def _args(self) -> argparse.Namespace:
        return argparse.Namespace(
            plan_dir=str(self.plan_dir),
            cwd=str(self.cwd),
            harness="codex",
            state_dir=str(self.state_dir),
            json=True,
            dry_run=False,
            unsafe_bypass=True,
            timeout=None,
            max_sessions=None,
            commit_mode=None,
            model=None,
            effort=None,
            note=None,
            allow_not_loop_ready=True,
            # Non-None only for the legacy-review fixture: a run that names a
            # review harness also requests a generated review of its own.
            review_harness=self.review_harness,
        )

    def _use_legacy_review_fixture(self) -> None:
        """Turn the pending session into a generated review of this repository.

        A legacy review turn is the one shape that captures neither write-guard
        snapshot, so it is the only way to reach the crash path with the review
        fingerprint as the turn's sole turn-start comparison.
        """
        self.review_harness = "claude"
        session_path = self.plan_dir / "session-1.md"
        session_path.write_text(
            review_session_body.build_generated_review_session_body(
                plans.parse_session_id("1"),
                review_harness="claude",
                plan_dir=self.plan_dir,
            ),
            encoding="utf-8",
        )
        self.assertTrue(
            review_session_contract.is_generated_review_session_file(session_path)
        )

    def _use_workspace_fixture(self) -> None:
        """Re-point this fixture at a registered two-repository workspace.

        The class default is a legacy single repository, which declares no
        write aliases and therefore has no way to reach a write-scope
        violation. Only the composition tests need one, so the workspace is
        built on demand rather than in ``setUp``.
        """
        self.workspace_root = self.tmp_path / "workspace"
        self.repositories = init_multi_repo_workspace(
            self.workspace_root,
            DEFAULT_MULTI_REPO_SPECS[:2],
        )
        self.plan_dir = self.tmp_path / "workspace-plan"
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
        session_path = self.plan_dir / "session-1.md"
        session_path.write_text(
            session_path.read_text(encoding="utf-8").replace(
                "**Reads repositories**: SVC, OAK\n**Writes repositories**: SVC, OAK",
                "**Reads repositories**: SVC\n**Writes repositories**: SVC",
            ),
            encoding="utf-8",
        )
        registry = plans.read_repository_registry(self.plan_dir)
        assert registry is not None
        plans.save_workspace_plan_metadata(
            self.plan_dir,
            self.workspace_root,
            registry.digest,
        )
        self.context = verification.VerificationContext.from_plan(
            self.plan_dir,
            self.workspace_root,
        )
        self.cwd = self.workspace_root
        # The frozen file lives in the alias the session *may* write, so the
        # scope violation and the protected-path violation are independent
        # breaches rather than two names for one write.
        self.protected = self.repositories["SVC"] / "notes.md"
        self.protected.write_text("turn-start contents\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "notes.md"],
            cwd=self.repositories["SVC"],
            text=True,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "notes"],
            cwd=self.repositories["SVC"],
            text=True,
            capture_output=True,
            check=True,
        )

    def _turn_rejections(self, guard: str) -> list[dict[str, Any]]:
        event_path = (
            state.plan_state_dir(self.plan_dir, self.state_dir)
            / events.EVENTS_FILE_NAME
        )
        return [
            record["payload"]
            for record in read_jsonl(event_path)
            if record["name"] == events.TURN_REJECTED_EVENT
            and record["payload"].get("guard") == guard
        ]

    def _enqueue_guard(self, run_id: str, *protect_paths: str) -> None:
        dispatch = intervention.dispatch_control_command(
            self.plan_dir,
            control_queue.COMMAND_ADD_GUARD,
            state_dir=self.state_dir,
            protect_paths=list(protect_paths),
            reason="operator froze this file",
            expected_run_id=run_id,
        )
        self.assertEqual(
            tuple(protect_paths),
            tuple(
                f"{path.qualifier}:{path.pattern}"
                for path in dispatch.command.protect_paths
            ),
        )

    def _invoke(self, run_id: str) -> tuple[int, dict[str, Any]]:
        lock_info = state.acquire_lock(self.plan_dir, self.state_dir)
        try:
            return run_loop.run_locked(
                args=self._args(),
                cwd=self.cwd,
                timeout=None,
                max_sessions=None,
                commit_mode=None,
                lock_info=lock_info,
                run_id=run_id,
                verification_context=self.context,
            )
        finally:
            state.release_lock(self.plan_dir, self.state_dir)

    def _mark_complete(self) -> harnesses.HarnessResult:
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

    def _run_with(self, run_id: str, turn: Any) -> tuple[int, dict[str, Any]]:
        with mock.patch.object(harnesses, "run_harness", side_effect=turn):
            return self._invoke(run_id)

    def test_a_changed_protected_path_rejects_the_turn_and_keeps_the_change(
        self,
    ) -> None:
        run_id = "guard-protected-changed"
        self._enqueue_guard(run_id, ".:notes.md")

        def edit_protected(*_args: object, **_kwargs: object) -> Any:
            self.protected.write_text("worker rewrote this\n", encoding="utf-8")
            return self._mark_complete()

        exit_code, payload = self._run_with(run_id, edit_protected)

        self.assertEqual(1, exit_code)
        self.assertEqual(
            events.PAUSE_REASON_GUARD_PROTECTED_PATH_CHANGED,
            payload["pause_reason"],
        )
        guard = payload["turns"][0]["protected_path_guard"]
        self.assertFalse(guard["passed"])
        self.assertEqual([".:notes.md"], sorted(guard["matched_paths"].values())[0])
        self.assertEqual(guard["guard_ids"], list(guard["matched_paths"]))
        self.assertTrue(guard["evidence"])
        self.assertIn("command-gate", guard["coverage"][guard["guard_ids"][0]])
        # The completion the turn claimed is withdrawn...
        self.assertTrue(guard["reverted_to_pending"])
        # ...but the worker's edit is left exactly where the worker put it. A
        # guard decides what gets published, not what gets destroyed.
        self.assertTrue(guard["retained"])
        self.assertEqual(
            "worker rewrote this\n", self.protected.read_text(encoding="utf-8")
        )

    def test_a_protected_path_restored_before_the_end_passes(self) -> None:
        run_id = "guard-protected-restored"
        self._enqueue_guard(run_id, ".:notes.md")
        original = self.protected.read_text(encoding="utf-8")

        def edit_then_restore(*_args: object, **_kwargs: object) -> Any:
            self.protected.write_text("scratch\n", encoding="utf-8")
            self.protected.write_text(original, encoding="utf-8")
            return self._mark_complete()

        exit_code, payload = self._run_with(run_id, edit_then_restore)

        self.assertEqual(0, exit_code)
        self.assertIsNone(payload.get("pause_reason"))
        guard = payload["turns"][0]["protected_path_guard"]
        self.assertTrue(guard["passed"])
        self.assertNotIn(".:notes.md", guard["observed_paths"])

    def test_a_dirty_protected_path_at_turn_start_is_the_accepted_baseline(
        self,
    ) -> None:
        run_id = "guard-protected-dirty-baseline"
        self._enqueue_guard(run_id, ".:notes.md")
        # The operator's own uncommitted edit, made before the run. The guard
        # freezes the file as the turn found it, not as HEAD has it.
        self.protected.write_text("operator edit\n", encoding="utf-8")

        def touch_something_else(*_args: object, **_kwargs: object) -> Any:
            (self.repo_root / "README.md").write_text("worker\n", encoding="utf-8")
            return self._mark_complete()

        exit_code, payload = self._run_with(run_id, touch_something_else)

        self.assertEqual(0, exit_code)
        self.assertTrue(payload["turns"][0]["protected_path_guard"]["passed"])
        self.assertEqual("operator edit\n", self.protected.read_text(encoding="utf-8"))

    def test_a_write_outside_the_pattern_does_not_trip_the_guard(self) -> None:
        run_id = "guard-protected-unmatched"
        self._enqueue_guard(run_id, ".:docs/**/*.md")

        def edit_protected(*_args: object, **_kwargs: object) -> Any:
            self.protected.write_text("not under docs/\n", encoding="utf-8")
            return self._mark_complete()

        exit_code, payload = self._run_with(run_id, edit_protected)

        self.assertEqual(0, exit_code)
        guard = payload["turns"][0]["protected_path_guard"]
        self.assertTrue(guard["passed"])
        self.assertIn(".:notes.md", guard["observed_paths"])

    def test_scope_violation_still_adjudicates_protected_paths(self) -> None:
        """Two breaches in one turn are both reported, not just the first.

        The write guard pauses first, so its reason is the one the operator
        acts on. But a turn that also moved a frozen path has to say so: an
        operator who repairs only the scope and reruns would otherwise never
        learn the guard was breached at all.
        """
        self._use_workspace_fixture()
        run_id = "guard-protected-with-scope-violation"
        self._enqueue_guard(run_id, "SVC:notes.md")
        out_of_scope = self.repositories["OAK"] / "README.md"

        def breach_both(*_args: object, **_kwargs: object) -> Any:
            out_of_scope.write_text("outside the write scope\n", encoding="utf-8")
            self.protected.write_text("worker rewrote this\n", encoding="utf-8")
            return self._mark_complete()

        exit_code, payload = self._run_with(run_id, breach_both)

        self.assertEqual(1, exit_code)
        # The primary pause is untouched: same reason, same remediation.
        self.assertEqual(
            events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
            payload["pause_reason"],
        )
        self.assertEqual(
            ["OAK:README.md"],
            payload["turns"][0]["workspace_write_guard"]["violations"][0][
                "qualified_paths"
            ],
        )

        guard = payload["turns"][0]["protected_path_guard"]
        self.assertFalse(guard["passed"])
        self.assertTrue(guard["adjudicated_only"])
        self.assertEqual(
            events.PAUSE_REASON_GUARD_PROTECTED_PATH_CHANGED, guard["reason"]
        )
        self.assertEqual(
            events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
            guard["deferred_to_pause_reason"],
        )
        self.assertEqual(guard["guard_ids"], list(guard["matched_paths"]))
        self.assertEqual(["SVC:notes.md"], sorted(guard["matched_paths"].values())[0])
        # The matched repository's before/after vectors survive the
        # composition, which is what "restore or accept" needs.
        self.assertTrue(guard["evidence"])
        self.assertTrue(guard["retained"])
        # The write guard already withdrew the session; this guard does not
        # withdraw it a second time.
        self.assertFalse(guard["reverted_to_pending"])
        self.assertIn(
            "- **Status**: pending",
            (self.plan_dir / "session-1.md").read_text(encoding="utf-8"),
        )

        # The pause an operator reads carries both halves.
        self.assertEqual(guard, payload["pause_details"]["protected_path_guard"])
        rejections = self._turn_rejections("protected-path-guard")
        self.assertEqual(1, len(rejections))
        self.assertFalse(rejections[0]["reopened"])
        self.assertEqual(guard["guard_ids"], rejections[0]["detail"]["guard_ids"])
        self.assertEqual(
            "worker rewrote this\n", self.protected.read_text(encoding="utf-8")
        )

    def test_harness_exception_still_adjudicates_protected_paths(self) -> None:
        """A crashing turn still answers the operator's standing instruction.

        The exception cleanup is the last place the run's guards are still
        alive, so the verdict is reached there or never. The exception stays
        the outcome -- it is re-raised unchanged -- and the verdict rides
        beside it in the event stream.
        """
        run_id = "guard-protected-exception"
        self._enqueue_guard(run_id, ".:notes.md")

        def edit_then_raise(*_args: object, **_kwargs: object) -> Any:
            self.protected.write_text("worker rewrote this\n", encoding="utf-8")
            raise RuntimeError("synthetic harness crash")

        with (
            mock.patch.object(harnesses, "run_harness", side_effect=edit_then_raise),
            self.assertRaisesRegex(RuntimeError, "synthetic harness crash"),
        ):
            self._invoke(run_id)

        rejections = self._turn_rejections("protected-path-guard")
        self.assertEqual(1, len(rejections))
        rejection = rejections[0]
        self.assertEqual(
            events.PAUSE_REASON_GUARD_PROTECTED_PATH_CHANGED, rejection["reason"]
        )
        self.assertFalse(rejection["reopened"])
        self.assertTrue(rejection["detail"]["adjudicated_only"])
        self.assertEqual(
            [".:notes.md"],
            sorted(rejection["detail"]["matched_paths"].values())[0],
        )
        self.assertEqual(
            rejection["detail"]["guard_ids"],
            list(rejection["detail"]["matched_paths"]),
        )
        self.assertEqual(
            "worker rewrote this\n", self.protected.read_text(encoding="utf-8")
        )

    def test_legacy_review_exception_still_adjudicates_protected_paths(self) -> None:
        """A crashing legacy review is adjudicated like any other crashing turn.

        It captures no write-guard snapshot, so before this it entered the
        harness with no exception context at all and the crash path skipped the
        protected-path guard entirely -- the guard then expired with the
        operator's standing instruction never answered. Its turn-start review
        fingerprint is the comparison that answers it.
        """
        self._use_legacy_review_fixture()
        run_id = "guard-protected-legacy-review-exception"
        self._enqueue_guard(run_id, ".:notes.md")

        def edit_then_raise(*_args: object, **_kwargs: object) -> Any:
            self.protected.write_text("review rewrote this\n", encoding="utf-8")
            raise RuntimeError("synthetic review crash")

        with (
            mock.patch.object(harnesses, "run_harness", side_effect=edit_then_raise),
            self.assertRaisesRegex(RuntimeError, "synthetic review crash"),
        ):
            self._invoke(run_id)

        rejections = self._turn_rejections("protected-path-guard")
        self.assertEqual(1, len(rejections))
        rejection = rejections[0]
        self.assertEqual(
            events.PAUSE_REASON_GUARD_PROTECTED_PATH_CHANGED, rejection["reason"]
        )
        self.assertTrue(rejection["detail"]["adjudicated_only"])
        self.assertFalse(rejection["reopened"])
        self.assertEqual(
            [".:notes.md"],
            sorted(rejection["detail"]["matched_paths"].values())[0],
        )
        self.assertEqual(
            rejection["detail"]["guard_ids"],
            list(rejection["detail"]["matched_paths"]),
        )
        # The review's edit is retained, exactly as it is for an implementation
        # turn: the guard decides what gets published, not what survives.
        self.assertEqual(
            "review rewrote this\n", self.protected.read_text(encoding="utf-8")
        )

    def test_legacy_review_exception_without_evidence_records_unavailable(
        self,
    ) -> None:
        """Unreadable post-crash evidence is a verdict, not a silent clearance.

        The recapture is what names the changed paths here, so a recapture that
        cannot answer must leave the guard explicitly unavailable rather than
        clear -- the failure mode the finding calls out is a frozen path with
        neither a violation nor an unavailable verdict.
        """
        self._use_legacy_review_fixture()
        run_id = "guard-protected-legacy-review-blind"
        self._enqueue_guard(run_id, ".:notes.md")

        def edit_then_raise(*_args: object, **_kwargs: object) -> Any:
            self.protected.write_text("review rewrote this\n", encoding="utf-8")
            raise RuntimeError("synthetic review crash")

        real_capture = review_guard._capture_review_repo_fingerprint
        captures: list[object] = []

        def capture_then_fail(*args: object, **kwargs: object) -> Any:
            # Only the post-crash recapture fails; the turn-start capture has
            # to succeed or the turn never reaches the harness at all.
            captures.append(args)
            if len(captures) == 1:
                return real_capture(*args, **kwargs)
            raise repository_evidence.RepositoryEvidenceError("git is gone")

        with (
            mock.patch.object(harnesses, "run_harness", side_effect=edit_then_raise),
            mock.patch.object(
                review_guard,
                "_capture_review_repo_fingerprint",
                side_effect=capture_then_fail,
            ),
            self.assertRaisesRegex(RuntimeError, "synthetic review crash"),
        ):
            self._invoke(run_id)

        self.assertEqual(2, len(captures))

        rejections = self._turn_rejections("protected-path-guard")
        self.assertEqual(1, len(rejections))
        self.assertEqual(
            events.PAUSE_REASON_GUARD_PROTECTED_PATH_UNAVAILABLE,
            rejections[0]["reason"],
        )
        self.assertEqual(
            "review rewrote this\n", self.protected.read_text(encoding="utf-8")
        )


class WorkspaceGuardExceptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.workspace_root = self.tmp_path / "workspace"
        self.repositories = init_multi_repo_workspace(
            self.workspace_root,
            DEFAULT_MULTI_REPO_SPECS[:2],
        )
        self.plan_dir = self.tmp_path / "plan"
        self.state_dir = self.tmp_path / "state"
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
        session_path = self.plan_dir / "session-1.md"
        session_path.write_text(
            session_path.read_text(encoding="utf-8").replace(
                "**Reads repositories**: SVC, OAK\n**Writes repositories**: SVC, OAK",
                "**Reads repositories**: SVC\n**Writes repositories**: SVC",
            ),
            encoding="utf-8",
        )
        registry = plans.read_repository_registry(self.plan_dir)
        assert registry is not None
        plans.save_workspace_plan_metadata(
            self.plan_dir,
            self.workspace_root,
            registry.digest,
        )
        self.context = verification.VerificationContext.from_plan(
            self.plan_dir,
            self.workspace_root,
        )

    def _args(self) -> argparse.Namespace:
        return argparse.Namespace(
            plan_dir=str(self.plan_dir),
            cwd=str(self.workspace_root),
            harness="codex",
            state_dir=str(self.state_dir),
            json=True,
            dry_run=False,
            unsafe_bypass=True,
            timeout=None,
            max_sessions=None,
            commit_mode=None,
            model=None,
            effort=None,
            note=None,
            allow_not_loop_ready=True,
        )

    def _invoke(self, run_id: str) -> tuple[int, dict[str, Any]]:
        lock_info = state.acquire_lock(self.plan_dir, self.state_dir)
        try:
            return run_loop.run_locked(
                args=self._args(),
                cwd=self.workspace_root,
                timeout=None,
                max_sessions=None,
                commit_mode=None,
                lock_info=lock_info,
                run_id=run_id,
                verification_context=self.context,
            )
        finally:
            state.release_lock(self.plan_dir, self.state_dir)

    def _pause_reasons(self) -> list[str]:
        event_path = (
            state.plan_state_dir(self.plan_dir, self.state_dir)
            / events.EVENTS_FILE_NAME
        )
        return [
            record["payload"]["reason"]
            for record in read_jsonl(event_path)
            if record["name"] == events.RUN_PAUSED_EVENT
        ]

    def _completed_harness_result(
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

    def test_harness_exception_records_scope_violation_and_blocks_rerun(
        self,
    ) -> None:
        target = self.repositories["OAK"] / "README.md"

        def mutate_then_raise(*_args: object, **_kwargs: object) -> None:
            target.write_text("retained unexpected mutation\n", encoding="utf-8")
            raise RuntimeError("synthetic harness crash")

        with (
            mock.patch.object(
                harnesses,
                "run_harness",
                side_effect=mutate_then_raise,
            ),
            self.assertRaisesRegex(RuntimeError, "synthetic harness crash"),
        ):
            self._invoke("exception-run")

        event_path = (
            state.plan_state_dir(self.plan_dir, self.state_dir)
            / events.EVENTS_FILE_NAME
        )
        pause_records = [
            record
            for record in read_jsonl(event_path)
            if record["name"] == events.RUN_PAUSED_EVENT
        ]
        self.assertEqual(1, len(pause_records))
        pause = pause_records[0]["payload"]
        self.assertEqual(
            events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
            pause["reason"],
        )
        guard = pause["details"]["workspace_write_guard"]
        self.assertEqual(
            ["OAK:README.md"],
            guard["violations"][0]["qualified_paths"],
        )
        self.assertIn("snapshot_before", guard)

        with mock.patch.object(
            harnesses,
            "run_harness",
            side_effect=AssertionError("rerun must stop before the harness"),
        ):
            exit_code, payload = self._invoke("blocked-rerun")

        self.assertEqual(1, exit_code)
        self.assertEqual(
            events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
            payload["pause_reason"],
        )
        self.assertEqual([], payload["turns"])

    def test_unregistered_sibling_write_does_not_pause_the_run(self) -> None:
        sibling = self.workspace_root / "unrelated-active-work"
        init_git_repo(sibling)

        def mutate_sibling(*args: object, **kwargs: object) -> harnesses.HarnessResult:
            (sibling / "concurrent.txt").write_text(
                "another run is working here\n", encoding="utf-8"
            )
            return self._completed_harness_result(*args, **kwargs)

        with mock.patch.object(
            harnesses,
            "run_harness",
            side_effect=mutate_sibling,
        ):
            exit_code, payload = self._invoke("collateral-write-run")

        self.assertEqual(0, exit_code)
        self.assertEqual([], self._pause_reasons())
        self.assertTrue(payload["turns"][0]["workspace_write_guard"]["passed"])
        self.assertIn(
            "- **Status**: complete",
            (self.plan_dir / "session-1.md").read_text(encoding="utf-8"),
        )
        self.assertTrue((sibling / "concurrent.txt").is_file())

    def test_registered_repository_removal_still_pauses(self) -> None:
        removed = self.repositories["OAK"]

        def remove_then_complete(
            *args: object, **kwargs: object
        ) -> harnesses.HarnessResult:
            shutil.rmtree(removed)
            return self._completed_harness_result(*args, **kwargs)

        with mock.patch.object(
            harnesses,
            "run_harness",
            side_effect=remove_then_complete,
        ):
            exit_code, payload = self._invoke("removed-repository-run")

        self.assertEqual(1, exit_code)
        self.assertEqual(
            events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
            payload["pause_reason"],
        )
        guard = payload["turns"][0]["workspace_write_guard"]
        self.assertEqual(["tundra_OAK"], guard["removed"])
        self.assertEqual("repository-removed", guard["violations"][0]["kind"])
        self.assertIn(
            "- **Status**: pending",
            (self.plan_dir / "session-1.md").read_text(encoding="utf-8"),
        )

    def test_registered_repository_symlink_swap_still_pauses(self) -> None:
        registered = self.repositories["OAK"]
        twin = self.tmp_path / "oak-twin"
        subprocess.run(
            ["git", "clone", str(registered), str(twin)],
            text=True,
            capture_output=True,
            check=True,
        )

        def swap_then_complete(
            *args: object, **kwargs: object
        ) -> harnesses.HarnessResult:
            shutil.rmtree(registered)
            registered.symlink_to(twin)
            return self._completed_harness_result(*args, **kwargs)

        with mock.patch.object(
            harnesses,
            "run_harness",
            side_effect=swap_then_complete,
        ):
            exit_code, payload = self._invoke("symlinked-repository-run")

        self.assertEqual(1, exit_code)
        self.assertEqual(
            events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
            payload["pause_reason"],
        )
        guard = payload["turns"][0]["workspace_write_guard"]
        self.assertEqual(["tundra_OAK"], guard["removed"])
        self.assertEqual("repository-removed", guard["violations"][0]["kind"])
        self.assertTrue(registered.is_symlink())
        self.assertIn(
            "- **Status**: pending",
            (self.plan_dir / "session-1.md").read_text(encoding="utf-8"),
        )

    def test_post_harness_system_exit_records_scope_violation(self) -> None:
        target = self.repositories["OAK"] / "README.md"

        def mutate_then_exit(*_args: object, **_kwargs: object) -> None:
            target.write_text("retained system-exit mutation\n", encoding="utf-8")
            raise SystemExit(9)

        with (
            mock.patch.object(
                harnesses,
                "run_harness",
                side_effect=self._completed_harness_result,
            ),
            mock.patch.object(
                turn_execution,
                "write_turn_artifacts",
                side_effect=mutate_then_exit,
            ),
            self.assertRaises(SystemExit) as raised,
        ):
            self._invoke("system-exit-run")

        self.assertEqual(9, raised.exception.code)
        self.assertIn(
            events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
            self._pause_reasons(),
        )
        self.assertIn(
            "- **Status**: pending",
            (self.plan_dir / "session-1.md").read_text(encoding="utf-8"),
        )

    def test_post_harness_keyboard_interrupt_records_scope_violation(self) -> None:
        target = self.repositories["OAK"] / "README.md"

        def mutate_then_interrupt(*_args: object, **_kwargs: object) -> None:
            target.write_text("retained interrupted mutation\n", encoding="utf-8")
            raise KeyboardInterrupt

        with (
            mock.patch.object(
                harnesses,
                "run_harness",
                side_effect=self._completed_harness_result,
            ),
            mock.patch.object(
                turn_execution,
                "write_turn_artifacts",
                side_effect=mutate_then_interrupt,
            ),
        ):
            exit_code, payload = self._invoke("keyboard-interrupt-run")

        self.assertEqual(1, exit_code)
        self.assertEqual(
            events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
            payload["pause_reason"],
        )
        self.assertNotEqual("interrupted", payload["pause_reason"])
        self.assertIn(
            "- **Status**: pending",
            (self.plan_dir / "session-1.md").read_text(encoding="utf-8"),
        )

    def test_scope_repair_requirement_gates_named_later_session(self) -> None:
        specs = DEFAULT_MULTI_REPO_SPECS[:2]
        contracts_by_id = {
            session_id: _contract_block(
                '{"type": "file_exists", "repo": "SVC", '
                '"path": "README.md", "name": "svc-readme"}',
                session=session_id,
            )
            for session_id in ("1", "2")
        }
        write_multi_repo_plan(
            self.plan_dir,
            ("pending", "pending"),
            specs=specs,
            contracts_by_id=contracts_by_id,
        )
        for session_id in ("1", "2"):
            session_path = self.plan_dir / f"session-{session_id}.md"
            session_path.write_text(
                session_path.read_text(encoding="utf-8").replace(
                    "**Reads repositories**: SVC, OAK\n"
                    "**Writes repositories**: SVC, OAK",
                    "**Reads repositories**: SVC\n**Writes repositories**: SVC",
                ),
                encoding="utf-8",
            )
        registry = plans.read_repository_registry(self.plan_dir)
        assert registry is not None
        plans.save_workspace_plan_metadata(
            self.plan_dir,
            self.workspace_root,
            registry.digest,
        )
        self.context = verification.VerificationContext.from_plan(
            self.plan_dir,
            self.workspace_root,
        )

        reached: list[str] = []

        def complete_session(name: str, **kwargs: object) -> harnesses.HarnessResult:
            prompt = str(kwargs.get("prompt", ""))
            session_id = "2" if "session-2.md" in prompt else "1"
            reached.append(session_id)
            if session_id == "2":
                (self.repositories["OAK"] / "README.md").write_text(
                    "retained out-of-scope mutation\n",
                    encoding="utf-8",
                )
            session_path = self.plan_dir / f"session-{session_id}.md"
            session_path.write_text(
                session_path.read_text(encoding="utf-8").replace(
                    "- **Status**: pending", "- **Status**: complete", 1
                ),
                encoding="utf-8",
            )
            return harnesses.HarnessResult(
                name=name,
                argv=["fake"],
                returncode=0,
                stdout="ok\n",
                stderr="",
            )

        with mock.patch.object(
            harnesses,
            "run_harness",
            side_effect=complete_session,
        ):
            first_exit, first_payload = self._invoke("scope-run-1")

        self.assertEqual(1, first_exit)
        self.assertEqual(["1", "2"], reached)
        self.assertEqual(
            events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
            first_payload["pause_reason"],
        )

        first_session = self.plan_dir / "session-1.md"
        first_session.write_text(
            first_session.read_text(encoding="utf-8").replace(
                "- **Status**: complete", "- **Status**: pending", 1
            ),
            encoding="utf-8",
        )
        reached.clear()

        with mock.patch.object(
            harnesses,
            "run_harness",
            side_effect=complete_session,
        ):
            second_exit, second_payload = self._invoke("scope-run-2")

        self.assertEqual(1, second_exit)
        self.assertEqual(["1"], reached)
        self.assertEqual(
            events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
            second_payload["pause_reason"],
        )


class SettledPauseCommitGateTests(unittest.TestCase):
    """The startup commit gate on the run a settled pause hands over to.

    The arrangement is the one the settlement exists for: one session whose
    verification fails its first attempt and passes every one after it, two
    ``run_locked`` passes over the same plan and the same state, and no human
    between them. What the first pass leaves in the worktree is the only thing
    that decides whether the second can start at all, so both outcomes are
    pinned here -- the clean handover the settlement is meant to produce, and
    the honest refusal that still happens when something the commit could not
    stage stayed behind.
    """

    SPECS = DEFAULT_MULTI_REPO_SPECS[:2]

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.workspace_root = self.tmp_path / "workspace"
        self.repositories = init_multi_repo_workspace(self.workspace_root, self.SPECS)
        self.plan_dir = self.tmp_path / "plan"
        self.state_dir = self.tmp_path / "state"
        self.commit_env = fake_commit_harness_env(self.tmp_path)
        self.marker = self.tmp_path / "verification-attempted"
        self.turns = 0

    def _write_plan(self, *, dirt: Path | None = None) -> None:
        script = self.tmp_path / "verify-once.sh"
        lines = ["#!/bin/sh"]
        if dirt is not None:
            # Verification runs after the post-turn write guard, so a check
            # that writes into a registered repository leaves a path no guard
            # evidence names -- and therefore one no staging derived from that
            # evidence can take with it.
            lines.append(f': > "{dirt}"')
        lines.extend(
            [
                # Fails its first run, passes every one after it: the pause the
                # first pass records is real, and the second pass is stopped by
                # the worktree or by nothing.
                f'if [ -f "{self.marker}" ]; then exit 0; fi',
                f': > "{self.marker}"',
                "exit 1",
                "",
            ]
        )
        script.write_text("\n".join(lines), encoding="utf-8")
        check = json.dumps(
            {
                "type": "command",
                "name": "verify-once",
                "repo": "SVC",
                "argv": ["sh", str(script)],
                "timeout_seconds": 60,
            }
        )
        write_multi_repo_plan(
            self.plan_dir,
            ("pending",),
            specs=self.SPECS,
            contracts_by_id={"1": _contract_block(check, session="1")},
        )
        registry = plans.read_repository_registry(self.plan_dir)
        assert registry is not None
        plans.save_workspace_plan_metadata(
            self.plan_dir,
            self.workspace_root,
            registry.digest,
        )
        self.context = verification.VerificationContext.from_plan(
            self.plan_dir,
            self.workspace_root,
        )

    def _work(self, name: str, **_kwargs: Any) -> harnesses.HarnessResult:
        self.turns += 1
        (self.repositories["SVC"] / "worked.txt").write_text(
            f"attempt {self.turns}\n", encoding="utf-8"
        )
        session_path = self.plan_dir / "session-1.md"
        session_path.write_text(
            session_path.read_text(encoding="utf-8").replace(
                "- **Status**: pending", "- **Status**: complete", 1
            ),
            encoding="utf-8",
        )
        return harnesses.HarnessResult(
            name=name, argv=["fake"], returncode=0, stdout="ok\n", stderr=""
        )

    def _refuse(self, name: str, **_kwargs: Any) -> harnesses.HarnessResult:
        raise AssertionError(f"the harness must not be reached; got {name}")

    def _invoke(self, run_id: str, harness: Any) -> tuple[int, dict[str, Any]]:
        args = argparse.Namespace(
            plan_dir=str(self.plan_dir),
            cwd=str(self.workspace_root),
            harness="codex",
            state_dir=str(self.state_dir),
            json=True,
            dry_run=False,
            unsafe_bypass=True,
            timeout=None,
            max_sessions=None,
            commit_mode=run_selection.COMMIT_MODE_PER_SESSION,
            model=None,
            effort=None,
            note=None,
            allow_not_loop_ready=True,
        )
        lock_info = state.acquire_lock(self.plan_dir, self.state_dir)
        try:
            with (
                patched_env(**self.commit_env),
                mock.patch.object(harnesses, "run_harness", side_effect=harness),
            ):
                return run_loop.run_locked(
                    args=args,
                    cwd=self.workspace_root,
                    timeout=None,
                    max_sessions=None,
                    commit_mode=run_selection.COMMIT_MODE_PER_SESSION,
                    lock_info=lock_info,
                    run_id=run_id,
                    verification_context=self.context,
                )
        finally:
            state.release_lock(self.plan_dir, self.state_dir)

    def _git(self, alias: str, *args: str) -> str:
        return git_stdout(self.repositories[alias], *args)

    def _session_status(self) -> str:
        for line in (
            (self.plan_dir / "session-1.md").read_text(encoding="utf-8").splitlines()
        ):
            if line.startswith("- **Status**:"):
                return line.split(":", 1)[1].strip()
        self.fail("session-1.md declares no status")

    def _pause_reasons(self) -> list[str]:
        event_path = (
            state.plan_state_dir(self.plan_dir, self.state_dir)
            / events.EVENTS_FILE_NAME
        )
        return [
            record["payload"]["reason"]
            for record in read_jsonl(event_path)
            if record["name"] == events.RUN_PAUSED_EVENT
        ]

    def test_commit_preflight_accepts_a_relaunch_after_the_prior_run_settled(
        self,
    ) -> None:
        self._write_plan()

        exit_code, first = self._invoke("settled-run-1", self._work)

        self.assertEqual(1, exit_code)
        self.assertEqual("verify-failed", first["pause_reason"])
        self.assertTrue(first["pause_policy"]["recoverable"])
        self.assertEqual("pending", self._session_status())
        # The settlement took everything the turn retained, in the one
        # repository the turn wrote to, and admitted no residue.
        record = first["paused_work_commit"]
        self.assertEqual("committed", record["result"])
        self.assertEqual([], record["residue"]["repositories"])
        for alias in ("SVC", "OAK"):
            self.assertEqual("", self._git(alias, "status", "--porcelain"))

        exit_code, second = self._invoke("settled-run-2", self._work)

        # Gate A ran, passed, and recorded every registered repository as
        # clean -- the record is what a supervisor reads to know the handover
        # was clean rather than merely unblocked.
        preflight = second["commit_preflight"]
        self.assertEqual(
            ["SVC", "OAK"], [row["alias"] for row in preflight["repositories"]]
        )
        self.assertTrue(all(row["clean"] for row in preflight["repositories"]))
        self.assertEqual(0, exit_code)
        self.assertFalse(second["paused"])
        self.assertNotIn("commit-preflight", self._pause_reasons())
        # And the relaunch spent the repair attempt the first pause earned.
        self.assertEqual("1", second["turns"][0]["session_id"])
        self.assertEqual("complete", self._session_status())

    def test_a_settled_pause_that_left_residue_still_pauses_preflight_and_names_it(
        self,
    ) -> None:
        residue_path = self.repositories["SVC"] / "check-artifact.log"
        self._write_plan(dirt=residue_path)

        exit_code, first = self._invoke("residue-run-1", self._work)

        self.assertEqual(1, exit_code)
        self.assertEqual("verify-failed", first["pause_reason"])
        # The commit landed -- the turn's own work is in the history -- but a
        # worktree still dirty afterwards is what ``run_commit`` calls an
        # unsuccessful commit, so the settlement fails and the group stays
        # owed. That verdict is not softened here; what the record adds is the
        # residue, re-probed rather than assumed away by the failure.
        record = first["paused_work_commit"]
        self.assertEqual("failed", record["result"])
        self.assertEqual("dirty-after-commit", record["commit"]["status_result"])
        self.assertEqual(
            [
                {
                    "alias": "SVC",
                    "clean": False,
                    "dirty_paths": ["SVC:check-artifact.log"],
                }
            ],
            record["residue"]["repositories"],
        )
        self.assertEqual(
            "worked.txt", self._git("SVC", "show", "--name-only", "--format=")
        )
        self.assertEqual(
            "?? check-artifact.log", self._git("SVC", "status", "--porcelain")
        )
        # The failure was absorbed rather than paused on, and it left the run
        # ending with the paused-work group still unresolved.
        self.assertEqual(
            [commit_groups.GROUP_SCOPE_SESSION_PAUSED],
            [row["scope"] for row in first["commit_failures"]],
        )
        self.assertEqual(
            [(commit_groups.GROUP_SCOPE_SESSION_PAUSED, "failed")],
            [
                (row["scope"], row["status"])
                for row in first["unresolved_commit_groups"]
            ],
        )

        exit_code, second = self._invoke("residue-run-2", self._refuse)

        # The gate is not taught to tolerate what the settlement could not
        # take: dirt it did not author is still dirt, and the run stops before
        # a turn. What the fix buys here is the naming, not the acceptance --
        # the prior run's record already says which paths these are.
        self.assertEqual(1, exit_code)
        self.assertEqual("commit-preflight", second["pause_reason"])
        self.assertEqual([], second["turns"])
        self.assertNotIn("commit_preflight", second)
        self.assertIn("SVC", second["pause_message"])
        self.assertEqual(
            ["SVC:check-artifact.log"],
            [
                path
                for row in record["residue"]["repositories"]
                for path in row["dirty_paths"]
            ],
        )
        self.assertEqual(
            "?? check-artifact.log", self._git("SVC", "status", "--porcelain")
        )


class RepairChildWriteScopeRegenerationTests(unittest.TestCase):
    """Widen a materialized repair child whose findings were recomputed."""

    SPECS = DEFAULT_MULTI_REPO_SPECS[:2]

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.workspace_root = self.tmp_path / "workspace"
        init_multi_repo_workspace(self.workspace_root, self.SPECS)
        self.plan_dir = self.tmp_path / "plan"
        self.state_dir = self.tmp_path / "state"

    def _finding(
        self,
        finding_id: str,
        repair_group: str,
        repo: str,
        *,
        evidence_repos: tuple[str, ...],
    ) -> dict[str, Any]:
        return _structured_review_finding(
            finding_id=finding_id,
            repair_group=repair_group,
            repo=repo,
            evidence=[
                {"path": f"{alias}:pkg/{finding_id}.py", "symbol": "target"}
                for alias in evidence_repos
            ],
            verification_checks=[
                {
                    "type": "command",
                    "name": f"verify-{finding_id}",
                    "repo": repo,
                    "argv": ["python3", "-c", "pass"],
                }
            ],
        )

    def _register_plan(self, findings: list[dict[str, Any]]) -> None:
        write_completed_review_plan(self.plan_dir, findings)
        rows = "\n".join(
            f"| {alias} | {path} | {origin} |" for alias, path, origin in self.SPECS
        )
        (self.plan_dir / "manifest.md").write_text(
            "# Manifest\n\n## Repositories\n\n"
            "| Alias | Path | Origin |\n"
            "| --- | --- | --- |\n"
            f"{rows}\n",
            encoding="utf-8",
        )
        for session_id, writes in (("1", "SVC, OAK"), ("2", "(none)")):
            path = self.plan_dir / f"session-{session_id}.md"
            heading, remainder = path.read_text(encoding="utf-8").split("\n", 1)
            path.write_text(
                (
                    f"{heading}\n\n**Reads repositories**: SVC, OAK\n"
                    f"**Writes repositories**: {writes}\n{remainder}"
                ).replace(
                    "- **Files modified**: (none yet)",
                    "- **Files modified**: (none)",
                ),
                encoding="utf-8",
            )
        self._write_artifact(findings)
        registry = plans.read_repository_registry(self.plan_dir)
        assert registry is not None
        plans.save_workspace_plan_metadata(
            self.plan_dir,
            self.workspace_root,
            registry.digest,
        )

    def _write_artifact(self, findings: list[dict[str, Any]]) -> None:
        (self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
            json.dumps(
                {
                    "version": review.REPOSITORY_AWARE_REVIEW_ARTIFACT_VERSION,
                    "outcome": "findings",
                    "findings": findings,
                }
            ),
            encoding="utf-8",
        )

    def _materialize_child(self, findings: list[dict[str, Any]]) -> Path:
        self._register_plan(findings)
        result = review_repair.materialize_review_repair_sessions(
            self.plan_dir,
            state_dir=self.state_dir,
        )
        self.assertTrue(result.ok, result.error)
        return self.plan_dir / "session-2.1.md"

    def _child_status(self) -> plans.SessionStatus:
        for session in plans.discover_session_files(self.plan_dir):
            if session.id == "2.1":
                return plans.read_session_status(session)
        raise AssertionError("the repair child was not materialized")

    def _regenerate(self) -> dict[str, Any] | None:
        context = verification.VerificationContext.from_plan(
            self.plan_dir,
            self.workspace_root,
        )
        workspace = context.workspace
        assert workspace is not None
        status = self._child_status()
        return workspace_evidence._regenerated_repair_child_write_scope(
            session=status,
            plan_dir=self.plan_dir,
            workspace=workspace,
            scope=plans.parse_session_scope(
                status.path.read_text(encoding="utf-8"),
                workspace.registry.aliases,
            ),
        )

    def _narrow(self, child: Path) -> str:
        """Persist the scope a child materialized before the evidence widened."""
        materialized = child.read_text(encoding="utf-8")
        self.assertIn("**Writes repositories**: SVC, OAK", materialized)
        child.write_text(
            materialized.replace(
                "**Writes repositories**: SVC, OAK",
                "**Writes repositories**: OAK",
            ),
            encoding="utf-8",
        )
        return materialized

    def test_narrowed_child_is_widened_to_what_its_findings_require(self) -> None:
        child = self._materialize_child(
            [
                self._finding(
                    "F1",
                    "manifest-contract",
                    "OAK",
                    evidence_repos=("OAK", "SVC"),
                )
            ]
        )
        materialized = self._narrow(child)

        record = self._regenerate()

        self.assertEqual(
            {
                "session_id": "2.1",
                "repair_group": "manifest-contract",
                "durable_write_repositories": ["OAK"],
                "required_write_repositories": ["SVC", "OAK"],
                "added_write_repositories": ["SVC"],
                "write_repositories": ["SVC", "OAK"],
                "read_repositories": ["SVC", "OAK"],
            },
            record,
        )
        # Rewriting only the structural scope fields restores the child the
        # authoring path would render today, byte for byte.
        self.assertEqual(materialized, child.read_text(encoding="utf-8"))
        self.assertIsNone(self._regenerate())

    def test_child_wider_than_its_findings_is_never_narrowed(self) -> None:
        child = self._materialize_child(
            [self._finding("F1", "svc-only", "SVC", evidence_repos=("SVC",))]
        )
        materialized = child.read_text(encoding="utf-8")
        self.assertIn("**Writes repositories**: SVC", materialized)
        widened = materialized.replace(
            "**Writes repositories**: SVC",
            "**Writes repositories**: SVC, OAK",
        )
        child.write_text(widened, encoding="utf-8")

        self.assertIsNone(self._regenerate())
        self.assertEqual(widened, child.read_text(encoding="utf-8"))

    def test_human_authored_session_is_never_rewritten(self) -> None:
        child = self._materialize_child(
            [
                self._finding(
                    "F1",
                    "manifest-contract",
                    "OAK",
                    evidence_repos=("OAK", "SVC"),
                )
            ]
        )
        self._narrow(child)
        # Everything a generated child carries except the markers, so the
        # refusal can only come from the generated-session predicate.
        human_authored = "\n".join(
            line
            for line in child.read_text(encoding="utf-8").split("\n")
            if "asl:generated-review-repair-session" not in line
        )
        child.write_text(human_authored, encoding="utf-8")

        self.assertIsNone(self._regenerate())
        self.assertEqual(human_authored, child.read_text(encoding="utf-8"))

    def test_group_the_artifact_no_longer_carries_leaves_the_child_alone(
        self,
    ) -> None:
        finding = self._finding(
            "F1",
            "manifest-contract",
            "OAK",
            evidence_repos=("OAK", "SVC"),
        )
        child = self._materialize_child([finding])
        self._narrow(child)
        narrowed = child.read_text(encoding="utf-8")
        self._write_artifact([{**finding, "repair_group": "renamed-group"}])

        self.assertIsNone(self._regenerate())
        self.assertEqual(narrowed, child.read_text(encoding="utf-8"))


class RepairScopeRegenerationOrderSourceTests(unittest.TestCase):
    """Regeneration widens the durable file before anything reads it.

    Ordering is the whole contract. The durable child file is what commit
    preflight, the task packet, the persisted prompt and the write guard each
    read, so a rewrite landing after any of them leaves that reader describing
    a scope the run no longer uses -- and a start-time override of the value
    alone would leave the file itself disagreeing with all four.
    """

    # Every module the turn path has been sliced into. The contract is an
    # ordering one, and raw line numbers stop being comparable the moment two
    # ordered steps live in different files -- so each step is located by
    # search and ordered by where the loop reaches its owner, which keeps the
    # contract intact as the extraction keeps carving the loop up.
    TURN_PATH_MODULES = (
        run_loop,
        turn_preparation,
        turn_preflight,
        turn_postflight,
        turn_settlement,
    )

    def setUp(self) -> None:
        self.trees = {
            module.__name__.rsplit(".", 1)[-1]: ast.parse(
                Path(module.__file__).read_text(encoding="utf-8")
            )
            for module in self.TURN_PATH_MODULES
        }
        self.tree = self.trees["run_loop"]
        self.preparation_tree = self.trees["turn_preparation"]
        self.run_locked = self._function("run_locked")
        self.entry_points = self._entry_points()
        self.resume_repair = self._function(
            "resume_repair_session", tree=self.preparation_tree
        )
        self.prepare_prompt = self._function(
            "prepare_turn_prompt", tree=self.preparation_tree
        )

    def _entry_points(self) -> dict[str, tuple[int, ast.FunctionDef]]:
        """The steps the loop delegates, keyed by where the loop reaches them.

        Discovered rather than listed: a step extracted into a new module
        rejoins the order by being called from the loop, so this contract does
        not have to be re-pointed at each module the turn path grows.
        """
        reached: dict[str, tuple[int, ast.FunctionDef]] = {}
        for module_name, tree in self.trees.items():
            if module_name == "run_loop":
                continue
            for node in ast.iter_child_nodes(tree):
                if not isinstance(node, ast.FunctionDef):
                    continue
                calls = self._call_lines(self.run_locked, node.name)
                if calls:
                    reached[node.name] = (min(calls), node)
        return reached

    def _owners(self) -> list[ast.FunctionDef]:
        return [self.run_locked, *(owner for _, owner in self.entry_points.values())]

    def _positions(
        self, lines_in: Callable[[ast.FunctionDef], list[int]]
    ) -> list[tuple[int, int]]:
        """Where the turn path performs something, in the loop's own order.

        A step the loop still performs itself sorts by its own line; a step it
        delegates sorts by where the loop reaches the owner, then by the line
        inside it. That is what makes two ordered steps comparable after one of
        them moves into a module of its own.
        """
        positions = [(line, -1) for line in lines_in(self.run_locked)]
        for loop_line, owner in self.entry_points.values():
            positions.extend((loop_line, line) for line in lines_in(owner))
        return sorted(positions)

    def _call_positions(self, name: str) -> list[tuple[int, int]]:
        return self._positions(lambda node: self._call_lines(node, name))

    def _function(self, name: str, *, tree: ast.AST | None = None) -> ast.FunctionDef:
        matches = [
            node
            for node in ast.walk(tree if tree is not None else self.tree)
            if isinstance(node, ast.FunctionDef) and node.name == name
        ]
        self.assertEqual(1, len(matches), f"expected one {name} definition")
        return matches[0]

    def _call_lines(self, node: ast.AST, name: str) -> list[int]:
        # A call site keeps its meaning when the callee is extracted into a
        # package module, so match `name(...)` and `module.name(...)` alike --
        # otherwise this ordering contract silently stops seeing a reader the
        # moment the symbol moves out of the runner.
        return sorted(
            call.lineno
            for call in ast.walk(node)
            if isinstance(call, ast.Call)
            and (
                (isinstance(call.func, ast.Name) and call.func.id == name)
                or (isinstance(call.func, ast.Attribute) and call.func.attr == name)
            )
        )

    def _turn_record_lines(self, node: ast.FunctionDef) -> list[int]:
        # The turn record is where the rendered prompt becomes durable state, so
        # pin the literal that carries it rather than every rebinding of the
        # name. Matched on the two keys that make it the turn record rather than
        # on the name it is bound to, which is the part that moves.
        return sorted(
            assign.lineno
            for assign in ast.walk(node)
            if isinstance(assign, ast.Assign | ast.AnnAssign)
            and isinstance(assign.value, ast.Dict)
            and {"prompt", "turn_index"}
            <= {key.value for key in assign.value.keys if isinstance(key, ast.Constant)}
        )

    def _turn_record_position(self) -> tuple[int, int]:
        positions = self._positions(self._turn_record_lines)
        self.assertEqual(1, len(positions), "expected one persisted turn record")
        return positions[0]

    def test_regeneration_precedes_every_reader_of_the_child_scope(self) -> None:
        # Inside the resumption: widen, re-read, then prove the tree is clean.
        (regenerated,) = self._call_lines(
            self.resume_repair, "_regenerated_repair_child_write_scope"
        )
        reread = self._call_lines(self.resume_repair, "_session_scope")
        (preflight_call,) = self._call_lines(
            self.resume_repair, "_repair_commit_preflight"
        )

        # The re-read is what makes the widened file the value in hand; every
        # other reader has to come after it, not merely after the rewrite.
        self.assertTrue(
            any(line > regenerated for line in reread),
            "the durable file must be re-read after it is widened",
        )
        self.assertLess(regenerated, preflight_call)

        # The packet is built inside the prompt step, so the loop's own call
        # order is what keeps it downstream of the widening.
        self.assertTrue(
            self._call_lines(self.prepare_prompt, "build_task_packet"),
            "the task packet must be built by the prompt step",
        )
        self.assertEqual(
            [],
            self._call_lines(self.run_locked, "build_task_packet"),
            "a second packet build in the loop would read its own scope",
        )

        # Across the turn path: resume first, then the prompt that builds the
        # packet, then the record that makes the prompt durable, then the guard.
        (resumed,) = self._call_positions("resume_repair_session")
        prompt = self._call_positions("prepare_turn_prompt")
        prompt_persisted = self._turn_record_position()
        guard = self._call_positions("_workspace_write_guard_evidence")

        self.assertTrue(guard, "expected a write guard reading the child scope")
        self.assertLess(resumed, min(prompt))
        self.assertLess(min(prompt), prompt_persisted)
        self.assertLess(prompt_persisted, min(guard))

    def test_the_retained_scope_branch_no_longer_widens_the_child_itself(self) -> None:
        branches = [
            node
            for owner in self._owners()
            for node in ast.walk(owner)
            if isinstance(node, ast.If)
            and ast.unparse(node.test) == "retained_scope_repair is not None"
            and self._call_lines(node, "_workspace_write_guard_evidence")
        ]

        self.assertEqual(1, len(branches), "expected one retained-scope-repair branch")
        self.assertEqual(
            [],
            self._call_lines(branches[0], "_regenerated_repair_child_write_scope"),
            "widening inside the guard branch is what F002 moved out of it",
        )

    def test_session_scope_is_only_ever_read_from_the_session_file(self) -> None:
        # The resumption is the one place allowed to hand the value back, and
        # it re-read the file to produce it, so taking `session_scope` off its
        # result is still the file's own value rather than a computed one.
        allowed = ("_session_scope(", ".session_scope")

        bound_anywhere = False
        for module_name, tree in self.trees.items():
            assignments = [
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Name) and target.id == "session_scope"
                    for target in node.targets
                )
            ]
            bound_anywhere = bound_anywhere or bool(assignments)
            for node in assignments:
                source = ast.unparse(node.value)
                self.assertTrue(
                    any(allowance in source for allowance in allowed),
                    "session_scope must stay the session file's own value; "
                    f"{module_name} line {node.lineno} computes one instead",
                )

        self.assertTrue(bound_anywhere, "expected the run loop to bind session_scope")


class _GeneratedRepairDispatchFixture(unittest.TestCase):
    """A completed multi-repository review whose repair children can be run.

    Both dispatch gates only exist inside a real ``run_locked`` turn, so these
    tests build the whole artifact chain -- registry, final review artifact,
    materialized children -- and drive the runner rather than the helpers.
    """

    SPECS = DEFAULT_MULTI_REPO_SPECS[:2]

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp_path = Path(tmp.name)
        self.workspace_root = self.tmp_path / "workspace"
        self.repositories = init_multi_repo_workspace(self.workspace_root, self.SPECS)
        self.plan_dir = self.tmp_path / "plan"
        self.state_dir = self.tmp_path / "state"
        self.child = self.plan_dir / "session-2.1.md"
        self.prompts: list[str] = []
        self.commit_env = fake_commit_harness_env(self.tmp_path)

    def _finding(
        self,
        evidence_aliases: tuple[str, ...],
        *,
        finding_id: str = "F1",
        repair_group: str = "manifest-contract",
        repo: str = "OAK",
    ) -> dict[str, Any]:
        return _structured_review_finding(
            finding_id=finding_id,
            repair_group=repair_group,
            repo=repo,
            evidence=[
                {"path": f"{alias}:pkg/{finding_id}.py", "symbol": "target"}
                for alias in evidence_aliases
            ],
            verification_checks=[
                {
                    "type": "command",
                    "name": f"verify-{finding_id}",
                    "repo": repo,
                    "argv": ["python3", "-c", "pass"],
                }
            ],
        )

    def _write_review_artifact(self, findings: list[dict[str, Any]]) -> None:
        counts = {
            severity: sum(1 for item in findings if item["severity"] == severity)
            for severity in ("high", "med", "low")
        }
        (self.plan_dir / review.FINAL_REVIEW_FILENAME).write_text(
            "# Final review\n\n"
            f"Status: {len(findings)} Findings ({counts['high']} high, "
            f"{counts['med']} med, {counts['low']} low)\n",
            encoding="utf-8",
        )
        (self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
            json.dumps(
                {
                    "version": review.REPOSITORY_AWARE_REVIEW_ARTIFACT_VERSION,
                    "outcome": "findings",
                    "findings": findings,
                }
            ),
            encoding="utf-8",
        )

    def _pin_workspace(self) -> None:
        registry = plans.read_repository_registry(self.plan_dir)
        self.assertIsNotNone(registry, "the plan must carry a repository registry")
        assert registry is not None
        plans.save_workspace_plan_metadata(
            self.plan_dir, self.workspace_root, registry.digest
        )

    def _materialize_children(self) -> None:
        result = review_repair.materialize_review_repair_sessions(
            self.plan_dir, state_dir=self.state_dir
        )
        self.assertTrue(result.ok, result.error)

    def _write_scope(self, path: Path) -> str:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("**Writes repositories**:"):
                return line.split(":", 1)[1].strip()
        self.fail(f"{path.name} declares no write scope")

    def _narrow(self, path: Path, aliases: str) -> None:
        text = path.read_text(encoding="utf-8")
        current = self._write_scope(path)
        path.write_text(
            text.replace(
                f"**Writes repositories**: {current}",
                f"**Writes repositories**: {aliases}",
                1,
            ),
            encoding="utf-8",
        )

    def _prompt_field(self, prompt: str, label: str) -> str:
        for line in prompt.splitlines():
            if line.startswith(f"- {label}:"):
                return line.split(":", 1)[1].strip()
        self.fail(f"the task packet carries no {label!r} line")

    def _record_prompt(self, name: str, **kwargs: Any) -> harnesses.HarnessResult:
        self.prompts.append(str(kwargs.get("prompt", "")))
        return harnesses.HarnessResult(
            name=name, argv=["fake"], returncode=0, stdout="ok\n", stderr=""
        )

    def _refuse(self, name: str, **kwargs: Any) -> harnesses.HarnessResult:
        raise AssertionError(f"the harness must not be reached; got {name}")

    def _event_path(self) -> Path:
        plan_state = state.plan_state_dir(self.plan_dir, self.state_dir)
        return plan_state / events.EVENTS_FILE_NAME

    def _pause_reasons(self) -> list[str]:
        event_path = self._event_path()
        if not event_path.exists():
            return []
        return [
            record["payload"]["reason"]
            for record in read_jsonl(event_path)
            if record["name"] == events.RUN_PAUSED_EVENT
        ]

    def _run(self, run_id: str, harness: Any) -> tuple[int, dict[str, Any]]:
        args = argparse.Namespace(
            plan_dir=str(self.plan_dir),
            cwd=str(self.workspace_root),
            harness="codex",
            state_dir=str(self.state_dir),
            json=True,
            dry_run=False,
            unsafe_bypass=True,
            timeout=None,
            max_sessions=None,
            commit_mode=None,
            model=None,
            effort=None,
            note=None,
            allow_not_loop_ready=True,
        )
        context = verification.VerificationContext.from_plan(
            self.plan_dir, self.workspace_root
        )
        lock_info = state.acquire_lock(self.plan_dir, self.state_dir)
        try:
            with (
                patched_env(**self.commit_env),
                mock.patch.object(harnesses, "run_harness", side_effect=harness),
            ):
                return run_loop.run_locked(
                    args=args,
                    cwd=self.workspace_root,
                    timeout=None,
                    max_sessions=None,
                    commit_mode=None,
                    lock_info=lock_info,
                    run_id=run_id,
                    verification_context=context,
                )
        finally:
            state.release_lock(self.plan_dir, self.state_dir)


class RepairScopeStaleDispatchTests(_GeneratedRepairDispatchFixture):
    """``repair-scope-stale`` gates the child before a turn is ever spent.

    The blocker used to be advisory: the continuation computed it, nothing
    consulted it before dispatch, and a child whose durable scope had been
    narrowed away from what its findings require ran anyway -- under the narrow
    scope, so its legitimate writes looked like violations.
    """

    def setUp(self) -> None:
        super().setUp()
        write_completed_review_plan(self.plan_dir, [self._finding(("OAK", "SVC"))])
        rows = "\n".join(
            f"| {alias} | {path} | {origin} |" for alias, path, origin in self.SPECS
        )
        (self.plan_dir / "manifest.md").write_text(
            "# Manifest\n\n## Repositories\n\n"
            "| Alias | Path | Origin |\n| --- | --- | --- |\n" + rows + "\n",
            encoding="utf-8",
        )
        for session_id, writes in (("1", "SVC, OAK"), ("2", "(none)")):
            path = self.plan_dir / f"session-{session_id}.md"
            heading, remainder = path.read_text(encoding="utf-8").split("\n", 1)
            path.write_text(
                (
                    f"{heading}\n\n**Reads repositories**: SVC, OAK\n"
                    f"**Writes repositories**: {writes}\n{remainder}"
                ).replace(
                    "- **Files modified**: (none yet)", "- **Files modified**: (none)"
                ),
                encoding="utf-8",
            )

    def _seed(self, findings: list[dict[str, Any]]) -> None:
        # Materialization is one-shot per review session, so every child a test
        # needs has to be present in the artifact before it runs.
        self._write_review_artifact(findings)
        self._pin_workspace()
        self._materialize_children()
        self.assertEqual("SVC, OAK", self._write_scope(self.child))

    def test_a_narrowed_child_runs_only_under_its_regenerated_scope(self) -> None:
        self._seed([self._finding(("OAK", "SVC"))])
        self._narrow(self.child, "OAK")

        _exit_code, payload = self._run("stale-widened", self._record_prompt)

        self.assertEqual("SVC, OAK", self._write_scope(self.child))
        self.assertEqual(1, len(self.prompts))
        # The packet the worker actually received is the proof: a child
        # dispatched under the stale scope would name OAK alone here.
        self.assertEqual(
            "SVC, OAK", self._prompt_field(self.prompts[0], "Writable aliases")
        )
        self.assertNotIn(
            events.PAUSE_REASON_MALFORMED_REVIEW_REPAIR, self._pause_reasons()
        )
        self.assertEqual("2.1", payload["turns"][0]["session_id"])

    def test_a_child_whose_scope_cannot_be_repaired_is_never_dispatched(self) -> None:
        self._seed([self._finding(("OAK", "SVC"))])
        self._narrow(self.child, "OAK")
        # Regeneration is fail-open, so a durable file the runner cannot rewrite
        # is the reachable case where the blocker has to stop the run itself.
        self.child.chmod(0o444)
        self.addCleanup(self.child.chmod, 0o644)

        exit_code, payload = self._run("stale-blocked", self._refuse)

        self.assertEqual(1, exit_code)
        self.assertEqual(
            events.PAUSE_REASON_MALFORMED_REVIEW_REPAIR, payload["pause_reason"]
        )
        self.assertEqual([], payload["turns"])
        self.assertEqual("OAK", self._write_scope(self.child))
        resume = payload["review_resume"]
        self.assertEqual("run-repairs", resume["action"])
        self.assertFalse(resume["eligible"])
        self.assertEqual("repair-scope-stale", resume["blocker"])
        (gap,) = resume["repair_scope_gaps"]
        self.assertEqual("2.1", gap["repair_session_id"])
        self.assertEqual(["SVC"], gap["missing_write_repositories"])
        self.assertEqual(["SVC", "OAK"], gap["required_write_repositories"])

    def test_a_stale_sibling_does_not_deadlock_the_child_that_can_run(self) -> None:
        # The projection is plan-wide, so the sibling's staleness blocks the
        # whole continuation even though the sibling is not the one dispatching.
        self._seed(
            [
                self._finding(("OAK", "SVC")),
                self._finding(
                    ("SVC", "OAK"),
                    finding_id="F2",
                    repair_group="docs-drift",
                    repo="SVC",
                ),
            ]
        )
        sibling = self.plan_dir / "session-2.2.md"
        self._narrow(sibling, "SVC")

        _exit_code, _payload = self._run("stale-sibling", self._record_prompt)

        self.assertEqual("SVC, OAK", self._write_scope(sibling))
        self.assertEqual(1, len(self.prompts))
        self.assertEqual(
            "SVC, OAK", self._prompt_field(self.prompts[0], "Writable aliases")
        )


class SettledPauseRepairPreflightTests(_GeneratedRepairDispatchFixture):
    """Gate B on the repair child a settled pause hands back to.

    A repair child owns its own commit because its parent's commit mode never
    reached it, which makes a recoverable pause mid-repair the case where
    retained work would otherwise sit in the tree until a human moved it. The
    redispatch runs the stricter of the two startup gates -- ``git status``
    with ``--untracked-files=all`` -- so this is the arrangement that says
    whether the settlement leaves a tree that gate accepts, and not merely one
    plain ``--porcelain`` reports clean.
    """

    def setUp(self) -> None:
        super().setUp()
        self.turns = 0
        self.marker = self.tmp_path / "repair-verification-attempted"
        self.script = self.tmp_path / "verify-once.sh"
        self.script.write_text(
            "\n".join(
                [
                    "#!/bin/sh",
                    # Fails the first attempt and passes every one after it, so
                    # the first run's pause is real and the second run is
                    # stopped by the worktree or by nothing.
                    f'if [ -f "{self.marker}" ]; then exit 0; fi',
                    f': > "{self.marker}"',
                    "exit 1",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        findings = [self._verify_once_finding()]
        write_completed_review_plan(self.plan_dir, findings)
        rows = "\n".join(
            f"| {alias} | {path} | {origin} |" for alias, path, origin in self.SPECS
        )
        (self.plan_dir / "manifest.md").write_text(
            "# Manifest\n\n## Repositories\n\n"
            "| Alias | Path | Origin |\n| --- | --- | --- |\n" + rows + "\n",
            encoding="utf-8",
        )
        for session_id, writes in (("1", "SVC, OAK"), ("2", "(none)")):
            path = self.plan_dir / f"session-{session_id}.md"
            heading, remainder = path.read_text(encoding="utf-8").split("\n", 1)
            path.write_text(
                (
                    f"{heading}\n\n**Reads repositories**: SVC, OAK\n"
                    f"**Writes repositories**: {writes}\n{remainder}"
                ).replace(
                    "- **Files modified**: (none yet)", "- **Files modified**: (none)"
                ),
                encoding="utf-8",
            )
        self._write_review_artifact(findings)
        self._pin_workspace()
        self._materialize_children()
        self.assertEqual("SVC, OAK", self._write_scope(self.child))

    def _verify_once_finding(self) -> dict[str, Any]:
        return _structured_review_finding(
            finding_id="F1",
            repair_group="manifest-contract",
            repo="OAK",
            evidence=[
                {"path": f"{alias}:pkg/F1.py", "symbol": "target"}
                for alias in ("OAK", "SVC")
            ],
            verification_checks=[
                {
                    "type": "command",
                    "name": "verify-F1",
                    "repo": "OAK",
                    "argv": ["sh", str(self.script)],
                    "timeout_seconds": 60,
                }
            ],
        )

    def _repair_turn(self, name: str, **kwargs: Any) -> harnesses.HarnessResult:
        self.prompts.append(str(kwargs.get("prompt", "")))
        self.turns += 1
        (self.repositories["OAK"] / "repaired.txt").write_text(
            f"attempt {self.turns}\n", encoding="utf-8"
        )
        self.child.write_text(
            self.child.read_text(encoding="utf-8").replace(
                "- **Status**: pending", "- **Status**: complete", 1
            ),
            encoding="utf-8",
        )
        return harnesses.HarnessResult(
            name=name, argv=["fake"], returncode=0, stdout="ok\n", stderr=""
        )

    def _git(self, alias: str, *args: str) -> str:
        return git_stdout(self.repositories[alias], *args)

    def test_repair_commit_preflight_accepts_a_child_whose_prior_pause_was_settled(
        self,
    ) -> None:
        exit_code, first = self._run("repair-settled-1", self._repair_turn)

        self.assertEqual(1, exit_code)
        self.assertEqual("verify-failed", first["pause_reason"])
        self.assertTrue(first["pause_policy"]["recoverable"])
        # The child is owed a commit even with no commit mode, and the
        # settlement took the work its turn retained before the run ended.
        record = first["paused_work_commit"]
        self.assertEqual("committed", record["result"])
        self.assertEqual([], record["residue"]["repositories"])
        self.assertIn("- **Status**: pending", self.child.read_text(encoding="utf-8"))
        for alias in ("SVC", "OAK"):
            self.assertEqual(
                "",
                self._git(alias, "status", "--porcelain", "--untracked-files=all"),
            )

        exit_code, second = self._run("repair-settled-2", self._repair_turn)

        # Gate B ran ahead of the redispatch and found every repository the
        # child may write to clean, with no sanction needed: the settlement
        # left nothing for the strict gate to forgive.
        (preflight,) = second["repair_commit_preflights"]
        self.assertEqual(
            ["SVC", "OAK"], [row["alias"] for row in preflight["repositories"]]
        )
        self.assertTrue(all(row["clean"] for row in preflight["repositories"]))
        self.assertIsNone(preflight.get("retained_scope_repair"))
        self.assertNotIn("commit-preflight", self._pause_reasons())
        self.assertEqual(0, exit_code)
        self.assertEqual("2.1", second["turns"][0]["session_id"])
        self.assertIn("- **Status**: complete", self.child.read_text(encoding="utf-8"))


class RepairScopeRegenerationIntegrationTests(_GeneratedRepairDispatchFixture):
    """Regeneration lands before preflight, the packet, the prompt and the guard.

    The scenario is the one the retention exists for: a child writes outside its
    declared scope, the runner keeps those changes and reverts the child so its
    scope can be widened, and the next run has to repair the durable file early
    enough that every downstream reader sees the widened value.
    """

    SPECS = DEFAULT_MULTI_REPO_SPECS[:3]

    def setUp(self) -> None:
        super().setUp()
        aliases = ", ".join(alias for alias, _path, _origin in self.SPECS)
        contracts = {
            session_id: _contract_block(
                '{"type": "file_exists", "repo": "SVC", '
                '"path": "README.md", "name": "svc-readme"}',
                session=session_id,
            )
            for session_id in ("1", "2")
        }
        write_multi_repo_plan(
            self.plan_dir,
            ("complete", "complete"),
            specs=self.SPECS,
            contracts_by_id=contracts,
        )
        # ``write_multi_repo_plan`` writes an ordinary session 2; the repair
        # machinery only recognises a child's parent by these two markers.
        review_path = self.plan_dir / "session-2.md"
        heading, remainder = review_path.read_text(encoding="utf-8").split("\n", 1)
        review_path.write_text(
            (
                f"{heading}\n\n{review.REVIEW_SESSION_MARKER}\n"
                "<!-- asl:generated-review-session version="
                f"{review.GENERATED_REVIEW_SESSION_VERSION} -->\n{remainder}"
            ).replace(
                f"**Writes repositories**: {aliases}",
                "**Writes repositories**: (none)",
            ),
            encoding="utf-8",
        )
        for session_id in ("1", "2"):
            path = self.plan_dir / f"session-{session_id}.md"
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    "- **Files modified**: (none yet)",
                    "- **Files modified**: (none)",
                ),
                encoding="utf-8",
            )
        self._write_review_artifact([self._finding(("OAK", "SVC"))])
        self._pin_workspace()
        self._materialize_children()
        self.assertEqual("SVC, OAK", self._write_scope(self.child))

    def _stamp_ready(self) -> None:
        # The retained-violation branch only runs once the pre-loop readiness
        # gate passes, and that gate needs both a journal and a fresh stamp.
        plans.write_progress_journal(
            self.plan_dir,
            tuple(
                plans.ProgressEntry(
                    session_id=plans.parse_session_id(session_id),
                    status="complete",
                    completed_at="2026-06-19T00:00:00Z",
                )
                for session_id in ("1", "2")
            ),
        )
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=plans.stable_readiness_sha256(self.plan_dir),
                checked_at="2026-06-19T00:00:00Z",
            ),
        )

    def _stray_write(self, name: str, **kwargs: Any) -> harnesses.HarnessResult:
        self.prompts.append(str(kwargs.get("prompt", "")))
        (self.repositories["OAK"] / "notes.md").write_text(
            "in-scope work\n", encoding="utf-8"
        )
        (self.repositories["OWL"] / "notes.md").write_text(
            "stray work\n", encoding="utf-8"
        )
        self.child.write_text(
            self.child.read_text(encoding="utf-8").replace(
                "- **Status**: pending", "- **Status**: complete", 1
            ),
            encoding="utf-8",
        )
        return harnesses.HarnessResult(
            name=name, argv=["fake"], returncode=0, stdout="ok\n", stderr=""
        )

    def _git(self, alias: str, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.repositories[alias]), *args],
            text=True,
            capture_output=True,
            check=True,
        )
        return result.stdout

    def _retain_a_violation(self) -> None:
        self._stamp_ready()
        _exit_code, first = self._run("retained-run-1", self._stray_write)
        self.assertEqual("workspace-write-scope-violation", first["pause_reason"])
        self.assertIn("- **Status**: pending", self.child.read_text(encoding="utf-8"))

    def _drop_recorded_post_turn_evidence(self) -> None:
        # A pause recorded before the guard kept an after-vector carries the
        # retained pathnames and nothing about their content; stripping the key
        # reproduces that record shape exactly.
        event_path = self._event_path()
        stripped = 0
        lines: list[str] = []
        for record in read_jsonl(event_path):
            payload = record.get("payload")
            details = payload.get("details") if isinstance(payload, dict) else None
            guard = (
                details.get("workspace_write_guard")
                if isinstance(details, dict)
                else None
            )
            if (
                isinstance(guard, dict)
                and guard.pop("snapshot_after", None) is not None
            ):
                stripped += 1
            lines.append(json.dumps(record))
        self.assertTrue(stripped, "the pause must record post-turn content evidence")
        event_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _widen_the_review_to_the_stray_repository(self) -> None:
        # The review now agrees the stray repository was in scope all along,
        # which is exactly what regeneration has to widen the child to.
        self._write_review_artifact([self._finding(("OAK", "SVC", "OWL"))])
        (self.plan_dir / "session-2.1.expand.md").write_text(
            "# Expansion for 2.1\n\nRepaired scope notes.\n", encoding="utf-8"
        )
        self._stamp_ready()

    def test_a_retained_violation_is_repaired_before_preflight_and_the_packet(
        self,
    ) -> None:
        self._retain_a_violation()
        self._widen_the_review_to_the_stray_repository()

        _exit_code, second = self._run("retained-run-2", self._record_prompt)

        self.assertEqual("SVC, OAK, OWL", self._write_scope(self.child))
        # Preflight ran against the widened scope and sanctioned the dirt the
        # retention deliberately kept, instead of dead-ending the repair.
        (preflight_record,) = second["repair_commit_preflights"]
        self.assertEqual(
            {"OAK", "OWL"},
            {
                row["alias"]
                for row in preflight_record["repositories"]
                if not row["clean"]
            },
        )
        sanction = preflight_record["retained_scope_repair"]
        self.assertEqual("2.1", sanction["session_id"])
        self.assertEqual("OAK, OWL", sanction["sanctioned_repositories"])
        self.assertEqual(
            ["OAK:notes.md", "OWL:notes.md"],
            sorted(sanction["sanctioned_dirty_paths"]),
        )
        self.assertNotIn("commit-preflight", self._pause_reasons())
        # And the packet the repair worker received names the added alias.
        self.assertEqual(
            "SVC, OAK, OWL", self._prompt_field(self.prompts[-1], "Writable aliases")
        )

    def test_unrelated_dirt_after_retention_is_not_committed(self) -> None:
        self._retain_a_violation()
        # An operator starts unrelated work in a repository the repair child is
        # about to write to. The repair's commit is repository-wide, so dirt the
        # sanction lets through is published under the repair's name -- work
        # nobody reviewed, in a commit nobody attributed to its author.
        (self.repositories["OAK"] / "operator-unrelated.md").write_text(
            "operator work\n", encoding="utf-8"
        )
        self._widen_the_review_to_the_stray_repository()
        head_before = self._git("OAK", "rev-parse", "HEAD")

        exit_code, payload = self._run("retained-unrelated-dirt", self._refuse)

        self.assertEqual(1, exit_code)
        self.assertEqual("commit-preflight", payload["pause_reason"])
        self.assertEqual([], payload["turns"])
        details = payload["pause_details"]
        sanction = details["retained_scope_repair"]
        # The retained turn's own two paths stay sanctioned; the path it never
        # wrote is what stops the run, and only its repository is named.
        self.assertEqual(
            ["OAK:notes.md", "OWL:notes.md"], sorted(sanction["sanctioned_dirty_paths"])
        )
        self.assertEqual(
            ["OAK:operator-unrelated.md"], sanction["unsanctioned_dirty_paths"]
        )
        self.assertEqual(
            ["OAK"], [row["alias"] for row in details["dirty_repositories"]]
        )
        # Nothing was published: the operator's file is still their own
        # uncommitted work, and so is the retained repair it would have ridden.
        self.assertEqual(head_before, self._git("OAK", "rev-parse", "HEAD"))
        self.assertEqual(
            "?? notes.md\n?? operator-unrelated.md\n",
            self._git("OAK", "status", "--porcelain"),
        )

    def test_operator_edit_to_retained_path_after_pause_is_not_committed(self) -> None:
        self._retain_a_violation()
        # The operator edits a path the retained turn really did write. Its
        # pathname is proven, but these bytes are not: nobody reviewed them, and
        # the repository-wide repair commit would publish them under the repair
        # child's name exactly as it would publish a brand-new file.
        (self.repositories["OAK"] / "notes.md").write_text(
            "in-scope work\noperator addition\n", encoding="utf-8"
        )
        self._widen_the_review_to_the_stray_repository()
        head_before = self._git("OAK", "rev-parse", "HEAD")

        exit_code, payload = self._run("retained-path-edited", self._refuse)

        self.assertEqual(1, exit_code)
        self.assertEqual("commit-preflight", payload["pause_reason"])
        self.assertEqual([], payload["turns"])
        details = payload["pause_details"]
        sanction = details["retained_scope_repair"]
        # The pathname is still recorded as one the retained turn wrote; what
        # withdraws the sanction is that the content at it has since moved.
        self.assertIn("OAK:notes.md", sanction["retained_turn_paths"])
        self.assertEqual(["OWL:notes.md"], sanction["sanctioned_dirty_paths"])
        self.assertEqual(["OAK:notes.md"], sanction["unsanctioned_dirty_paths"])
        self.assertEqual(["OAK:notes.md"], sanction["content_drifted_dirty_paths"])
        self.assertTrue(sanction["content_evidence"]["available"])
        self.assertEqual(
            ["OAK"], [row["alias"] for row in details["dirty_repositories"]]
        )
        # Nothing was published, and the operator's own bytes are still theirs.
        self.assertEqual(head_before, self._git("OAK", "rev-parse", "HEAD"))
        self.assertEqual("?? notes.md\n", self._git("OAK", "status", "--porcelain"))
        self.assertEqual(
            "in-scope work\noperator addition\n",
            (self.repositories["OAK"] / "notes.md").read_text(encoding="utf-8"),
        )

    def test_staging_a_retained_path_after_pause_withdraws_its_sanction(self) -> None:
        self._retain_a_violation()
        # Identical bytes, moved into the index. The commit the repair child
        # ends with stages everything anyway, so an index the retained turn did
        # not leave behind is as unproven as content it did not leave behind.
        self._git("OAK", "add", "notes.md")
        self._widen_the_review_to_the_stray_repository()

        exit_code, payload = self._run("retained-path-staged", self._refuse)

        self.assertEqual(1, exit_code)
        self.assertEqual("commit-preflight", payload["pause_reason"])
        sanction = payload["pause_details"]["retained_scope_repair"]
        self.assertEqual(["OAK:notes.md"], sanction["content_drifted_dirty_paths"])
        self.assertEqual(["OWL:notes.md"], sanction["sanctioned_dirty_paths"])

    def test_a_retained_violation_without_post_turn_evidence_cannot_be_sanctioned(
        self,
    ) -> None:
        self._retain_a_violation()
        self._drop_recorded_post_turn_evidence()
        self._widen_the_review_to_the_stray_repository()

        exit_code, payload = self._run("retained-without-after-evidence", self._refuse)

        self.assertEqual(1, exit_code)
        self.assertEqual("commit-preflight", payload["pause_reason"])
        details = payload["pause_details"]
        sanction = details["retained_scope_repair"]
        # A pathname the runner cannot check the content of is not sanctioned at
        # all, so a record written before the after-vector existed fails closed.
        self.assertEqual([], sanction["sanctioned_dirty_paths"])
        self.assertEqual(
            ["OAK:notes.md", "OWL:notes.md"],
            sorted(sanction["unsanctioned_dirty_paths"]),
        )
        self.assertFalse(sanction["content_evidence"]["available"])
        self.assertIn(
            "no post-turn content evidence", sanction["content_evidence"]["reason"]
        )

    def test_dirt_without_a_retained_violation_still_fails_commit_preflight(
        self,
    ) -> None:
        (self.repositories["OAK"] / "notes.md").write_text(
            "unexplained work\n", encoding="utf-8"
        )

        exit_code, payload = self._run("unsanctioned-dirt", self._refuse)

        self.assertEqual(1, exit_code)
        self.assertEqual("commit-preflight", payload["pause_reason"])
        self.assertEqual([], payload["turns"])
        details = payload["pause_details"]
        self.assertEqual(
            ["OAK"], [row["alias"] for row in details["dirty_repositories"]]
        )
        self.assertNotIn("retained_scope_repair", details)


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
        registry = plans.read_repository_registry(self.plan_dir)
        assert registry is not None
        plans.save_workspace_plan_metadata(
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
        return str(plans.canonicalize_path(path))

    def _publish_rival_claim(self, roots: tuple[Path, ...], *, pid: int) -> None:
        canonical = plans.canonicalize_path(self.other_plan_dir)
        rival_state = self.state_dir / plans.path_sha256(canonical)
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


class RunVerificationInterruptTests(unittest.TestCase):
    """Direct ``run_locked`` invocation tests covering KeyboardInterrupt
    handling for the post-turn verification phase and the outer session loop.

    These exercise the cleanup contract: any KeyboardInterrupt raised after a
    harness turn finishes must leave a coherent state payload with
    ``pause_reason='interrupted'``, ``run_finished_at`` populated, and no
    dangling ``active_turn`` / ``active_verification`` entries.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.plan_dir = self.tmp_path / "plan"
        self.state_dir = self.tmp_path / "state"
        self.repo_root = self.tmp_path / "repo"
        self.config_home = self.tmp_path / "config"
        init_git_repo(self.repo_root)
        self.fake_codex = install_fake_codex(self.tmp_path)
        config_path = self.config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps({"codex": {"executable": str(self.fake_codex)}}),
            encoding="utf-8",
        )

    def _make_args(self) -> argparse.Namespace:
        return argparse.Namespace(
            plan_dir=str(self.plan_dir),
            cwd=str(self.repo_root),
            harness="codex",
            state_dir=str(self.state_dir),
            json=False,
            dry_run=False,
            unsafe_bypass=True,
            timeout=None,
            max_sessions=None,
            commit_mode=None,
            model=None,
            effort=None,
            note=None,
            allow_not_loop_ready=True,
        )

    def _invoke_run_locked(
        self,
    ) -> tuple[int, dict[str, Any]]:
        env_updates = {
            "XDG_CONFIG_HOME": str(self.config_home),
            "FAKE_CODEX_MODE": "success",
            "FAKE_CODEX_SLEEP": "5",
            "PYTHONPATH": str(REPO_ROOT)
            + os.pathsep
            + os.environ.get("PYTHONPATH", ""),
        }
        args = self._make_args()
        with patched_env(**env_updates):
            lock_info = state.acquire_lock(self.plan_dir, self.state_dir)
            try:
                exit_code, payload = run_loop.run_locked(
                    args=args,
                    cwd=self.repo_root,
                    timeout=None,
                    max_sessions=None,
                    commit_mode=None,
                    lock_info=lock_info,
                    run_id="test-run",
                )
            finally:
                state.release_lock(self.plan_dir, self.state_dir)
        return exit_code, payload

    def test_keyboard_interrupt_during_verification_pauses_cleanly(
        self,
    ) -> None:
        contract = _contract_block(
            '{"type": "file_exists", "path": "README.md", "name": "readme"}',
            session="1",
        )
        write_plan(self.plan_dir, ("pending",), contracts_by_id={"1": contract})

        def raise_keyboard_interrupt(*_args: object, **_kwargs: object) -> None:
            raise KeyboardInterrupt

        with mock.patch.object(
            verification,
            "run_verification",
            side_effect=raise_keyboard_interrupt,
        ):
            exit_code, payload = self._invoke_run_locked()

        self.assertEqual(turn_execution.INTERRUPTED_EXIT, exit_code)
        self.assertTrue(payload["paused"])
        self.assertEqual("interrupted", payload["pause_reason"])
        self.assertIn("verification", payload["pause_message"])
        self.assertIn("interrupted by user", payload["pause_message"])
        self.assertIsNone(payload["active_turn"])
        self.assertIsNone(payload["active_verification"])
        self.assertIsInstance(payload["run_finished_at"], str)
        self.assertNotEqual("", payload["run_finished_at"])
        # The session was reverted to pending.
        session_text = (self.plan_dir / "session-1.md").read_text(encoding="utf-8")
        self.assertIn("- **Status**: pending", session_text)
        self.assertNotIn("- **Status**: complete", session_text)
        self.assertIn("verification interrupted by user", session_text)
        # Lock was released.
        self.assertFalse(
            state.lock_path_for_plan(self.plan_dir, self.state_dir).exists()
        )
        # The verification phase emitted a `finished` event with the
        # interrupted reason, keeping the observer contract symmetric.
        event_path = (
            state.plan_state_dir(self.plan_dir, self.state_dir)
            / events.EVENTS_FILE_NAME
        )
        event_records = read_jsonl(event_path)
        finished_records = [
            record
            for record in event_records
            if record["name"] == events.VERIFICATION_FINISHED_EVENT
        ]
        self.assertEqual(1, len(finished_records))
        self.assertEqual("interrupted", finished_records[0]["payload"]["reason"])
        self.assertFalse(finished_records[0]["payload"]["passed"])
        # `run_finished` was still emitted as the final lifecycle event.
        run_finished_records = [
            record for record in event_records if record["name"] == "run_finished"
        ]
        self.assertEqual(1, len(run_finished_records))

    def test_keyboard_interrupt_after_turn_finished_pauses_cleanly(
        self,
    ) -> None:
        """Cover the outer ``run_locked`` KeyboardInterrupt wrapper.

        When a KeyboardInterrupt escapes the post-turn bookkeeping helpers
        (here, ``_append_progress_entry``), the outer ``try/except`` must call
        ``_pause_run('interrupted', ...)`` once, then ``_finish_run`` must
        clear both active phases and stamp ``run_finished_at``.
        """

        # No completion contract: verification is skipped, so the next thing
        # called after `turn_finished` is `_append_progress_entry`, which we
        # mock to raise KeyboardInterrupt.
        write_plan(self.plan_dir, ("pending",))

        def raise_keyboard_interrupt(*_args: object, **_kwargs: object) -> bool:
            raise KeyboardInterrupt

        with mock.patch.object(
            handoff_guards,
            "_append_progress_entry",
            side_effect=raise_keyboard_interrupt,
        ):
            exit_code, payload = self._invoke_run_locked()

        self.assertEqual(turn_execution.INTERRUPTED_EXIT, exit_code)
        self.assertTrue(payload["paused"])
        self.assertEqual("interrupted", payload["pause_reason"])
        self.assertEqual("run interrupted by user", payload["pause_message"])
        # Pause details should carry the in-flight session identity.
        pause_details = payload["pause_details"]
        self.assertEqual(1, pause_details["session"])
        self.assertIsNone(payload["active_turn"])
        self.assertIsNone(payload["active_verification"])
        self.assertIsInstance(payload["run_finished_at"], str)
        self.assertNotEqual("", payload["run_finished_at"])
        # The lock is released even when the outer wrapper catches the KI.
        self.assertFalse(
            state.lock_path_for_plan(self.plan_dir, self.state_dir).exists()
        )
        # Exactly one `run_paused` event was recorded (no duplicate from a
        # cascading handler).
        event_path = (
            state.plan_state_dir(self.plan_dir, self.state_dir)
            / events.EVENTS_FILE_NAME
        )
        event_records = read_jsonl(event_path)
        pause_records = [
            record
            for record in event_records
            if record["name"] == events.RUN_PAUSED_EVENT
        ]
        self.assertEqual(1, len(pause_records))
        self.assertEqual("interrupted", pause_records[0]["payload"]["reason"])


class LongVerificationHeartbeatIntegrationTests(unittest.TestCase):
    """End-to-end coverage for the full post-turn verification lifecycle.

    Drives ``run_locked`` directly with a synthetic long-running verification
    command and a shortened heartbeat interval so at least one
    ``verification_heartbeat`` event fires inside the check window. Also locks
    in the documented post-turn event ordering: ``turn_finished`` precedes the
    four verification phase events, the progress journal entry is appended
    after verification passes, and ``run_finished`` is the final lifecycle
    event.

    Also covers the historical ``state.json`` shape (no ``active_verification``
    key) as a fixture-style regression: ``status_payload`` and
    ``_print_text_status`` must remain consumable by pre-feature state
    snapshots without raising or inventing detail.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.plan_dir = self.tmp_path / "plan"
        self.state_dir = self.tmp_path / "state"
        self.repo_root = self.tmp_path / "repo"
        self.config_home = self.tmp_path / "config"
        init_git_repo(self.repo_root)
        self.fake_codex = install_fake_codex(self.tmp_path)
        config_path = self.config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps({"codex": {"executable": str(self.fake_codex)}}),
            encoding="utf-8",
        )

    def _make_args(self) -> argparse.Namespace:
        return argparse.Namespace(
            plan_dir=str(self.plan_dir),
            cwd=str(self.repo_root),
            harness="codex",
            state_dir=str(self.state_dir),
            json=False,
            dry_run=False,
            unsafe_bypass=True,
            timeout=None,
            max_sessions=None,
            commit_mode=None,
            model=None,
            effort=None,
            note=None,
            allow_not_loop_ready=True,
        )

    def _invoke_run_locked(self) -> tuple[int, dict[str, Any]]:
        env_updates = {
            "XDG_CONFIG_HOME": str(self.config_home),
            "FAKE_CODEX_MODE": "success",
            "PYTHONPATH": str(REPO_ROOT)
            + os.pathsep
            + os.environ.get("PYTHONPATH", ""),
        }
        args = self._make_args()
        with patched_env(**env_updates):
            lock_info = state.acquire_lock(self.plan_dir, self.state_dir)
            try:
                exit_code, payload = run_loop.run_locked(
                    args=args,
                    cwd=self.repo_root,
                    timeout=None,
                    max_sessions=None,
                    commit_mode=None,
                    lock_info=lock_info,
                    run_id="test-run-heartbeat",
                )
            finally:
                state.release_lock(self.plan_dir, self.state_dir)
        return exit_code, payload

    def test_long_verification_command_emits_heartbeats_and_orders_post_turn_events(
        self,
    ) -> None:
        sleep_argv = [
            sys.executable,
            "-c",
            "import time; time.sleep(0.3)",
        ]
        check_payload = (
            '{"type": "command", "name": "long-sleep", '
            f'"argv": {json.dumps(sleep_argv)}, "timeout_seconds": 30}}'
        )
        contract = _contract_block(check_payload, session="1")
        write_plan(self.plan_dir, ("pending",), contracts_by_id={"1": contract})

        real_enforce = session_verification._enforce_session_verification

        def short_heartbeat_enforce(**kwargs: object) -> bool:
            kwargs["heartbeat_interval"] = 0.05
            return real_enforce(**kwargs)

        with mock.patch.object(
            session_verification,
            "_enforce_session_verification",
            new=short_heartbeat_enforce,
        ):
            exit_code, payload = self._invoke_run_locked()

        self.assertEqual(0, exit_code)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])
        self.assertEqual("all-complete", payload["reason"])
        # Both active phases cleared cleanly at run finish.
        self.assertIsNone(payload["active_turn"])
        self.assertIsNone(payload["active_verification"])
        # Final snapshot reflects the post-verify all-complete plan state.
        self.assertEqual(1, payload["final_snapshot"]["counts"]["complete"])
        self.assertIsNone(payload["final_snapshot"]["next_pending"])

        event_path = (
            state.plan_state_dir(self.plan_dir, self.state_dir)
            / events.EVENTS_FILE_NAME
        )
        event_records = read_jsonl(event_path)
        event_names = [record["name"] for record in event_records]

        # At least one verification_heartbeat fired during the synthetic long
        # verify. With a 0.3s sleep and a 0.05s interval, we expect several
        # heartbeats; the assertion uses >=1 to avoid scheduler flakiness.
        heartbeat_count = event_names.count(events.VERIFICATION_HEARTBEAT_EVENT)
        self.assertGreaterEqual(
            heartbeat_count,
            1,
            f"expected verification_heartbeat events; got: {event_names}",
        )

        # Heartbeats happened strictly between check_started and
        # check_finished for the same check.
        check_started_index = event_names.index(events.VERIFICATION_CHECK_STARTED_EVENT)
        check_finished_index = event_names.index(
            events.VERIFICATION_CHECK_FINISHED_EVENT
        )
        heartbeat_indexes = [
            i
            for i, name in enumerate(event_names)
            if name == events.VERIFICATION_HEARTBEAT_EVENT
        ]
        for hb_index in heartbeat_indexes:
            self.assertGreater(hb_index, check_started_index)
            self.assertLess(hb_index, check_finished_index)

        # Documented post-turn event ordering: the harness exit is announced
        # before the four verification phase events, turn_finished follows
        # them because verification is a guard that can still refuse the turn,
        # and run_finished comes last.
        execution_finished_index = event_names.index(
            events.TURN_EXECUTION_FINISHED_EVENT
        )
        turn_finished_index = event_names.index(events.TURN_FINISHED_EVENT)
        verification_started_index = event_names.index(
            events.VERIFICATION_STARTED_EVENT
        )
        verification_finished_index = event_names.index(
            events.VERIFICATION_FINISHED_EVENT
        )
        run_finished_index = event_names.index("run_finished")
        self.assertLess(execution_finished_index, verification_started_index)
        self.assertLess(verification_started_index, check_started_index)
        self.assertLess(check_started_index, check_finished_index)
        self.assertLess(check_finished_index, verification_finished_index)
        self.assertLess(verification_finished_index, turn_finished_index)
        self.assertLess(turn_finished_index, run_finished_index)

        # Progress journal append happened after verification passed and
        # before run_finished. The append itself is silent on success, so the
        # durable evidence is the journal entry that exists by the time the
        # run reports completion.
        entries = plans.read_progress_entries(self.plan_dir)
        self.assertEqual(1, len(entries))
        self.assertEqual("1", entries[0].session_id.value)
        self.assertEqual("complete", entries[0].status)
        self.assertTrue(payload["turns"][0]["progress_appended"])

    def test_agent_mux_notifications_describe_the_same_phase_sequence(
        self,
    ) -> None:
        """The AgentMux notification stream must mirror the persisted
        lifecycle: same phase transitions as state/events, one monotonic
        per-run sequence, and verification heartbeats carrying check
        identity — the daemon-side contract from the reliability plan."""
        _fake_amx, amx_log = install_fake_amx(self.tmp_path)
        sleep_argv = [
            sys.executable,
            "-c",
            "import time; time.sleep(0.3)",
        ]
        check_payload = (
            '{"type": "command", "name": "long-sleep", '
            f'"argv": {json.dumps(sleep_argv)}, "timeout_seconds": 30}}'
        )
        contract = _contract_block(check_payload, session="1")
        write_plan(self.plan_dir, ("pending",), contracts_by_id={"1": contract})

        real_enforce = session_verification._enforce_session_verification

        def short_heartbeat_enforce(**kwargs: object) -> bool:
            kwargs["heartbeat_interval"] = 0.05
            return real_enforce(**kwargs)

        with (
            agent_mux_notify_enabled(),
            patched_env(
                PATH=str(self.tmp_path) + os.pathsep + os.environ.get("PATH", ""),
                FAKE_AMX_LOG=str(amx_log),
                # Slow interpreter start under parallel-shard load must not
                # silently drop a notification record.
                ASL_AGENT_MUX_NOTIFY_TIMEOUT_SECONDS="30",
            ),
            mock.patch.object(
                session_verification,
                "_enforce_session_verification",
                new=short_heartbeat_enforce,
            ),
        ):
            exit_code, payload = self._invoke_run_locked()

        self.assertEqual(0, exit_code)
        self.assertTrue(payload["ok"])

        amx_records = read_jsonl(amx_log)
        notified_events = [record["argv"][-1] for record in amx_records]
        hook_payloads = [json.loads(str(record["stdin"])) for record in amx_records]

        # At least one verification heartbeat notification fired inside the
        # synthetic 0.3s check window (0.05s interval).
        verification_heartbeats = [
            hook_payload
            for event, hook_payload in zip(notified_events, hook_payloads, strict=False)
            if event == agent_mux_delivery.VERIFICATION_HEARTBEAT_EVENT
        ]
        self.assertGreaterEqual(
            len(verification_heartbeats),
            1,
            f"expected verification heartbeat notifications; got: {notified_events}",
        )

        # The notification stream, with consecutive same-event runs
        # collapsed, is exactly the documented lifecycle order.
        collapsed_events = [
            event
            for index, event in enumerate(notified_events)
            if index == 0 or event != notified_events[index - 1]
        ]
        self.assertEqual(
            [
                agent_mux_delivery.START_EVENT,
                agent_mux_delivery.TURN_START_EVENT,
                agent_mux_delivery.VERIFICATION_START_EVENT,
                agent_mux_delivery.VERIFICATION_HEARTBEAT_EVENT,
                agent_mux_delivery.VERIFICATION_FINISH_EVENT,
                agent_mux_delivery.TURN_FINISH_EVENT,
                agent_mux_delivery.FINISH_EVENT,
            ],
            collapsed_events,
        )

        # Every notification carries the run identity, the daemon phase for
        # its event, and one strictly monotonic string-encoded sequence.
        run_id = payload["run_id"]
        for event, hook_payload in zip(notified_events, hook_payloads, strict=False):
            self.assertEqual(run_id, hook_payload["run_id"])
            self.assertEqual(
                agent_mux_delivery.PHASE_BY_EVENT[event],
                hook_payload["phase"],
            )
            self.assertEqual(
                f"{run_id}:{hook_payload['source_sequence']}",
                hook_payload["source_event_id"],
            )
            self.assertTrue(hook_payload["emitted_time"].isdigit())
        sequences = [
            int(hook_payload["source_sequence"]) for hook_payload in hook_payloads
        ]
        self.assertEqual(list(range(1, len(sequences) + 1)), sequences)

        # The persisted counter in state.json matches the last emission.
        state_payload = json.loads(
            Path(str(payload["state_path"])).read_text(encoding="utf-8")
        )
        self.assertEqual(len(sequences), state_payload["agent_mux_sequence"])

        # Verification heartbeats carry the check identity, heartbeat count,
        # and recency fields the daemon consumes.
        for heartbeat_payload in verification_heartbeats:
            self.assertEqual("long-sleep", heartbeat_payload["check_name"])
            self.assertEqual("command", heartbeat_payload["check_kind"])
            self.assertGreaterEqual(heartbeat_payload["heartbeat_count"], 1)
            self.assertIn("last_heartbeat_at", heartbeat_payload)
            self.assertEqual("1", heartbeat_payload["session_id"])

        # The notification phase transitions equal the phase transitions
        # derivable from the persisted events.jsonl — the "same phase
        # sequence" completion contract.
        event_path = (
            state.plan_state_dir(self.plan_dir, self.state_dir)
            / events.EVENTS_FILE_NAME
        )
        internal_phase_by_event = {
            "turn_started": "turn",
            events.TURN_HEARTBEAT_EVENT: "turn",
            "turn_finished": "between",
            events.VERIFICATION_STARTED_EVENT: "verification",
            events.VERIFICATION_HEARTBEAT_EVENT: "verification",
            events.VERIFICATION_FINISHED_EVENT: "between",
            "run_finished": "finished",
        }
        internal_phases = ["between"] + [
            internal_phase_by_event[record["name"]]
            for record in read_jsonl(event_path)
            if record["name"] in internal_phase_by_event
        ]
        collapse = lambda values: [  # noqa: E731 - one-off local, reads better inline
            value
            for index, value in enumerate(values)
            if index == 0 or value != values[index - 1]
        ]
        self.assertEqual(
            collapse(internal_phases),
            collapse([hook_payload["phase"] for hook_payload in hook_payloads]),
        )

        # `active_verification.last_heartbeat_at` was persisted while the
        # check ran; the final state clears the block entirely.
        self.assertIsNone(state_payload["active_verification"])

    def test_historical_state_without_active_verification_renders_cleanly(
        self,
    ) -> None:
        write_plan(self.plan_dir, ("complete",))
        plan_state = state.plan_state_dir(self.plan_dir, self.state_dir)
        plan_state.mkdir(parents=True, exist_ok=True)
        state_path = plan_state / events.STATE_FILE_NAME
        # Synthetic snapshot of a pre-feature `state.json`: no
        # `active_verification` key, no stale-orphan tracking, no
        # newer turn-level fields. The new code paths must default the
        # missing fields without raising or inventing detail.
        historical_payload = {
            "ok": True,
            "paused": False,
            "reason": "all-complete",
            "harness": "codex",
            "run_id": "historical-run-1",
            "run_started_at": "2025-01-01T00:00:00+00:00",
            "run_finished_at": "2025-01-01T00:05:00+00:00",
            "duration_seconds": 300.0,
            "active_turn": None,
        }
        state_path.write_text(
            json.dumps(historical_payload, sort_keys=True),
            encoding="utf-8",
        )

        snapshot = plans.inspect_plan(self.plan_dir)
        payload = state.status_payload(snapshot, self.state_dir)

        # Top-level shape mirrors `runner_state` and defaults missing keys.
        self.assertIsNone(payload["active_turn"])
        self.assertIsNone(payload["active_verification"])
        runner_state = payload["runner_state"]
        self.assertIsNone(runner_state["active_turn"])
        self.assertIsNone(runner_state["active_verification"])
        # A historical run that recorded `run_finished_at` is not stale, even
        # without a live lock — the stale-orphan check only fires when state
        # never finished.
        self.assertFalse(runner_state["stale_unfinished"])
        self.assertIsNone(runner_state["stale_reason"])

        # The text renderer must not raise when fed a historical payload and
        # must not invent `Active verification:` or `stale/orphaned` lines
        # that weren't present in the snapshot.
        buffer = io.StringIO()
        args = argparse.Namespace(
            plan_dir=str(self.plan_dir),
            state_dir=str(self.state_dir),
            json=False,
        )
        with redirect_stdout(buffer):
            exit_code = state.status_from_args(args)
        self.assertEqual(0, exit_code)
        rendered = buffer.getvalue()
        self.assertNotIn("Active verification:", rendered)
        self.assertNotIn("stale/orphaned", rendered)
        self.assertIn("Run state: run historical-run-1", rendered)


class RunProgressJournalIntegrationTests(unittest.TestCase):
    """End-to-end coverage for runner-side ``progress.md`` integration."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.plan_dir = self.tmp_path / "plan"
        self.state_dir = self.tmp_path / "state"
        self.repo_root = self.tmp_path / "repo"
        self.config_home = self.tmp_path / "config"
        init_git_repo(self.repo_root)
        self.fake_codex = install_fake_codex(self.tmp_path)
        config_path = self.config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps({"codex": {"executable": str(self.fake_codex)}}),
            encoding="utf-8",
        )

    def _write_plan(
        self,
        statuses: tuple[str, ...],
        *,
        contracts_by_id: dict[str, str],
        readiness: str | None = "loop-ready",
    ) -> None:
        write_plan(self.plan_dir, statuses, contracts_by_id=contracts_by_id)
        if readiness is None:
            return
        sha = plans.stable_readiness_sha256(self.plan_dir)
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state=readiness,
                readiness_sha256=sha,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )

    def _run(self) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
        argv = [
            sys.executable,
            "-m",
            "agent_session_loop",
            "run",
            str(self.plan_dir),
            "--cwd",
            str(self.repo_root),
            "--harness",
            "codex",
            "--state-dir",
            str(self.state_dir),
            "--json",
            "--allow-not-loop-ready",
        ]
        env = os.environ.copy()
        env.update(
            {
                "XDG_CONFIG_HOME": str(self.config_home),
                "FAKE_CODEX_MODE": "success",
                "FAKE_CODEX_SLEEP": "5",
                "PYTHONPATH": str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", ""),
            }
        )
        completed = subprocess.run(
            argv,
            cwd=REPO_ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        return completed, json.loads(completed.stdout)

    def _passing_contracts(self, count: int) -> dict[str, str]:
        return {
            str(i): _contract_block(
                f'{{"type": "file_exists", "path": "README.md", "name": "readme-{i}"}}',
                session=str(i),
            )
            for i in range(1, count + 1)
        }

    def test_two_session_run_writes_progress_journal_in_order(self) -> None:
        self._write_plan(
            ("pending", "pending"),
            contracts_by_id=self._passing_contracts(2),
        )

        completed, payload = self._run()

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual("all-complete", payload["reason"])

        entries = plans.read_progress_entries(self.plan_dir)
        self.assertEqual(2, len(entries))
        self.assertEqual(["1", "2"], [entry.session_id.value for entry in entries])
        self.assertEqual(["complete", "complete"], [entry.status for entry in entries])

        self.assertEqual(2, len(payload["turns"]))
        for turn in payload["turns"]:
            self.assertTrue(turn["progress_appended"])
            self.assertNotIn("progress_error", turn)

    def test_verification_failure_short_circuits_progress_append(self) -> None:
        contracts_by_id = {
            "1": _contract_block(
                '{"type": "file_exists", "path": "MISSING.md", "name": "missing"}',
                session="1",
            )
        }
        self._write_plan(("pending",), contracts_by_id=contracts_by_id)

        completed, payload = self._run()

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["paused"])
        self.assertEqual("verify-failed", payload["pause_reason"])

        self.assertFalse(plans.progress_path(self.plan_dir).exists())
        self.assertNotIn("progress_appended", payload["turns"][0])
        failure_events = [
            record
            for record in read_jsonl(Path(payload["events_path"]))
            if record["name"] == "progress-journal-append-failed"
        ]
        self.assertEqual([], failure_events)

    def test_append_failure_records_error_and_continues_run(self) -> None:
        self._write_plan(
            ("pending",),
            contracts_by_id=self._passing_contracts(1),
        )
        # Pre-write progress.md with a future schema version so the real
        # append helper raises PlanError without crashing the run.
        plans.progress_path(self.plan_dir).write_text(
            "# Progress Journal\n<!-- agent-session-loop progress-journal v999 -->\n",
            encoding="utf-8",
        )

        completed, payload = self._run()

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual("all-complete", payload["reason"])

        turn = payload["turns"][0]
        self.assertFalse(turn["progress_appended"])
        self.assertIn("v999", turn["progress_error"])

        failure_events = [
            record["payload"]
            for record in read_jsonl(Path(payload["events_path"]))
            if record["name"] == "progress-journal-append-failed"
        ]
        self.assertEqual(1, len(failure_events))
        self.assertEqual("1", failure_events[0]["session"])
        self.assertIn("v999", failure_events[0]["error"])


class HandoffCorrectionTests(unittest.TestCase):
    """A mechanically malformed handoff gets one bounded correction turn."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.workspace_root = self.tmp_path / "workspace"
        self.repositories = init_multi_repo_workspace(
            self.workspace_root,
            DEFAULT_MULTI_REPO_SPECS[:2],
        )
        self.plan_dir = self.tmp_path / "plan"
        self.state_dir = self.tmp_path / "state"
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
        session_path = self.plan_dir / "session-1.md"
        session_path.write_text(
            session_path.read_text(encoding="utf-8").replace(
                "**Reads repositories**: SVC, OAK\n**Writes repositories**: SVC, OAK",
                "**Reads repositories**: SVC\n**Writes repositories**: SVC",
            ),
            encoding="utf-8",
        )
        registry = plans.read_repository_registry(self.plan_dir)
        assert registry is not None
        plans.save_workspace_plan_metadata(
            self.plan_dir,
            self.workspace_root,
            registry.digest,
        )
        self.context = verification.VerificationContext.from_plan(
            self.plan_dir,
            self.workspace_root,
        )

    def _args(self) -> argparse.Namespace:
        return argparse.Namespace(
            plan_dir=str(self.plan_dir),
            cwd=str(self.workspace_root),
            harness="codex",
            state_dir=str(self.state_dir),
            json=True,
            dry_run=False,
            unsafe_bypass=True,
            timeout=None,
            max_sessions=None,
            commit_mode=None,
            model=None,
            effort=None,
            note=None,
            allow_not_loop_ready=True,
        )

    def _invoke(self, run_id: str) -> tuple[int, dict[str, Any]]:
        lock_info = state.acquire_lock(self.plan_dir, self.state_dir)
        try:
            return run_loop.run_locked(
                args=self._args(),
                cwd=self.workspace_root,
                timeout=None,
                max_sessions=None,
                commit_mode=None,
                lock_info=lock_info,
                run_id=run_id,
                verification_context=self.context,
            )
        finally:
            state.release_lock(self.plan_dir, self.state_dir)

    def _records(self) -> list[dict[str, Any]]:
        event_path = (
            state.plan_state_dir(self.plan_dir, self.state_dir)
            / events.EVENTS_FILE_NAME
        )
        return read_jsonl(event_path)

    def _payloads(self, name: str) -> list[dict[str, Any]]:
        return [
            record["payload"] for record in self._records() if record["name"] == name
        ]

    def _complete_with(self, files_modified: str) -> None:
        """Mark the session complete with an arbitrary Files modified line."""
        (self.repositories["SVC"] / "README.md").write_text(
            f"changed by the harness: {files_modified}\n", encoding="utf-8"
        )
        session_path = self.plan_dir / "session-1.md"
        lines = session_path.read_text(encoding="utf-8").splitlines(keepends=True)
        for index, line in enumerate(lines):
            if line.startswith(plans.STATUS_PREFIX):
                lines[index] = f"{plans.STATUS_PREFIX} complete\n"
            elif line.startswith(plans.HANDOFF_FILES_PREFIX):
                lines[index] = f"{plans.HANDOFF_FILES_PREFIX} {files_modified}\n"
        session_path.write_text("".join(lines), encoding="utf-8")

    def _harness(self, *files_lines: str):
        """Return a ``run_harness`` stand-in writing one handoff per turn."""
        pending = list(files_lines)

        def run(*_args: object, **_kwargs: object) -> harnesses.HarnessResult:
            self._complete_with(pending.pop(0) if pending else "(none)")
            return harnesses.HarnessResult(
                name="codex",
                argv=["fake"],
                returncode=0,
                stdout="ok\n",
                stderr="",
            )

        return run

    def test_unqualified_entry_is_corrected_within_one_extra_turn(self) -> None:
        with mock.patch.object(
            harnesses,
            "run_harness",
            side_effect=self._harness("README.md", "SVC:README.md"),
        ):
            exit_code, payload = self._invoke("correction-run")

        self.assertEqual(0, exit_code)
        self.assertFalse(payload["paused"])
        self.assertEqual("all-complete", payload["reason"])
        corrections = self._payloads(events.TURN_HANDOFF_CORRECTION_EVENT)
        self.assertEqual(1, len(corrections))
        self.assertEqual(
            handoff_correction.HANDOFF_DEFECT_UNQUALIFIED_ENTRY,
            corrections[0]["defect"],
        )
        self.assertEqual(1, corrections[0]["attempt"])
        self.assertEqual(
            handoff_correction.MAX_HANDOFF_CORRECTION_ATTEMPTS,
            corrections[0]["max_attempts"],
        )
        self.assertFalse(corrections[0]["exhausted"])
        self.assertIn("README.md", corrections[0]["detail"])
        self.assertEqual([], self._payloads(events.RUN_PAUSED_EVENT))

    def test_execution_event_precedes_each_accepted_turn(self) -> None:
        with mock.patch.object(
            harnesses,
            "run_harness",
            side_effect=self._harness("README.md", "SVC:README.md"),
        ):
            self._invoke("ordering-run")

        names = [
            record["name"]
            for record in self._records()
            if record["name"]
            in (events.TURN_EXECUTION_FINISHED_EVENT, events.TURN_FINISHED_EVENT)
        ]
        # Both turns ran, but the first one's handoff was malformed and was
        # sent back for correction, so only the second was ever accepted.
        self.assertEqual(
            [
                events.TURN_EXECUTION_FINISHED_EVENT,
                events.TURN_EXECUTION_FINISHED_EVENT,
                events.TURN_FINISHED_EVENT,
            ],
            names,
        )
        executions = self._payloads(events.TURN_EXECUTION_FINISHED_EVENT)
        self.assertFalse(executions[0]["plan_readable"])
        self.assertIn("session-1.md", executions[0]["plan_error"])
        self.assertTrue(executions[1]["plan_readable"])
        for execution in executions:
            self.assertEqual(0, execution["returncode"])
            self.assertTrue(Path(execution["result_path"]).is_file())

    def test_repeated_defect_exhausts_the_correction_and_pauses(self) -> None:
        with mock.patch.object(
            harnesses,
            "run_harness",
            side_effect=self._harness("README.md", "README.md"),
        ):
            exit_code, payload = self._invoke("exhausted-run")

        self.assertNotEqual(0, exit_code)
        self.assertTrue(payload["paused"])
        self.assertEqual("malformed-plan", payload["pause_reason"])
        evidence = payload["pause_details"]["handoff_correction"]
        self.assertTrue(evidence["exhausted"])
        self.assertEqual(2, evidence["attempt"])
        self.assertEqual(
            handoff_correction.HANDOFF_DEFECT_UNQUALIFIED_ENTRY, evidence["defect"]
        )
        self.assertEqual(1, len(self._payloads(events.TURN_HANDOFF_CORRECTION_EVENT)))
        # The session is still reopened so the plan parses, but a defect the
        # bounded correction already failed on is not advertised as repairable.
        reopen = payload["pause_details"]["handoff_reopen"]
        self.assertTrue(reopen["reopened"])
        self.assertFalse(reopen["repairable"])

    def test_scope_violation_is_not_treated_as_a_correctable_defect(self) -> None:
        with mock.patch.object(
            harnesses,
            "run_harness",
            side_effect=self._harness("OAK:README.md"),
        ) as run_harness:
            exit_code, payload = self._invoke("scope-run")

        self.assertNotEqual(0, exit_code)
        self.assertTrue(payload["paused"])
        self.assertEqual("malformed-plan", payload["pause_reason"])
        self.assertNotIn("handoff_correction", payload["pause_details"])
        self.assertEqual([], self._payloads(events.TURN_HANDOFF_CORRECTION_EVENT))
        self.assertEqual(1, run_harness.call_count)

    def test_uninspectable_completed_handoff_is_reopened_as_repairable(self) -> None:
        with mock.patch.object(
            harnesses,
            "run_harness",
            side_effect=self._harness("OAK:README.md"),
        ):
            _, payload = self._invoke("reopen-run")

        self.assertEqual("malformed-plan", payload["pause_reason"])
        evidence = payload["pause_details"]["handoff_reopen"]
        self.assertTrue(evidence["reopened"])
        self.assertTrue(evidence["repairable"])
        self.assertIn("session-1.md", evidence["plan_error"])

        # The plan parses again, and it claims the work still has to be handed
        # off, so a relaunch has something to select.
        session_text = (self.plan_dir / "session-1.md").read_text(encoding="utf-8")
        self.assertIn(f"{plans.STATUS_PREFIX} pending", session_text)
        self.assertIn("The status was reset to pending", session_text)
        self.assertIn("OAK:README.md", session_text)
        snapshot = plans.inspect_plan(self.plan_dir)
        self.assertEqual(["pending"], [item.status for item in snapshot.sessions])
        self.assertEqual(
            ["pending"],
            [item["status"] for item in payload["final_snapshot"]["sessions"]],
        )

        rejections = self._payloads(events.TURN_REJECTED_EVENT)
        self.assertEqual(1, len(rejections))
        self.assertTrue(rejections[0]["reopened"])
        reopenings = self._payloads(events.SESSION_REOPENED_EVENT)
        self.assertEqual(1, len(reopenings))
        self.assertEqual("complete", reopenings[0]["from_status"])
        self.assertEqual("pending", reopenings[0]["to_status"])
        self.assertEqual("plan-inspection", reopenings[0]["guard"])
        # Replaying the stream lands on the projection the run reports, not on
        # a state the consumer has to re-read the plan to confirm.
        self.assertEqual(snapshot.content_sha256, reopenings[0]["content_sha256"])
        self.assertEqual(payload["final_snapshot"]["counts"], reopenings[0]["counts"])

    def test_correction_turn_preserves_review_artifacts(self) -> None:
        (self.plan_dir / review.FINAL_REVIEW_FILENAME).write_text(
            f"# Final review\n\n{review.REVIEW_STATUS_ALL_CLEAR}\n", encoding="utf-8"
        )
        (self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
            json.dumps(
                {
                    "version": review.REVIEW_ARTIFACT_VERSION,
                    "outcome": "all-clear",
                    "findings": [],
                }
            ),
            encoding="utf-8",
        )
        digest = review.final_review_artifact_sha256(self.plan_dir)

        with mock.patch.object(
            harnesses,
            "run_harness",
            side_effect=self._harness("README.md", "SVC:README.md"),
        ):
            self._invoke("preserved-run")

        corrections = self._payloads(events.TURN_HANDOFF_CORRECTION_EVENT)
        preserved = corrections[0]["preserved_review_artifacts"]
        self.assertEqual(digest, preserved["review_artifact_sha256"])
        self.assertTrue(preserved["review_verdict_ok"])
        self.assertEqual("all-clear", preserved["review_outcome"])
        self.assertEqual(digest, review.final_review_artifact_sha256(self.plan_dir))


class PausedHandoffStatusTests(unittest.TestCase):
    """A structured runner pause outranks the status value it was written with."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.repo_root = self.tmp_path / "repo"
        init_git_repo(self.repo_root)
        self.plan_dir = self.tmp_path / "plan"
        self.state_dir = self.tmp_path / "state"
        write_plan(self.plan_dir, ("pending",))

    def _args(self) -> argparse.Namespace:
        return argparse.Namespace(
            plan_dir=str(self.plan_dir),
            cwd=str(self.repo_root),
            harness="codex",
            state_dir=str(self.state_dir),
            json=True,
            dry_run=False,
            unsafe_bypass=True,
            timeout=None,
            max_sessions=None,
            commit_mode=None,
            model=None,
            effort=None,
            note=None,
            allow_not_loop_ready=True,
        )

    def _invoke(self, run_id: str) -> tuple[int, dict[str, Any]]:
        lock_info = state.acquire_lock(self.plan_dir, self.state_dir)
        try:
            return run_loop.run_locked(
                args=self._args(),
                cwd=self.repo_root,
                timeout=None,
                max_sessions=None,
                commit_mode=None,
                lock_info=lock_info,
                run_id=run_id,
            )
        finally:
            state.release_lock(self.plan_dir, self.state_dir)

    def _payloads(self, name: str) -> list[dict[str, Any]]:
        event_path = (
            state.plan_state_dir(self.plan_dir, self.state_dir)
            / events.EVENTS_FILE_NAME
        )
        return [
            record["payload"]
            for record in read_jsonl(event_path)
            if record["name"] == name
        ]

    def _harness(self, status: str, last_message: str):
        def run(*_args: object, **_kwargs: object) -> harnesses.HarnessResult:
            session_path = self.plan_dir / "session-1.md"
            lines = session_path.read_text(encoding="utf-8").splitlines(keepends=True)
            for index, line in enumerate(lines):
                if line.startswith(plans.STATUS_PREFIX):
                    lines[index] = f"{plans.STATUS_PREFIX} {status}\n"
            session_path.write_text("".join(lines), encoding="utf-8")
            return harnesses.HarnessResult(
                name="codex",
                argv=["fake"],
                returncode=0,
                stdout="ok\n",
                stderr="",
                last_message=last_message,
            )

        return run

    def test_structured_pause_outranks_a_paused_handoff_status(self) -> None:
        with mock.patch.object(
            harnesses,
            "run_harness",
            side_effect=self._harness(
                "paused",
                "Runner status: paused\nRUNNER_PAUSE: blocked - needs a decision\n",
            ),
        ):
            _, payload = self._invoke("paused-status-run")

        self.assertTrue(payload["paused"])
        self.assertEqual("blocked", payload["pause_reason"])
        repair = payload["pause_details"]["handoff_status_repair"]
        self.assertEqual("paused", repair["from_status"])
        self.assertEqual("pending", repair["to_status"])
        self.assertEqual("blocked", repair["pause_reason"])

        session_text = (self.plan_dir / "session-1.md").read_text(encoding="utf-8")
        self.assertIn(f"{plans.STATUS_PREFIX} pending", session_text)
        self.assertIn("there is no paused status value", session_text)
        self.assertEqual(
            "pending", plans.inspect_plan(self.plan_dir).sessions[0].status
        )
        self.assertEqual(
            ["pending"],
            [item["status"] for item in payload["final_snapshot"]["sessions"]],
        )

        reopenings = self._payloads(events.SESSION_REOPENED_EVENT)
        self.assertEqual(1, len(reopenings))
        self.assertEqual("paused", reopenings[0]["from_status"])
        self.assertEqual("pending", reopenings[0]["to_status"])
        self.assertEqual("plan-status-validity", reopenings[0]["guard"])
        self.assertEqual("blocked", reopenings[0]["reason"])
        self.assertEqual(
            plans.inspect_plan(self.plan_dir).content_sha256,
            reopenings[0]["content_sha256"],
        )
        self.assertEqual(payload["final_snapshot"]["counts"], reopenings[0]["counts"])

    def test_unstructured_pause_leaves_an_invalid_status_terminal(self) -> None:
        with mock.patch.object(
            harnesses,
            "run_harness",
            side_effect=self._harness(
                "paused",
                "RUNNER_PAUSE: wrangler auth expired\n",
            ),
        ):
            _, payload = self._invoke("unstructured-pause-run")

        self.assertTrue(payload["paused"])
        self.assertEqual("unknown-status", payload["pause_reason"])
        self.assertNotIn("handoff_status_repair", payload["pause_details"])
        self.assertIn(
            f"{plans.STATUS_PREFIX} paused",
            (self.plan_dir / "session-1.md").read_text(encoding="utf-8"),
        )
        self.assertEqual([], self._payloads(events.SESSION_REOPENED_EVENT))


if __name__ == "__main__":
    unittest.main()
