"""Run readiness, verification enforcement, and journal integration tests.

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
    agent_mux_notify_enabled,
    patched_env,
    write_plan,
    install_fake_amx,
    install_fake_codex,
    init_git_repo,
    read_jsonl,
    _command_contract,
    _contract_block,
)


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
        config_path = (
            self.config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        )
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
    ) -> plans.ReadinessMetadata | None:
        contracts = {
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
        if max_pending_sessions is not None:
            argv.extend(["--max-pending-sessions", str(max_pending_sessions)])
        env = os.environ.copy()
        env.update(
            {
                "XDG_CONFIG_HOME": str(self.config_home),
                "FAKE_CODEX_MODE": mode,
                "FAKE_CODEX_SLEEP": "5",
                "PYTHONPATH": str(REPO_ROOT)
                + os.pathsep
                + env.get("PYTHONPATH", ""),
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
        self.assertEqual(
            str((self.plan_dir / "manifest.md").resolve()), repair["path"]
        )
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
        self.assertEqual(
            "complete", plans.read_readiness_metadata(self.plan_dir).state
        )

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
        self.assertEqual(
            "complete", plans.read_readiness_metadata(self.plan_dir).state
        )

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
        self.assertEqual(
            "complete", plans.read_readiness_metadata(self.plan_dir).state
        )

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

    def test_lifecycle_transitions_to_complete_when_all_sessions_finish(
        self,
    ) -> None:
        self._write_plan(("pending",), readiness="loop-ready")

        completed, payload = self._run()

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual("loop-ready", payload["readiness_state_initial"])
        self.assertEqual("complete", payload["readiness_state_final"])
        metadata = plans.read_readiness_metadata(self.plan_dir)
        self.assertEqual("complete", metadata.state)
        self.assertEqual(
            plans.stable_readiness_sha256(self.plan_dir),
            metadata.readiness_sha256,
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

    def test_lifecycle_refreshes_running_sha_after_future_session_notes(
        self,
    ) -> None:
        self._write_plan(("pending", "pending"), readiness="loop-ready")
        original_metadata = plans.read_readiness_metadata(self.plan_dir)
        self.assertIsNotNone(original_metadata)

        completed, payload = self._run(
            mode="success-future-note",
            max_sessions=1,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual("max-sessions", payload["reason"])
        self.assertEqual("loop-ready", payload["readiness_state_final"])
        self.assertIsNone(payload.get("readiness_state_refresh_error"))
        self.assertIsNotNone(payload.get("readiness_state_refreshed_at"))
        future_session = self.plan_dir / "session-2.md"
        self.assertIn(
            "Generated future-session note",
            future_session.read_text(encoding="utf-8"),
        )
        refreshed_metadata = plans.read_readiness_metadata(self.plan_dir)
        self.assertEqual("loop-ready", refreshed_metadata.state)
        self.assertEqual(
            plans.stable_readiness_sha256(self.plan_dir),
            refreshed_metadata.readiness_sha256,
        )
        self.assertNotEqual(
            original_metadata.readiness_sha256,
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
        self.assertEqual(
            ["1", "2"], [entry.session_id.value for entry in entries]
        )

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
        categories = [
            repair["category"] for repair in repair_payload["auto_repairs"]
        ]
        self.assertEqual(
            ["progress-journal-missing", "manifest-readiness"], categories
        )
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
            finding["category"]
            for finding in payload["readiness_gate"]["findings"]
        }
        self.assertIn("baseline-missing", categories)

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
            finding["category"]
            for finding in payload["readiness_gate"]["findings"]
        }
        self.assertNotIn("baseline-missing", categories)


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
        config_path = (
            self.config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        )
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
                '{"type": "file_exists", "path": "README.md", '
                '"name": "readme"}',
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
                '{"type": "file_exists", "path": "README.md", '
                '"name": "readme"}',
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
                '{"type": "file_exists", "path": "MISSING.md", '
                '"name": "missing"}',
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
                '{"type": "file_exists", "path": "MISSING.md", '
                '"name": "missing"}',
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
        self._write_plan(
            ("pending",), contracts_by_id=None, readiness=None
        )

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
        config_path = (
            self.config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        )
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
    ) -> tuple[int, dict[str, object]]:
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
        session_text = (
            self.plan_dir / "session-1.md"
        ).read_text(encoding="utf-8")
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
        self.assertEqual(
            "interrupted", finished_records[0]["payload"]["reason"]
        )
        self.assertFalse(finished_records[0]["payload"]["passed"])
        # `run_finished` was still emitted as the final lifecycle event.
        run_finished_records = [
            record
            for record in event_records
            if record["name"] == "run_finished"
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
        self.assertEqual(
            "interrupted", pause_records[0]["payload"]["reason"]
        )


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
        config_path = (
            self.config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        )
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

    def _invoke_run_locked(self) -> tuple[int, dict[str, object]]:
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
        write_plan(
            self.plan_dir, ("pending",), contracts_by_id={"1": contract}
        )

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
        check_started_index = event_names.index(
            events.VERIFICATION_CHECK_STARTED_EVENT
        )
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

        # Documented post-turn event ordering: turn_finished precedes the
        # four verification phase events, which precede run_finished.
        turn_finished_index = event_names.index("turn_finished")
        verification_started_index = event_names.index(
            events.VERIFICATION_STARTED_EVENT
        )
        verification_finished_index = event_names.index(
            events.VERIFICATION_FINISHED_EVENT
        )
        run_finished_index = event_names.index("run_finished")
        self.assertLess(turn_finished_index, verification_started_index)
        self.assertLess(verification_started_index, check_started_index)
        self.assertLess(check_started_index, check_finished_index)
        self.assertLess(check_finished_index, verification_finished_index)
        self.assertLess(verification_finished_index, run_finished_index)

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
        write_plan(
            self.plan_dir, ("pending",), contracts_by_id={"1": contract}
        )

        real_enforce = runner._enforce_session_verification

        def short_heartbeat_enforce(**kwargs: object) -> bool:
            kwargs["heartbeat_interval"] = 0.05
            return real_enforce(**kwargs)

        with (
            agent_mux_notify_enabled(),
            patched_env(
                PATH=str(self.tmp_path)
                + os.pathsep
                + os.environ.get("PATH", ""),
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
        hook_payloads = [
            json.loads(str(record["stdin"])) for record in amx_records
        ]

        # At least one verification heartbeat notification fired inside the
        # synthetic 0.3s check window (0.05s interval).
        verification_heartbeats = [
            hook_payload
            for event, hook_payload in zip(notified_events, hook_payloads)
            if event == runner.AGENT_MUX_VERIFICATION_HEARTBEAT_EVENT
        ]
        self.assertGreaterEqual(
            len(verification_heartbeats),
            1,
            f"expected verification heartbeat notifications; got: "
            f"{notified_events}",
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
                runner.AGENT_MUX_TURN_FINISH_EVENT,
                runner.AGENT_MUX_VERIFICATION_START_EVENT,
                runner.AGENT_MUX_VERIFICATION_HEARTBEAT_EVENT,
                runner.AGENT_MUX_VERIFICATION_FINISH_EVENT,
                runner.AGENT_MUX_FINISH_EVENT,
            ],
            collapsed_events,
        )

        # Every notification carries the run identity, the daemon phase for
        # its event, and one strictly monotonic string-encoded sequence.
        run_id = payload["run_id"]
        for event, hook_payload in zip(notified_events, hook_payloads):
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
            int(hook_payload["source_sequence"])
            for hook_payload in hook_payloads
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
        collapse = lambda values: [  # noqa: E731
            value
            for index, value in enumerate(values)
            if index == 0 or value != values[index - 1]
        ]
        self.assertEqual(
            collapse(internal_phases),
            collapse(
                [hook_payload["phase"] for hook_payload in hook_payloads]
            ),
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
        config_path = (
            self.config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        )
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

    def _run(self) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
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
                '{"type": "file_exists", "path": "README.md", '
                f'"name": "readme-{i}"}}',
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
        self.assertEqual(
            ["1", "2"], [entry.session_id.value for entry in entries]
        )
        self.assertEqual(
            ["complete", "complete"], [entry.status for entry in entries]
        )

        self.assertEqual(2, len(payload["turns"]))
        for turn in payload["turns"]:
            self.assertTrue(turn["progress_appended"])
            self.assertNotIn("progress_error", turn)

    def test_verification_failure_short_circuits_progress_append(self) -> None:
        contracts_by_id = {
            "1": _contract_block(
                '{"type": "file_exists", "path": "MISSING.md", '
                '"name": "missing"}',
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
            "# Progress Journal\n"
            "<!-- agent-session-loop progress-journal v999 -->\n",
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


if __name__ == "__main__":
    unittest.main()
