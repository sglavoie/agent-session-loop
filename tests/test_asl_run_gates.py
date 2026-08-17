"""Run readiness, verification enforcement, and journal integration tests.

Split from tests/test_agent_session_loop.py."""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest import mock

from agent_session_loop import (
    baseline,
    cli,
    config,
    events,
    harnesses,
    plans,
    preflight,
    review,
    runner,
    session_overview,
    state,
    verification,
)
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    REPO_ROOT,
    _command_contract,
    _contract_block,
    _load_script_module,
    agent_mux_notify_enabled,
    init_git_repo,
    init_multi_repo_workspace,
    install_fake_amx,
    install_fake_codex,
    patched_env,
    read_jsonl,
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
            runner._validate_run_inputs(args)
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

            restored = runner._apply_resume_config(
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
                    runner._resume_gate_passed(self._report(state=readiness_state))
                )

    def test_resume_gate_rejects_other_states_and_findings(self) -> None:
        for readiness_state in ("draft", "reviewed", "complete"):
            with self.subTest(state=readiness_state):
                self.assertFalse(
                    runner._resume_gate_passed(self._report(state=readiness_state))
                )
        self.assertFalse(runner._resume_gate_passed(self._report(state=None)))
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
                self.assertFalse(runner._resume_gate_passed(report))

    def test_readiness_gate_still_requires_loop_ready(self) -> None:
        self.assertTrue(runner._readiness_gate_passed(self._report(state="loop-ready")))
        self.assertFalse(runner._readiness_gate_passed(self._report(state="running")))


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
            return runner._run_locked(
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
            runner.harnesses, "run_harness", side_effect=commit_then_complete
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
            runner.harnesses, "run_harness", side_effect=rewrite_then_complete
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

    def test_non_git_legacy_scope_refuses_before_harness(self) -> None:
        shutil.rmtree(self.repo_root / ".git")

        with mock.patch.object(
            runner.harnesses,
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
            return runner._run_locked(
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
                runner.harnesses,
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
            runner.harnesses,
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
            runner.harnesses,
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
            runner.harnesses,
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
            runner.harnesses,
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
                runner.harnesses,
                "run_harness",
                side_effect=self._completed_harness_result,
            ),
            mock.patch.object(
                runner,
                "_write_turn_artifacts",
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
                runner.harnesses,
                "run_harness",
                side_effect=self._completed_harness_result,
            ),
            mock.patch.object(
                runner,
                "_write_turn_artifacts",
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
            runner.harnesses,
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
            runner.harnesses,
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
                runner.harnesses,
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
    """Direct ``_run_locked`` invocation tests covering KeyboardInterrupt
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
                exit_code, payload = runner._run_locked(
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
            runner.verification,
            "run_verification",
            side_effect=raise_keyboard_interrupt,
        ):
            exit_code, payload = self._invoke_run_locked()

        self.assertEqual(runner.INTERRUPTED_EXIT, exit_code)
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
        """Cover the outer ``_run_locked`` KeyboardInterrupt wrapper.

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
            runner,
            "_append_progress_entry",
            side_effect=raise_keyboard_interrupt,
        ):
            exit_code, payload = self._invoke_run_locked()

        self.assertEqual(runner.INTERRUPTED_EXIT, exit_code)
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

    Drives ``_run_locked`` directly with a synthetic long-running verification
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
                exit_code, payload = runner._run_locked(
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

        real_enforce = runner._enforce_session_verification

        def short_heartbeat_enforce(**kwargs: object) -> bool:
            kwargs["heartbeat_interval"] = 0.05
            return real_enforce(**kwargs)

        with mock.patch.object(
            runner,
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

        real_enforce = runner._enforce_session_verification

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
                runner,
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
            if event == runner.AGENT_MUX_VERIFICATION_HEARTBEAT_EVENT
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
                runner.AGENT_MUX_START_EVENT,
                runner.AGENT_MUX_TURN_START_EVENT,
                runner.AGENT_MUX_VERIFICATION_START_EVENT,
                runner.AGENT_MUX_VERIFICATION_HEARTBEAT_EVENT,
                runner.AGENT_MUX_VERIFICATION_FINISH_EVENT,
                runner.AGENT_MUX_TURN_FINISH_EVENT,
                runner.AGENT_MUX_FINISH_EVENT,
            ],
            collapsed_events,
        )

        # Every notification carries the run identity, the daemon phase for
        # its event, and one strictly monotonic string-encoded sequence.
        run_id = payload["run_id"]
        for event, hook_payload in zip(notified_events, hook_payloads, strict=False):
            self.assertEqual(run_id, hook_payload["run_id"])
            self.assertEqual(
                runner.AGENT_MUX_PHASE_BY_EVENT[event],
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
            return runner._run_locked(
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
            runner.harnesses,
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
            runner.HANDOFF_DEFECT_UNQUALIFIED_ENTRY, corrections[0]["defect"]
        )
        self.assertEqual(1, corrections[0]["attempt"])
        self.assertEqual(
            runner.MAX_HANDOFF_CORRECTION_ATTEMPTS, corrections[0]["max_attempts"]
        )
        self.assertFalse(corrections[0]["exhausted"])
        self.assertIn("README.md", corrections[0]["detail"])
        self.assertEqual([], self._payloads(events.RUN_PAUSED_EVENT))

    def test_execution_event_precedes_each_accepted_turn(self) -> None:
        with mock.patch.object(
            runner.harnesses,
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
            runner.harnesses,
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
        self.assertEqual(runner.HANDOFF_DEFECT_UNQUALIFIED_ENTRY, evidence["defect"])
        self.assertEqual(1, len(self._payloads(events.TURN_HANDOFF_CORRECTION_EVENT)))
        # The session is still reopened so the plan parses, but a defect the
        # bounded correction already failed on is not advertised as repairable.
        reopen = payload["pause_details"]["handoff_reopen"]
        self.assertTrue(reopen["reopened"])
        self.assertFalse(reopen["repairable"])

    def test_scope_violation_is_not_treated_as_a_correctable_defect(self) -> None:
        with mock.patch.object(
            runner.harnesses,
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
            runner.harnesses,
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
            runner.harnesses,
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
            return runner._run_locked(
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
            runner.harnesses,
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

    def test_unstructured_pause_leaves_an_invalid_status_terminal(self) -> None:
        with mock.patch.object(
            runner.harnesses,
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
