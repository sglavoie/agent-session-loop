"""End-to-end QA, verification repair loop, and acceptance tests.

Split from tests/test_agent_session_loop.py."""

from __future__ import annotations

import argparse
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import dataclasses
from datetime import datetime, timezone
import errno
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import pty
import select
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from unittest import mock

from agent_session_loop import (
    artifacts,
    authoring,
    baseline,
    baseline_cache,
    cli,
    compact_state,
    config,
    contracts,
    control_queue,
    events,
    harnesses,
    install_verify,
    plans,
    preflight,
    prepare,
    progress_cli,
    progress_repair,
    qna,
    review,
    review_repair,
    run_control,
    runner,
    state,
    tail,
    verification,
    workbench,
    workbench_server,
)
from agent_session_loop.formatting import (
    format_duration_seconds,
    format_turn_finished_message,
    format_turn_started_message,
)
from tests.support import (
    REPO_ROOT,
    write_plan,
    install_fake_codex,
    install_fake_harness,
    init_git_repo,
    _contract_block,
    _write_loop_ready_plan,
)


class EndToEndQATests(unittest.TestCase):
    """Synthetic end-to-end coverage that wires preflight + run + verify.

    Unit-level coverage already exercises every preflight finding, every
    runner pause reason, and every verify outcome individually. These tests
    intentionally chain those subsystems together through the CLI surface,
    because the loop-readiness contract introduced in this plan only holds
    when the pieces compose correctly. Each test is the integration analog
    of a smoke step from ``docs/agent-session-loop.md``.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.plan_dir = self.tmp_path / "plan"
        self.state_dir = self.tmp_path / "state"
        self.repo_root = self.tmp_path / "repo"
        self.config_home = self.tmp_path / "config"
        self.path_dir = self.tmp_path / "path"
        self.path_dir.mkdir()
        init_git_repo(self.repo_root)
        self.fake_codex = install_fake_codex(self.tmp_path)
        self.fake_claude = install_fake_harness(self.tmp_path, "claude")
        config_path = (
            self.config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        )
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps(
                {
                    "codex": {"executable": str(self.fake_codex)},
                    "claude": {"executable": str(self.fake_claude)},
                }
            ),
            encoding="utf-8",
        )

    def _path_value(self, *names: str) -> str:
        for name in names:
            target = self.path_dir / name
            target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            target.chmod(0o755)
        return os.pathsep.join([str(self.path_dir), os.environ.get("PATH", "")])

    def _base_env(self, *, mode: str = "success") -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "XDG_CONFIG_HOME": str(self.config_home),
                "XDG_STATE_HOME": str(self.state_dir),
                "FAKE_CODEX_MODE": mode,
                "FAKE_CODEX_SLEEP": "5",
                "PYTHONPATH": str(REPO_ROOT)
                + os.pathsep
                + env.get("PYTHONPATH", ""),
            }
        )
        return env

    def _run_preflight(
        self,
        *,
        mark_loop_ready: bool = False,
        commit_mode: str | None = None,
        path_value: str | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
        argv = [
            sys.executable,
            "-m",
            "agent_session_loop",
            "preflight",
            str(self.plan_dir),
            "--cwd",
            str(self.repo_root),
            "--harness",
            "codex",
            "--state-dir",
            str(self.state_dir),
            "--json",
        ]
        if commit_mode == "per-session":
            argv.append("--commit-per-session")
        elif commit_mode == "end":
            argv.append("--commit-at-end")
        if mark_loop_ready:
            argv.append("--mark-loop-ready")
        env = self._base_env()
        if path_value is not None:
            env["PATH"] = path_value
        completed = subprocess.run(
            argv,
            cwd=REPO_ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        payload = json.loads(completed.stdout) if completed.stdout else {}
        return completed, payload

    def _run_run(
        self,
        *,
        allow_not_loop_ready: bool = False,
        max_sessions: int | None = None,
        review_harness: str | None = None,
        max_review_fix_cycles: int | None = None,
        mode: str = "success",
        review_outcomes: str | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
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
        if review_harness is not None:
            argv.extend(["--review-harness", review_harness])
        if max_review_fix_cycles is not None:
            argv.extend(
                ["--max-review-fix-cycles", str(max_review_fix_cycles)]
            )
        env = self._base_env(mode=mode)
        if review_outcomes is not None:
            env["FAKE_CODEX_REVIEW_OUTCOMES"] = review_outcomes
        completed = subprocess.run(
            argv,
            cwd=REPO_ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        payload = json.loads(completed.stdout)
        return completed, payload

    def _run_autopilot_review(
        self,
        *,
        max_review_fix_cycles: int,
        review_outcomes: str,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
        """Supervise the real CLI through the standalone Autopilot helper."""

        asl_wrapper = self.tmp_path / "asl"
        asl_wrapper.write_text(
            "#!/usr/bin/env python3\n"
            "from agent_session_loop.cli import main\n"
            "raise SystemExit(main())\n",
            encoding="utf-8",
        )
        asl_wrapper.chmod(asl_wrapper.stat().st_mode | stat.S_IXUSR)
        pythonpath = str(REPO_ROOT) + os.pathsep + os.environ.get(
            "PYTHONPATH", ""
        )
        command = shlex.join(
            [
                f"XDG_CONFIG_HOME={self.config_home}",
                f"XDG_STATE_HOME={self.state_dir}",
                "FAKE_CODEX_MODE=success-with-review",
                f"FAKE_CODEX_REVIEW_OUTCOMES={review_outcomes}",
                f"PYTHONPATH={pythonpath}",
                str(asl_wrapper),
                "run",
                str(self.plan_dir),
                "--cwd",
                str(self.repo_root),
                "--harness",
                "codex",
                "--review-harness",
                "claude",
                "--max-review-fix-cycles",
                str(max_review_fix_cycles),
                "--state-dir",
                str(self.state_dir),
                "--json",
            ]
        )
        completed = subprocess.run(
            [
                sys.executable,
                str(
                    REPO_ROOT
                    / "skills"
                    / "asl"
                    / "scripts"
                    / "asl-autopilot.py"
                ),
                "--command",
                command,
                "--json",
            ],
            cwd=REPO_ROOT,
            env=self._base_env(mode="success-with-review"),
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        payload = json.loads(completed.stdout) if completed.stdout else {}
        return completed, payload

    def test_completed_review_reopens_and_rereviews_through_cli(self) -> None:
        """A higher absolute cap resumes the persisted review in a new run."""

        _write_loop_ready_plan(
            self.plan_dir,
            ("pending",),
            contracts_by_id={
                "1": _contract_block(
                    '{"type": "file_exists", "path": "README.md", '
                    '"name": "readme"}',
                    session="1",
                )
            },
        )
        outcomes = "findings,all-clear"

        first, first_payload = self._run_run(
            review_harness="claude",
            max_review_fix_cycles=0,
            mode="success-with-review",
            review_outcomes=outcomes,
        )
        self.assertEqual(0, first.returncode, first.stderr)
        self.assertEqual(
            ["1", "2"],
            [turn["session_id"] for turn in first_payload["turns"]],
        )
        self.assertEqual(
            "higher-cap-required", first_payload["review_resume"]["blocker"]
        )

        second, second_payload = self._run_run(
            review_harness="claude",
            max_review_fix_cycles=1,
            mode="success-with-review",
            review_outcomes=outcomes,
        )

        self.assertEqual(0, second.returncode, second.stderr)
        self.assertEqual(
            ["2.1", "3"],
            [turn["session_id"] for turn in second_payload["turns"]],
        )
        self.assertEqual("all-clear", second_payload["review_fix_disposition"])
        self.assertEqual("terminal", second_payload["review_resume"]["action"])
        reopen = second_payload["review_reopen"]
        self.assertTrue(reopen["materialized"])
        self.assertEqual("2", reopen["source_review_session_id"])
        self.assertEqual(["2.1"], reopen["repair_session_ids"])
        self.assertTrue((self.plan_dir / "session-3.md").is_file())

    def test_autopilot_reopens_exact_legacy_review_history_after_normalization(
        self,
    ) -> None:
        """Legacy not-loop-ready + digestless malformed history resumes."""

        _write_loop_ready_plan(
            self.plan_dir,
            ("pending",),
            contracts_by_id={
                "1": _contract_block(
                    '{"type": "file_exists", "path": "README.md", '
                    '"name": "readme"}',
                    session="1",
                )
            },
        )
        seeded, seeded_payload = self._run_run(
            review_harness="claude",
            max_review_fix_cycles=1,
            mode="success-with-review",
            review_outcomes="findings,all-clear",
        )
        self.assertEqual(0, seeded.returncode, seeded.stderr)
        self.assertEqual(
            ["1", "2", "2.1", "3"],
            [turn["session_id"] for turn in seeded_payload["turns"]],
        )

        # Reproduce the motivating persisted history: the first source did
        # materialize a child but stopped not-loop-ready, and the later source
        # recorded malformed repair output before artifact digests existed.
        event_path = (
            state.plan_state_dir(self.plan_dir, self.state_dir)
            / events.EVENTS_FILE_NAME
        )
        records = [
            json.loads(line)
            for line in event_path.read_text(encoding="utf-8").splitlines()
        ]
        cycles = [
            record["payload"]
            for record in records
            if record["name"] == events.REVIEW_CYCLE_EVENT
        ]
        self.assertEqual(2, len(cycles))
        cycles[0]["disposition"] = "not-loop-ready"
        cycles[0].pop("review_output_sha256", None)
        cycles[0].pop("review_artifact_sha256", None)
        cycles[1].update(
            {
                "outcome": "findings",
                "disposition": "malformed-review-repair",
                "fingerprints": ["finding:legacy-nine-groups"],
                "introduced_findings": ["finding:legacy-nine-groups"],
                "closed_findings": [],
                "repeated_findings": [],
                "repair_session_ids": [],
                "max_review_fix_cycles": 1,
            }
        )
        cycles[1].pop("review_output_sha256", None)
        cycles[1].pop("review_artifact_sha256", None)
        event_path.write_text(
            "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
            encoding="utf-8",
        )

        findings = []
        for index in range(1, 10):
            findings.append(
                {
                    "id": f"legacy-{index}",
                    "severity": "high",
                    "title": f"Legacy finding {index}",
                    "evidence": [
                        {
                            "path": f"agent_session_loop/legacy_{index}.py:{index}",
                            "symbol": f"target_{index}",
                        }
                    ],
                    "explanation": "Compatibility fixture finding.",
                    "repair_group": f"group-{index}",
                    "done_criteria": ["The compatibility defect is fixed."],
                    "verification_checks": [
                        {
                            "type": "command",
                            "name": f"legacy-check-{index}",
                            "argv": ["python3", "-c", "pass"],
                        }
                    ],
                }
            )
        artifact_path = self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME
        artifact_path.write_text(
            json.dumps(
                {"version": 2, "outcome": "findings", "findings": findings},
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        (self.plan_dir / review.FINAL_REVIEW_FILENAME).write_text(
            "## Findings\n"
            "- Legacy review repair grouping requires normalization.\n"
            "Status: 9 Findings (9 high, 0 med, 0 low)\n",
            encoding="utf-8",
        )

        invalid, invalid_payload = self._run_autopilot_review(
            max_review_fix_cycles=2,
            review_outcomes="findings,all-clear,all-clear",
        )
        self.assertEqual(1, invalid.returncode, invalid.stderr)
        self.assertEqual("review_findings", invalid_payload["reason"])
        self.assertEqual(0, invalid_payload["chunks"])
        self.assertEqual(
            "artifact-invalid", invalid_payload["review_resume"]["blocker"]
        )
        self.assertIn(
            "exceed the maximum of 8",
            invalid_payload["review_resume"]["artifact_error"],
        )
        self.assertEqual(1, invalid_payload["review_resume"]["historical_materializations"])

        # Artifact correction is external. Preserve all nine findings while
        # normalizing them from nine repair groups to the supported eight.
        findings[-1]["repair_group"] = "group-8"
        artifact_path.write_text(
            json.dumps(
                {"version": 2, "outcome": "findings", "findings": findings},
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

        resumed, resumed_payload = self._run_autopilot_review(
            max_review_fix_cycles=2,
            review_outcomes="findings,all-clear,all-clear",
        )
        self.assertEqual(0, resumed.returncode, resumed.stderr)
        self.assertTrue(resumed_payload["ok"])
        self.assertEqual("complete", resumed_payload["reason"])
        self.assertEqual(1, resumed_payload["chunks"])
        self.assertEqual("all-clear", resumed_payload["review"]["outcome"])
        self.assertEqual("terminal", resumed_payload["review_resume"]["action"])
        for index in range(1, 9):
            child = self.plan_dir / f"session-3.{index}.md"
            self.assertTrue(child.is_file(), child)
            self.assertIn("- **Status**: complete", child.read_text(encoding="utf-8"))
        self.assertIn(
            "- **Status**: complete",
            (self.plan_dir / "session-4.md").read_text(encoding="utf-8"),
        )

        final_records = [
            json.loads(line)
            for line in event_path.read_text(encoding="utf-8").splitlines()
        ]
        reopen = [
            record["payload"]
            for record in final_records
            if record["name"] == events.REVIEW_REPAIR_REOPENED_EVENT
            and record["payload"].get("materialized") is True
            and record["payload"].get("source_review_session_id") == "3"
        ]
        self.assertEqual(1, len(reopen))
        self.assertEqual(1, reopen[0]["historical_materializations_before"])
        self.assertEqual(2, reopen[0]["historical_materializations_after"])
        self.assertIsNone(reopen[0]["source_review_output_sha256"])
        self.assertIsNone(reopen[0]["source_review_artifact_sha256"])
        self.assertEqual(
            [f"3.{index}" for index in range(1, 9)],
            reopen[0]["repair_session_ids"],
        )

    def _write_legacy_plan(self) -> None:
        """A pre-loop-readiness plan: no manifest readiness, no contract."""
        write_plan(self.plan_dir, ("pending",))

    def _repair_legacy_plan_to_draft(self) -> None:
        """Repair legacy content while leaving readiness for preflight to mark."""
        contract = _contract_block(
            '{"type": "file_exists", "path": "README.md", "name": "readme"}',
            session="1",
        )
        write_plan(
            self.plan_dir, ("pending",), contracts_by_id={"1": contract}
        )
        sha = plans.stable_readiness_sha256(self.plan_dir)
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="draft",
                readiness_sha256=sha,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )

    def test_full_lifecycle_repairs_legacy_plan_and_runs_to_completion(
        self,
    ) -> None:
        """Legacy plan → preflight red → repair → mark → run → verify pass."""
        self._write_legacy_plan()
        path_value = self._path_value("codex", "spec", "amx")

        # Step 1: preflight surfaces red findings on the legacy plan.
        before, before_payload = self._run_preflight(
            commit_mode="per-session", path_value=path_value
        )
        self.assertEqual(1, before.returncode)
        self.assertFalse(before_payload["ok"])
        before_categories = {
            f["category"] for f in before_payload["findings"]
        }
        self.assertIn("manifest-readiness", before_categories)
        self.assertIn("missing-contract", before_categories)
        # Repair hints stay aligned with findings 1:1 for the guided skill.
        self.assertEqual(
            len(before_payload["findings"]),
            len(before_payload["repair_hints"]),
        )

        # Step 2: repair (add contract, write draft readiness).
        self._repair_legacy_plan_to_draft()
        readiness_before_mark = plans.read_readiness_metadata(self.plan_dir)
        self.assertEqual("draft", readiness_before_mark.state)

        # Step 3: --mark-loop-ready promotes a clean draft plan.
        marked, marked_payload = self._run_preflight(
            commit_mode="per-session",
            mark_loop_ready=True,
            path_value=path_value,
        )
        self.assertEqual(0, marked.returncode, marked.stderr)
        self.assertTrue(marked_payload["ok"])
        self.assertEqual("loop-ready-marked", marked_payload["reason"])
        self.assertTrue(marked_payload["marked_loop_ready"])
        readiness_after_mark = plans.read_readiness_metadata(self.plan_dir)
        self.assertIsNotNone(readiness_after_mark)
        self.assertEqual("loop-ready", readiness_after_mark.state)
        self.assertEqual(
            plans.stable_readiness_sha256(self.plan_dir),
            readiness_after_mark.readiness_sha256,
        )
        self.assertNotEqual(readiness_before_mark.state, readiness_after_mark.state)

        # Step 4: asl run completes the plan with verification passing.
        run_completed, run_payload = self._run_run()
        self.assertEqual(0, run_completed.returncode, run_completed.stderr)
        self.assertTrue(run_payload["ok"])
        self.assertFalse(run_payload["paused"])
        self.assertEqual("all-complete", run_payload["reason"])
        self.assertEqual("complete", run_payload["readiness_state_final"])
        session_text = (self.plan_dir / "session-1.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("- **Status**: complete", session_text)
        verify_payload = run_payload["turns"][0]["verification"]
        self.assertEqual("ok", verify_payload["reason"])
        self.assertTrue(verify_payload["passed"])

    def test_verification_failure_pauses_then_resumes_after_repair(self) -> None:
        """`verify-failed` reverts status; re-running after repair completes."""
        contract = _contract_block(
            '{"type": "file_exists", "path": "later.txt", "name": "later"}',
            session="1",
        )
        write_plan(
            self.plan_dir, ("pending",), contracts_by_id={"1": contract}
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

        # First run: verify fails, status reverts to pending.
        first_completed, first_payload = self._run_run()
        self.assertEqual(1, first_completed.returncode)
        self.assertTrue(first_payload["paused"])
        self.assertEqual("verify-failed", first_payload["pause_reason"])
        pause_details = first_payload["pause_details"]
        self.assertEqual(["later"], pause_details["recipe"]["names"])
        self.assertEqual("file-missing", pause_details["failures"][0][
            "failure_reason"
        ])
        session_path = self.plan_dir / "session-1.md"
        session_text = session_path.read_text(encoding="utf-8")
        self.assertIn("- **Status**: pending", session_text)
        self.assertIn("verification failed", session_text)

        # A verify-failed pause flips readiness to `reviewed`; after the
        # underlying issue is repaired, preflight can re-mark the plan.
        reviewed_metadata = plans.read_readiness_metadata(self.plan_dir)
        self.assertEqual("reviewed", reviewed_metadata.state)

        # Repair: create the missing file so the contract will now pass.
        (self.repo_root / "later.txt").write_text("done\n", encoding="utf-8")

        remark_completed, remark_payload = self._run_preflight(mark_loop_ready=True)
        self.assertEqual(0, remark_completed.returncode, remark_completed.stderr)
        self.assertTrue(remark_payload["marked_loop_ready"])
        self.assertEqual("loop-ready", remark_payload["readiness"]["state"])

        # Second run: the still-pending session is picked up and completes.
        second_completed, second_payload = self._run_run()
        self.assertEqual(0, second_completed.returncode, second_completed.stderr)
        self.assertTrue(second_payload["ok"])
        self.assertFalse(second_payload["paused"])
        self.assertEqual("all-complete", second_payload["reason"])
        final_session = session_path.read_text(encoding="utf-8")
        self.assertIn("- **Status**: complete", final_session)
        # Readiness lifecycle resolves to complete after the resumed run.
        final_metadata = plans.read_readiness_metadata(self.plan_dir)
        self.assertEqual("complete", final_metadata.state)

    def test_stale_lock_surfaces_in_preflight_cli_with_repair_hint(
        self,
    ) -> None:
        """A stale lock JSON-fingerprints to env-lock.stale via the CLI."""
        _write_loop_ready_plan(
            self.plan_dir,
            ("pending",),
            contracts_by_id={
                "1": _contract_block(
                    '{"type": "file_exists", "path": "README.md", '
                    '"name": "readme"}',
                    session="1",
                )
            },
        )
        plan_state = state.plan_state_dir(self.plan_dir, self.state_dir)
        plan_state.mkdir(parents=True, exist_ok=True)
        lock_path = state.lock_path_for_plan(self.plan_dir, self.state_dir)
        lock_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "pid": 0,
                    "plan_dir": str(plans.canonicalize_path(self.plan_dir)),
                    "created_at": "2026-06-19T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        path_value = self._path_value("codex", "spec", "amx")
        completed, payload = self._run_preflight(
            commit_mode="per-session", path_value=path_value
        )
        self.assertEqual(1, completed.returncode)
        self.assertFalse(payload["ok"])
        lock_findings = [
            f for f in payload["findings"] if f["category"] == "env-lock"
        ]
        self.assertEqual(1, len(lock_findings))
        self.assertEqual("yellow", lock_findings[0]["severity"])
        self.assertIn("stale lock", lock_findings[0]["message"])
        # Each finding pairs with a stable repair hint check id.
        lock_hints = [
            hint
            for hint, finding in zip(
                payload["repair_hints"], payload["findings"]
            )
            if finding["category"] == "env-lock"
        ]
        self.assertEqual(1, len(lock_hints))
        self.assertEqual("env-lock.stale", lock_hints[0]["check_id"])

    def test_commit_mode_dirty_worktree_emits_yellow_via_preflight_cli(
        self,
    ) -> None:
        """`--commit-per-session` plus a dirty worktree surfaces env-git yellow."""
        _write_loop_ready_plan(
            self.plan_dir,
            ("pending",),
            contracts_by_id={
                "1": _contract_block(
                    '{"type": "file_exists", "path": "README.md", '
                    '"name": "readme"}',
                    session="1",
                )
            },
        )
        (self.repo_root / "dirty.txt").write_text(
            "uncommitted\n", encoding="utf-8"
        )
        path_value = self._path_value("codex", "spec", "amx")
        completed, payload = self._run_preflight(
            commit_mode="per-session", path_value=path_value
        )
        self.assertEqual(1, completed.returncode)
        self.assertFalse(payload["ok"])
        git_findings = [
            f for f in payload["findings"] if f["category"] == "env-git"
        ]
        self.assertEqual(1, len(git_findings))
        self.assertEqual("yellow", git_findings[0]["severity"])
        self.assertIn("dirty", git_findings[0]["message"])
        # Marking loop-ready must be refused while yellow findings remain.
        completed_mark, payload_mark = self._run_preflight(
            commit_mode="per-session",
            mark_loop_ready=True,
            path_value=path_value,
        )
        self.assertEqual(1, completed_mark.returncode)
        self.assertFalse(payload_mark["marked_loop_ready"])

    def test_json_contracts_share_stable_keys_across_preflight_and_verify(
        self,
    ) -> None:
        """The JSON shape published in the docs must hold at the CLI surface."""
        contract = _contract_block(
            '{"type": "file_exists", "path": "README.md", "name": "readme"}',
            session="1",
        )
        write_plan(
            self.plan_dir, ("pending",), contracts_by_id={"1": contract}
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
        path_value = self._path_value("codex", "spec", "amx")

        # Preflight JSON: documented top-level keys.
        preflight_completed, preflight_payload = self._run_preflight(
            commit_mode="per-session", path_value=path_value
        )
        self.assertEqual(0, preflight_completed.returncode)
        for key in (
            "ok",
            "command",
            "reason",
            "findings",
            "findings_by_severity",
            "repair_hints",
            "readiness",
            "expected_readiness_sha256",
            "mark_loop_ready_requested",
            "marked_loop_ready",
        ):
            self.assertIn(key, preflight_payload)
        self.assertEqual("preflight", preflight_payload["command"])

        # Verify JSON: documented top-level keys including stable reason enum.
        verify_argv = [
            sys.executable,
            "-m",
            "agent_session_loop",
            "verify",
            str(self.plan_dir),
            "--session",
            "1",
            "--cwd",
            str(self.repo_root),
            "--json",
        ]
        verify_completed = subprocess.run(
            verify_argv,
            cwd=REPO_ROOT,
            env=self._base_env(),
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(0, verify_completed.returncode, verify_completed.stderr)
        verify_payload = json.loads(verify_completed.stdout)
        for key in (
            "ok",
            "command",
            "reason",
            "passed",
            "recipe",
            "checks",
            "session",
            "cwd",
            "plan_dir",
        ):
            self.assertIn(key, verify_payload)
        self.assertEqual("verify", verify_payload["command"])
        self.assertEqual("ok", verify_payload["reason"])


class VerificationRepairLoopTests(unittest.TestCase):
    """Packet mode + fingerprint-aware verification repair (Session 10).

    Covers three guarantees: autonomous runs use the Python-built task packet
    instead of the ``session-run`` skill (with an ``ASL_WORKER_MODE=skill``
    rollback), a relaunch injects prior-failure repair context into the packet,
    and an identical verification failure is fingerprinted and marked exhausted
    so it cannot loop while a changed failure stays eligible.
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
        config_path = (
            self.config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        )
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps({"codex": {"executable": str(self.fake_codex)}}),
            encoding="utf-8",
        )

    def _write_plan(self, *, path: str = "MISSING.md", name: str = "missing") -> None:
        contracts_by_id = {
            "1": _contract_block(
                f'{{"type": "file_exists", "path": "{path}", "name": "{name}"}}',
                session="1",
            )
        }
        write_plan(self.plan_dir, ("pending",), contracts_by_id=contracts_by_id)
        sha = plans.stable_readiness_sha256(self.plan_dir)
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=sha,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )

    def _run(
        self, *, worker_mode: str | None = None
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
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
                "PYTHONPATH": str(REPO_ROOT)
                + os.pathsep
                + env.get("PYTHONPATH", ""),
            }
        )
        if worker_mode is not None:
            env["ASL_WORKER_MODE"] = worker_mode
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

    @staticmethod
    def _turn_prompt(payload: dict) -> str:
        # Codex passes the prompt as the final argv token.
        return str(payload["turns"][0]["argv"][-1])

    # --- packet vs skill prompt wiring -----------------------------------

    def test_packet_mode_is_default_and_avoids_session_run(self) -> None:
        self._write_plan(path="README.md", name="readme")

        completed, payload = self._run()

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        prompt = self._turn_prompt(payload)
        self.assertTrue(prompt.startswith(runner.TASK_PACKET_HEADING_PREFIX))
        self.assertNotIn("session-run", prompt)

    def test_skill_worker_mode_restores_session_run_invocation(self) -> None:
        self._write_plan(path="README.md", name="readme")

        completed, payload = self._run(worker_mode="skill")

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        prompt = self._turn_prompt(payload)
        self.assertTrue(prompt.startswith("$session-run "))
        self.assertIn("--runner-managed", prompt)

    # --- fingerprint recording + repair context --------------------------

    def test_first_failure_records_fingerprint_and_attempt(self) -> None:
        self._write_plan()

        completed, payload = self._run()

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["paused"])
        self.assertEqual("verify-failed", payload["pause_reason"])
        details = payload["pause_details"]
        self.assertIsInstance(details["verification_fingerprint"], str)
        self.assertTrue(
            details["verification_fingerprint"].startswith("verify-failed:")
        )
        self.assertEqual(1, details["verification_repair_attempt"])
        self.assertFalse(details["verification_repair_exhausted"])
        verify_payload = payload["turns"][0]["verification"]
        self.assertEqual(
            details["verification_fingerprint"],
            verify_payload["verification_fingerprint"],
        )

    def test_relaunch_injects_prior_failure_into_packet(self) -> None:
        self._write_plan()

        _first_completed, first_payload = self._run()
        fingerprint = first_payload["pause_details"]["verification_fingerprint"]

        _second_completed, second_payload = self._run()

        prompt = self._turn_prompt(second_payload)
        self.assertTrue(prompt.startswith(runner.TASK_PACKET_HEADING_PREFIX))
        self.assertIn("Verification repair references", prompt)
        self.assertIn("Prior verification attempt 1 failed", prompt)
        self.assertIn(fingerprint, prompt)
        self.assertIn("Failed check `missing`", prompt)

    def test_identical_failure_marks_repair_exhausted(self) -> None:
        self._write_plan()

        _first_completed, first_payload = self._run()
        _second_completed, second_payload = self._run()

        first_details = first_payload["pause_details"]
        second_details = second_payload["pause_details"]
        self.assertEqual(
            first_details["verification_fingerprint"],
            second_details["verification_fingerprint"],
        )
        self.assertEqual(2, second_details["verification_repair_attempt"])
        self.assertTrue(second_details["verification_repair_exhausted"])

    # --- pure helper coverage --------------------------------------------

    def test_fingerprint_is_stable_and_output_sensitive(self) -> None:
        base = {
            "failures": [
                {
                    "name": "unit",
                    "kind": "command",
                    "failure_reason": "command-failed",
                    "returncode": 1,
                    "stdout_sha256": "abc",
                    "stderr_sha256": "",
                }
            ]
        }
        same = json.loads(json.dumps(base))
        changed = json.loads(json.dumps(base))
        changed["failures"][0]["stdout_sha256"] = "def"

        self.assertEqual(
            runner._verification_fingerprint(base),
            runner._verification_fingerprint(same),
        )
        self.assertNotEqual(
            runner._verification_fingerprint(base),
            runner._verification_fingerprint(changed),
        )
        self.assertIsNone(runner._verification_fingerprint({"failures": []}))
        self.assertIsNone(runner._verification_fingerprint(None))

    def test_repair_exhaustion_decision(self) -> None:
        # An identical fingerprint that already appeared is exhausted.
        self.assertTrue(
            runner._is_verification_repair_exhausted(
                "verify-failed:x", ("verify-failed:x",), 2
            )
        )
        # A changed fingerprint under the attempt cap stays eligible.
        self.assertFalse(
            runner._is_verification_repair_exhausted(
                "verify-failed:y", ("verify-failed:x",), 2
            )
        )
        # Exceeding the attempt cap is exhausted regardless of fingerprint.
        self.assertTrue(
            runner._is_verification_repair_exhausted(
                "verify-failed:z",
                ("verify-failed:x", "verify-failed:y"),
                runner.MAX_VERIFICATION_REPAIR_ATTEMPTS + 1,
            )
        )
        # No fingerprint on the first attempt is not exhausted.
        self.assertFalse(
            runner._is_verification_repair_exhausted(None, (), 1)
        )

    def test_read_prior_verification_failures_filters_by_session(self) -> None:
        event_path = self.tmp_path / "events.jsonl"

        def _pause(session: str, reason: str, sha: str) -> None:
            events.append_event(
                event_path,
                events.Event(
                    name=events.RUN_PAUSED_EVENT,
                    payload={
                        "reason": reason,
                        "details": {
                            "session": session,
                            "failures": [
                                {
                                    "name": "missing",
                                    "kind": "file_exists",
                                    "failure_reason": "file-missing",
                                    "returncode": None,
                                    "stdout_sha256": sha,
                                    "stderr_sha256": "",
                                }
                            ],
                        },
                    },
                ),
            )

        _pause("1", "verify-failed", "a")
        _pause("2", "verify-failed", "b")
        _pause("1", "timeout", "c")
        _pause("1", "verify-failed", "d")

        priors = runner._read_prior_verification_failures(event_path, "1")
        self.assertEqual(2, len(priors))
        self.assertEqual(["a", "d"], [
            p["failures"][0]["stdout_sha256"] for p in priors
        ])

        state_info = runner._verification_repair_state(event_path, "1")
        self.assertEqual(3, state_info["attempt"])
        self.assertEqual(2, len(state_info["prior_fingerprints"]))
        references = state_info["references"]
        self.assertTrue(
            any("Prior verification attempt 2 failed" in ref for ref in references)
        )
        self.assertTrue(
            any("Failed check `missing`" in ref for ref in references)
        )


if __name__ == "__main__":
    unittest.main()
