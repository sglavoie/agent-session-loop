"""Workbench summary tests.

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
    DEFAULT_MULTI_REPO_SPECS,
    init_multi_repo_workspace,
    write_multi_repo_plan,
    write_plan,
    write_completed_review_plan,
    write_review_cycle_event,
    _structured_review_finding,
    _WORKBENCH_DEAD_PID,
    _make_workbench_plan,
    _write_state_payload,
    _write_lock_file,
    _discovered,
)


class WorkbenchSummaryTests(unittest.TestCase):
    def test_registered_summary_exposes_workspace_and_ordered_repositories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            specs = DEFAULT_MULTI_REPO_SPECS[:2]
            workspace_root = tmp_path / "workspace"
            init_multi_repo_workspace(workspace_root, specs)
            plan_dir = tmp_path / "plan"
            write_multi_repo_plan(plan_dir, ("pending",), specs=specs)
            registry = plans.read_repository_registry(plan_dir)
            assert registry is not None
            plans.save_workspace_plan_metadata(
                plan_dir,
                workspace_root,
                registry.digest,
                ("Library/Protected.dv",),
            )

            payload = workbench.build_workbench_summary(
                _discovered(plan_dir), state_dir=tmp_path / "state"
            ).to_payload()

            self.assertEqual(str(workspace_root.resolve()), payload["cwd"])
            self.assertEqual(str(workspace_root.resolve()), payload["workspace_root"])
            self.assertEqual(2, payload["repository_count"])
            self.assertEqual(
                ["Library/Protected.dv"],
                payload["workspace_inventory_excludes"],
            )
            self.assertEqual(["SVC", "OAK"], [row["alias"] for row in payload["repositories"]])

    def test_invalid_when_plan_directory_does_not_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            missing = tmp_path / "ghost"
            discovered = _discovered(missing, exists=False)

            summary = workbench.build_workbench_summary(
                discovered, state_dir=tmp_path / "state"
            )

            self.assertEqual("invalid", summary.primary_state)
            self.assertEqual("repair-plan", summary.recommended_action)
            self.assertFalse(summary.exists)
            self.assertIsNotNone(summary.detail)
            self.assertIn(str(missing), summary.detail)

    def test_invalid_when_plan_files_are_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = tmp_path / "broken"
            plan_dir.mkdir()
            (plan_dir / "context.md").write_text("# C\n", encoding="utf-8")
            discovered = _discovered(plans.canonicalize_path(plan_dir))

            summary = workbench.build_workbench_summary(
                discovered, state_dir=tmp_path / "state"
            )

            self.assertEqual("invalid", summary.primary_state)
            self.assertEqual("repair-plan", summary.recommended_action)
            self.assertTrue(summary.exists)
            self.assertIn("manifest.md", summary.detail or "")
            self.assertIsNotNone(summary.state_path)
            self.assertIsNotNone(summary.progress_path)

    def test_ready_state_without_runner_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            discovered = _discovered(plan_dir)

            summary = workbench.build_workbench_summary(
                discovered, state_dir=tmp_path / "state"
            )

            self.assertEqual("ready", summary.primary_state)
            self.assertEqual("run", summary.recommended_action)
            self.assertEqual("1", summary.next_pending_session_id)
            self.assertIsNotNone(summary.next_pending_session_path)
            self.assertEqual(2, summary.counts["pending"])
            self.assertIsNone(summary.run_id)
            self.assertEqual((), summary.warnings)

    def test_complete_when_no_pending_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = tmp_path / "done-plan"
            write_plan(plan_dir, ("complete", "complete"))
            canonical = plans.canonicalize_path(plan_dir)
            discovered = _discovered(canonical)

            summary = workbench.build_workbench_summary(
                discovered, state_dir=tmp_path / "state"
            )

            self.assertEqual("complete", summary.primary_state)
            self.assertEqual("view", summary.recommended_action)
            self.assertIsNone(summary.next_pending_session_id)
            self.assertEqual(2, summary.counts["complete"])

    def test_review_resume_cap_guidance_matches_status_and_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = write_completed_review_plan(
                tmp_path / "review-budget",
                [_structured_review_finding()],
            )
            state_root = tmp_path / "state"
            write_review_cycle_event(
                plan_dir,
                state_root,
                {
                    "cycle": 1,
                    "review_session_id": "2",
                    "disposition": "cycles-disabled",
                    "fingerprints": ["finding:legacy"],
                    "repair_session_ids": [],
                    "max_review_fix_cycles": 0,
                },
            )
            plan_before = {
                path.relative_to(plan_dir): path.read_bytes()
                for path in plan_dir.rglob("*")
                if path.is_file()
            }
            state_before = {
                path.relative_to(state_root): path.read_bytes()
                for path in state_root.rglob("*")
                if path.is_file()
            }

            summary = workbench.build_workbench_summary(
                _discovered(plan_dir), state_dir=state_root
            )
            status_payload = state.status_payload(
                plans.inspect_plan(plan_dir), state_root
            )

            self.assertEqual("complete", summary.primary_state)
            self.assertEqual("view", summary.recommended_action)
            self.assertIn("cap of 1", summary.next_action_hint)
            self.assertEqual(status_payload["review_resume"], summary.review_resume)
            self.assertEqual(
                summary.review_resume, summary.recovery["review_resume"]
            )
            self.assertEqual(
                "higher-cap-required", summary.review_resume["blocker"]
            )
            self.assertEqual(
                plan_before,
                {
                    path.relative_to(plan_dir): path.read_bytes()
                    for path in plan_dir.rglob("*")
                    if path.is_file()
                },
            )
            self.assertEqual(
                state_before,
                {
                    path.relative_to(state_root): path.read_bytes()
                    for path in state_root.rglob("*")
                    if path.is_file()
                },
            )

    def test_review_resume_without_recorded_cap_requests_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = write_completed_review_plan(
                tmp_path / "review-cap-required",
                [_structured_review_finding()],
            )
            state_root = tmp_path / "state"

            summary = workbench.build_workbench_summary(
                _discovered(plan_dir), state_dir=state_root
            )
            recovery = workbench.build_workbench_recovery(
                _discovered(plan_dir), state_dir=state_root
            )

            self.assertEqual("complete", summary.primary_state)
            self.assertEqual(
                "higher-cap-required", summary.review_resume["blocker"]
            )
            self.assertIsNone(summary.review_resume["selected_cap"])
            self.assertEqual(
                "Review repairs require a review-fix cap to resume",
                summary.detail,
            )
            self.assertIn(
                "Supply a review-fix cap of 1", summary.next_action_hint
            )
            self.assertNotIn("exhaust", summary.detail.lower())
            self.assertEqual("Review-fix cap required", recovery.title)
            self.assertIn("after supplying", recovery.summary)
            self.assertIn("--review-harness", recovery.actions[0].description)
            self.assertNotIn("exhaust", recovery.summary.lower())
            self.assertEqual(recovery.summary, summary.recovery["summary"])

    def test_missing_review_repairs_surface_inconsistent_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = write_completed_review_plan(
                tmp_path / "review-missing-repairs",
                [_structured_review_finding()],
            )
            state_root = tmp_path / "state"
            write_review_cycle_event(
                plan_dir,
                state_root,
                {
                    "cycle": 1,
                    "review_session_id": "2",
                    "disposition": "repairs-materialized",
                    "fingerprints": ["finding:legacy"],
                    "repair_session_ids": ["2.1"],
                    "max_review_fix_cycles": 1,
                },
            )

            summary = workbench.build_workbench_summary(
                _discovered(plan_dir), state_dir=state_root
            )
            recovery = workbench.build_workbench_recovery(
                _discovered(plan_dir), state_dir=state_root
            )

            self.assertEqual("complete", summary.primary_state)
            self.assertEqual("terminal", summary.review_resume["action"])
            self.assertEqual(
                "repair-sessions-missing", summary.review_resume["blocker"]
            )
            self.assertEqual("error", recovery.severity)
            self.assertEqual("Review continuation is inconsistent", recovery.title)
            self.assertEqual(recovery.title, summary.next_action_hint)
            self.assertEqual(recovery.summary, summary.detail)
            self.assertNotEqual("No pending sessions remain", summary.detail)

    def test_corrected_malformed_review_becomes_rerunnable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            findings = [
                _structured_review_finding(
                    finding_id=f"F{index}", repair_group=f"g{index}"
                )
                for index in range(9)
            ]
            plan_dir = write_completed_review_plan(
                tmp_path / "review-malformed", findings
            )
            state_root = tmp_path / "state"
            write_review_cycle_event(
                plan_dir,
                state_root,
                {
                    "cycle": 1,
                    "review_session_id": "2",
                    "disposition": "malformed-review-repair",
                    "fingerprints": [],
                    "repair_session_ids": [],
                    "max_review_fix_cycles": 1,
                },
            )

            blocked = workbench.build_workbench_summary(
                _discovered(plan_dir), state_dir=state_root
            )
            self.assertEqual("complete", blocked.primary_state)
            self.assertEqual("artifact-invalid", blocked.review_resume["blocker"])
            self.assertIn("Correct", blocked.next_action_hint)

            findings[-1]["repair_group"] = "g7"
            (plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
                json.dumps(
                    {"version": 2, "outcome": "findings", "findings": findings}
                ),
                encoding="utf-8",
            )
            ready = workbench.build_workbench_summary(
                _discovered(plan_dir), state_dir=state_root
            )
            self.assertEqual("recent", ready.primary_state)
            self.assertEqual("run", ready.recommended_action)
            self.assertTrue(ready.review_resume["eligible"])
            self.assertIn("Resume review repairs", ready.next_action_hint)

    def test_malformed_review_pause_without_turn_uses_recovery_summary(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            findings = [
                _structured_review_finding(
                    finding_id=f"F{index}", repair_group=f"g{index}"
                )
                for index in range(9)
            ]
            plan_dir = write_completed_review_plan(
                tmp_path / "review-malformed-paused", findings
            )
            state_root = tmp_path / "state"
            write_review_cycle_event(
                plan_dir,
                state_root,
                {
                    "cycle": 1,
                    "review_session_id": "2",
                    "disposition": "malformed-review-repair",
                    "fingerprints": [],
                    "repair_session_ids": [],
                    "max_review_fix_cycles": 1,
                },
            )
            _write_state_payload(
                plan_dir,
                state_root,
                {
                    "ok": False,
                    "paused": True,
                    "reason": events.PAUSE_REASON_MALFORMED_REVIEW_REPAIR,
                    "pause_reason": (
                        events.PAUSE_REASON_MALFORMED_REVIEW_REPAIR
                    ),
                    "pause_message": "Review artifact validation failed",
                    "run_id": "run-malformed-review",
                    "run_started_at": "2026-06-19T14:00:00+00:00",
                    "run_finished_at": "2026-06-19T14:01:00+00:00",
                    "active_turn": None,
                    "turns": [],
                },
            )
            discovered = _discovered(plan_dir)

            summary = workbench.build_workbench_summary(
                discovered, state_dir=state_root
            )
            recovery = workbench.build_workbench_recovery(
                discovered, state_dir=state_root
            )

            self.assertEqual("failed", summary.primary_state)
            self.assertEqual("inspect-failure", summary.recommended_action)
            self.assertEqual("review-continuation", recovery.kind)
            self.assertEqual("artifact-invalid", summary.review_resume["blocker"])
            self.assertEqual(recovery.title, summary.next_action_hint)
            self.assertEqual(recovery.summary, summary.detail)
            self.assertEqual(recovery.review_resume, summary.review_resume)
            self.assertEqual(summary.review_resume, summary.recovery["review_resume"])
            self.assertEqual(
                events.PAUSE_REASON_MALFORMED_REVIEW_REPAIR,
                summary.pause_reason,
            )
            self.assertEqual((), summary.warnings)

            findings[-1]["repair_group"] = "g7"
            (plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
                json.dumps(
                    {"version": 2, "outcome": "findings", "findings": findings}
                ),
                encoding="utf-8",
            )

            resumed = workbench.build_workbench_summary(
                discovered, state_dir=state_root
            )
            ready_recovery = workbench.build_workbench_recovery(
                discovered, state_dir=state_root
            )

            self.assertEqual("recent", resumed.primary_state)
            self.assertEqual("run", resumed.recommended_action)
            self.assertEqual(
                workbench.REVIEW_CONTINUATION_ACTIVITY_LABEL,
                resumed.activity_label,
            )
            self.assertNotIn("sessions remain", resumed.activity_label)
            self.assertEqual("Review continuation ready", resumed.next_action_hint)
            self.assertTrue(resumed.review_resume["eligible"])
            self.assertEqual("materialize-repairs", resumed.review_resume["action"])
            self.assertEqual("review-continuation", ready_recovery.kind)
            self.assertEqual("info", ready_recovery.severity)
            self.assertEqual("review-profile", ready_recovery.actions[0].id)
            self.assertEqual(
                "review-profile", resumed.recovery["primary_action"]["id"]
            )
            self.assertEqual(ready_recovery.title, resumed.next_action_hint)
            self.assertEqual(ready_recovery.summary, resumed.detail)
            self.assertEqual(ready_recovery.review_resume, resumed.review_resume)
            self.assertEqual(
                resumed.review_resume, resumed.recovery["review_resume"]
            )

    def test_completed_review_repairs_use_zero_pending_continuation_badge(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = write_completed_review_plan(
                tmp_path / "rereview-badge",
                [_structured_review_finding()],
            )
            state_root = tmp_path / "state"
            materialized = review_repair.materialize_review_repair_sessions(
                plan_dir, state_dir=state_root
            )
            self.assertTrue(materialized.ok, materialized.error)
            child = materialized.children[0].path
            child.write_text(
                child.read_text(encoding="utf-8").replace(
                    "- **Status**: pending", "- **Status**: complete"
                ),
                encoding="utf-8",
            )

            summary = workbench.build_workbench_summary(
                _discovered(plan_dir), state_dir=state_root
            )

            self.assertEqual("recent", summary.primary_state)
            self.assertEqual("rereview", summary.review_resume["action"])
            self.assertEqual(0, summary.counts["pending"])
            self.assertEqual(
                workbench.REVIEW_CONTINUATION_ACTIVITY_LABEL,
                summary.activity_label,
            )
            self.assertNotIn("sessions remain", summary.activity_label)

    def test_completed_at_from_progress_journal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = tmp_path / "done-progress"
            write_plan(plan_dir, ("complete", "complete"))
            canonical = plans.canonicalize_path(plan_dir)
            plans.append_progress_entry(
                canonical,
                plans.ProgressEntry(
                    session_id=plans.parse_session_id("2"),
                    status="complete",
                    completed_at="2026-06-19T14:22:00Z",
                ),
            )
            discovered = _discovered(canonical)

            summary = workbench.build_workbench_summary(
                discovered, state_dir=tmp_path / "state"
            )

            self.assertEqual("complete", summary.primary_state)
            self.assertEqual("2026-06-19T14:22:00Z", summary.completed_at)
            self.assertEqual("progress-journal", summary.last_activity_source)
            self.assertEqual(
                "2026-06-19T14:22:00Z", summary.to_payload()["completed_at"]
            )

    def test_completed_at_falls_back_to_run_finished(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = tmp_path / "done-runfinished"
            write_plan(plan_dir, ("complete", "complete"))
            canonical = plans.canonicalize_path(plan_dir)
            state_root = tmp_path / "state"
            _write_state_payload(
                canonical,
                state_root,
                {
                    "ok": True,
                    "paused": False,
                    "run_id": "20260619T120000Z-9",
                    "harness": "claude",
                    "run_started_at": "2026-06-19T12:00:00+00:00",
                    "run_finished_at": "2026-06-19T12:45:00+00:00",
                    "active_turn": None,
                    "active_verification": None,
                },
            )
            discovered = _discovered(canonical)

            summary = workbench.build_workbench_summary(
                discovered, state_dir=state_root
            )

            self.assertEqual("complete", summary.primary_state)
            self.assertEqual("2026-06-19T12:45:00+00:00", summary.completed_at)
            self.assertEqual("run-finished", summary.last_activity_source)

    def test_completed_at_missing_when_no_timestamp_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = tmp_path / "done-bare"
            write_plan(plan_dir, ("complete", "complete"))
            canonical = plans.canonicalize_path(plan_dir)
            discovered = _discovered(canonical)

            summary = workbench.build_workbench_summary(
                discovered, state_dir=tmp_path / "state"
            )

            self.assertEqual("complete", summary.primary_state)
            self.assertIsNone(summary.completed_at)
            self.assertIsNone(summary.to_payload()["completed_at"])

    def test_completed_at_null_for_non_complete_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            discovered = _discovered(plan_dir)

            summary = workbench.build_workbench_summary(
                discovered, state_dir=tmp_path / "state"
            )

            self.assertEqual("ready", summary.primary_state)
            self.assertIsNone(summary.completed_at)
            payload = summary.to_payload()
            self.assertIn("completed_at", payload)
            self.assertIsNone(payload["completed_at"])

    def test_running_classification_with_active_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            state_root = tmp_path / "state"
            artifact_dir = tmp_path / "artifacts" / "turn-1"
            artifact_dir.mkdir(parents=True)
            stdout = artifact_dir / "stdout.log"
            stdout.write_text("running", encoding="utf-8")
            _write_state_payload(
                plan_dir,
                state_root,
                {
                    "ok": True,
                    "paused": False,
                    "run_id": "20260619T120000Z-1",
                    "harness": "claude",
                    "run_started_at": "2026-06-19T12:00:00+00:00",
                    "run_finished_at": None,
                    "active_turn": {
                        "session_id": "1",
                        "turn_index": 4,
                        "artifact_dir": str(artifact_dir),
                        "stdout_path": str(stdout),
                        "last_message_path": str(artifact_dir / "last-message.txt"),
                    },
                    "active_verification": None,
                },
            )
            _write_lock_file(plan_dir, state_root, pid=os.getpid())
            discovered = _discovered(plan_dir)

            summary = workbench.build_workbench_summary(
                discovered, state_dir=state_root
            )

            self.assertEqual("running", summary.primary_state)
            self.assertEqual("watch", summary.recommended_action)
            self.assertIn("session 1", summary.next_action_hint)
            self.assertEqual("20260619T120000Z-1", summary.run_id)
            self.assertEqual("claude", summary.harness)
            self.assertEqual(artifact_dir, summary.artifact_dir)
            self.assertEqual(stdout, summary.stdout_path)

    def test_verifying_classification_with_active_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            state_root = tmp_path / "state"
            _write_state_payload(
                plan_dir,
                state_root,
                {
                    "ok": True,
                    "paused": False,
                    "run_id": "20260619T130000Z-1",
                    "harness": "codex",
                    "run_started_at": "2026-06-19T13:00:00+00:00",
                    "run_finished_at": None,
                    "active_turn": None,
                    "active_verification": {
                        "session_id": "1",
                        "turn_index": 2,
                        "check_count": 2,
                        "current_check_index": 1,
                        "current_check_name": "pytest-suite",
                        "current_check_kind": "command",
                    },
                },
            )
            _write_lock_file(plan_dir, state_root, pid=os.getpid())
            discovered = _discovered(plan_dir)

            summary = workbench.build_workbench_summary(
                discovered, state_dir=state_root
            )

            self.assertEqual("verifying", summary.primary_state)
            self.assertEqual("watch", summary.recommended_action)
            self.assertIn("2/2", summary.next_action_hint)
            self.assertIn("pytest-suite", summary.next_action_hint)

    def test_verifying_takes_priority_over_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            state_root = tmp_path / "state"
            _write_state_payload(
                plan_dir,
                state_root,
                {
                    "ok": True,
                    "paused": False,
                    "run_id": "run",
                    "harness": "claude",
                    "run_finished_at": None,
                    "active_turn": {"session_id": "1", "turn_index": 1},
                    "active_verification": {
                        "session_id": "1",
                        "turn_index": 1,
                        "check_count": 1,
                        "current_check_index": 0,
                        "current_check_name": "compile",
                    },
                },
            )
            _write_lock_file(plan_dir, state_root, pid=os.getpid())

            summary = workbench.build_workbench_summary(
                _discovered(plan_dir), state_dir=state_root
            )

            self.assertEqual("verifying", summary.primary_state)

    def test_paused_for_reply_with_readable_last_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            state_root = tmp_path / "state"
            artifact_dir = state.plan_state_dir(plan_dir, state_root) / "runs"
            artifact_dir.mkdir(parents=True)
            last_message = artifact_dir / "last-message.txt"
            last_message.write_text(
                "Need a decision: should we drop the v1 column?",
                encoding="utf-8",
            )
            _write_state_payload(
                plan_dir,
                state_root,
                {
                    "ok": False,
                    "paused": True,
                    "reason": "agent-question",
                    "pause_reason": "agent-question",
                    "pause_message": "Awaiting confirmation",
                    "pause_details": {"category": "decision"},
                    "run_id": "20260619T140000Z-1",
                    "harness": "claude",
                    "turns": [
                        {
                            "session_id": "1",
                            "turn_index": 1,
                            "last_message_path": str(last_message),
                            "harness": "claude",
                            "started_at": "2026-06-19T14:00:00+00:00",
                        }
                    ],
                },
            )
            discovered = _discovered(plan_dir)

            summary = workbench.build_workbench_summary(
                discovered, state_dir=state_root
            )

            self.assertEqual("paused-for-reply", summary.primary_state)
            self.assertEqual("reply", summary.recommended_action)
            self.assertEqual(last_message, summary.last_message_path)
            self.assertIsNotNone(summary.last_message_preview)
            self.assertIn("v1 column", summary.last_message_preview)
            self.assertEqual("agent-question", summary.pause_reason)
            self.assertEqual("Awaiting confirmation", summary.pause_message)
            self.assertEqual((), summary.warnings)

    def test_paused_for_reply_without_last_message_emits_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            state_root = tmp_path / "state"
            _write_state_payload(
                plan_dir,
                state_root,
                {
                    "ok": False,
                    "paused": True,
                    "reason": "agent-question",
                    "run_id": "20260619T140000Z-1",
                    "harness": "claude",
                    "turns": [
                        {
                            "session_id": "1",
                            "turn_index": 1,
                            "harness": "claude",
                        }
                    ],
                },
            )
            discovered = _discovered(plan_dir)

            summary = workbench.build_workbench_summary(
                discovered, state_dir=state_root
            )

            self.assertEqual("paused-for-reply", summary.primary_state)
            self.assertEqual("reply", summary.recommended_action)
            self.assertIsNone(summary.last_message_preview)
            self.assertEqual(1, len(summary.warnings))
            self.assertIn("paused-q&a context unreadable", summary.warnings[0])

    def test_expired_wait_matches_qna_reply_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            state_root = tmp_path / "state"
            artifact_dir = state.plan_state_dir(plan_dir, state_root) / "runs"
            artifact_dir.mkdir(parents=True)
            last_message = artifact_dir / "last-message.txt"
            last_message.write_text(
                "Use the strict or lenient parser?\n",
                encoding="utf-8",
            )
            _write_state_payload(
                plan_dir,
                state_root,
                {
                    "ok": False,
                    "paused": False,
                    "run_id": "20260724T120000Z-1",
                    "harness": "claude",
                    "run_started_at": "2026-07-24T12:00:00+00:00",
                    "run_finished_at": None,
                    "active_turn": None,
                    "active_verification": None,
                    "turns": [
                        {
                            "session_id": "1",
                            "turn_index": 2,
                            "last_message_path": str(last_message),
                            "harness": "claude",
                        }
                    ],
                    "waiting_for_reply": {
                        "question_id": "question-2",
                        "session_id": "1",
                        "turn_index": 2,
                        "started_at": "2026-07-24T12:00:00+00:00",
                        "base_deadline": "2026-07-24T12:05:00+00:00",
                        "hard_deadline": "2026-07-24T12:15:00+00:00",
                        "last_lease_activity_at": None,
                    },
                },
            )

            qna_context = qna.load_qna_context(
                plan_dir,
                state_dir=state_root,
            )
            summary = workbench.build_workbench_summary(
                _discovered(plan_dir),
                state_dir=state_root,
            )

            self.assertEqual(qna.EXPIRED_WAIT_PAUSE_REASON, qna_context.pause_reason)
            self.assertEqual("paused-for-reply", summary.primary_state)
            self.assertEqual("reply", summary.recommended_action)
            self.assertEqual(qna_context.pause_reason, summary.pause_reason)
            self.assertEqual("reply-needed", summary.recovery["kind"])

    def test_failed_when_last_run_finished_with_ok_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            state_root = tmp_path / "state"
            _write_state_payload(
                plan_dir,
                state_root,
                {
                    "ok": False,
                    "paused": False,
                    "run_id": "20260619T120000Z-2",
                    "harness": "claude",
                    "reason": "verify-failed",
                    "run_started_at": "2026-06-19T12:00:00+00:00",
                    "run_finished_at": "2026-06-19T12:05:00+00:00",
                    "active_turn": None,
                    "active_verification": None,
                },
            )
            discovered = _discovered(plan_dir)

            summary = workbench.build_workbench_summary(
                discovered, state_dir=state_root
            )

            self.assertEqual("failed", summary.primary_state)
            self.assertEqual("inspect-failure", summary.recommended_action)
            self.assertIn("verify-failed", summary.next_action_hint)

    def test_stale_when_runner_state_unfinished_without_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            state_root = tmp_path / "state"
            _write_state_payload(
                plan_dir,
                state_root,
                {
                    "ok": True,
                    "paused": False,
                    "run_id": "20260619T120000Z-3",
                    "harness": "claude",
                    "run_started_at": "2026-06-19T12:00:00+00:00",
                    "run_finished_at": None,
                    "active_turn": None,
                    "active_verification": None,
                },
            )
            discovered = _discovered(plan_dir)

            summary = workbench.build_workbench_summary(
                discovered, state_dir=state_root
            )

            self.assertEqual("stale", summary.primary_state)
            self.assertEqual("unlock", summary.recommended_action)
            self.assertIn("no lock file is present", summary.detail or "")

    def test_stale_when_lock_is_dead(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            state_root = tmp_path / "state"
            _write_lock_file(plan_dir, state_root, pid=_WORKBENCH_DEAD_PID)
            discovered = _discovered(plan_dir)

            with mock.patch.object(
                state,
                "is_pid_running",
                side_effect=lambda pid: pid == os.getpid(),
            ):
                summary = workbench.build_workbench_summary(
                    discovered, state_dir=state_root
                )

            self.assertEqual("stale", summary.primary_state)
            self.assertEqual("unlock", summary.recommended_action)

    def test_recent_when_finished_ok_with_pending_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            state_root = tmp_path / "state"
            _write_state_payload(
                plan_dir,
                state_root,
                {
                    "ok": True,
                    "paused": False,
                    "run_id": "20260619T120000Z-4",
                    "harness": "claude",
                    "run_started_at": "2026-06-19T12:00:00+00:00",
                    "run_finished_at": "2026-06-19T12:30:00+00:00",
                    "active_turn": None,
                    "active_verification": None,
                },
            )
            discovered = _discovered(plan_dir)

            summary = workbench.build_workbench_summary(
                discovered, state_dir=state_root
            )

            self.assertEqual("recent", summary.primary_state)
            self.assertEqual("run", summary.recommended_action)
            self.assertEqual("1", summary.next_pending_session_id)
            self.assertEqual(
                workbench.ACTIVITY_LABEL_BY_STATE["recent"],
                summary.activity_label,
            )
            self.assertIn("2026-06-19T12:30:00+00:00", summary.detail or "")

    def test_preserves_registry_label_profile_and_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            state_root = tmp_path / "state"
            (plan_dir / workbench.FINAL_REVIEW_FILE_NAME).write_text(
                "# Final review\n", encoding="utf-8"
            )
            plans.append_progress_entry(
                plan_dir,
                plans.ProgressEntry(
                    session_id=plans.parse_session_id("1"),
                    status="complete",
                    completed_at="2026-06-19T14:22:00Z",
                ),
            )
            discovered = workbench.DiscoveredPlan(
                plan_dir=plan_dir,
                sources=("state", "registry"),
                label="Alpha",
                profile=workbench.LaunchProfile(harness="claude", timeout=900),
                exists=True,
            )

            summary = workbench.build_workbench_summary(
                discovered, state_dir=state_root
            )

            self.assertEqual("ready", summary.primary_state)
            self.assertEqual("Alpha", summary.label)
            self.assertIsNotNone(summary.profile)
            self.assertEqual("claude", summary.profile.harness)
            self.assertEqual(900, summary.profile.timeout)
            self.assertEqual(("state", "registry"), summary.sources)
            self.assertIsNotNone(summary.final_review_path)
            self.assertTrue(summary.progress_path.name == "progress.md")

            payload = summary.to_payload()
            self.assertEqual("ready", payload["primary_state"])
            self.assertEqual("run", payload["recommended_action"])
            self.assertEqual(["state", "registry"], payload["sources"])
            self.assertEqual("Alpha", payload["label"])
            self.assertEqual("claude", payload["profile"]["harness"])
            self.assertEqual(str(plan_dir), payload["plan_dir"])
            self.assertEqual(str(summary.progress_path), payload["progress_path"])
            self.assertEqual(
                str(plan_dir / workbench.FINAL_REVIEW_FILE_NAME),
                payload["final_review_path"],
            )

    def test_build_workbench_summaries_orders_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_a = _make_workbench_plan(tmp_path, "alpha")
            plan_b = _make_workbench_plan(tmp_path, "beta")
            discovered = [_discovered(plan_a), _discovered(plan_b)]

            summaries = workbench.build_workbench_summaries(
                discovered, state_dir=tmp_path / "state"
            )

            self.assertEqual(2, len(summaries))
            self.assertEqual(plan_a, summaries[0].plan_dir)
            self.assertEqual(plan_b, summaries[1].plan_dir)
            for summary in summaries:
                self.assertEqual("ready", summary.primary_state)

    def test_corrupt_workspace_metadata_is_isolated_in_summary_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            healthy = _make_workbench_plan(tmp_path, "healthy")
            broken = tmp_path / "broken"
            write_multi_repo_plan(broken, ("pending",))
            (broken / plans.PLAN_METADATA_FILE_NAME).write_text(
                "{not-json", encoding="utf-8"
            )

            summaries = workbench.build_workbench_summaries(
                [_discovered(healthy), _discovered(broken)],
                state_dir=tmp_path / "state",
            )

            self.assertEqual(2, len(summaries))
            self.assertEqual("ready", summaries[0].primary_state)
            self.assertEqual("invalid", summaries[1].primary_state)
            self.assertIn(
                "workspace metadata unreadable", summaries[1].detail or ""
            )
            self.assertEqual(
                "Workspace metadata cannot be inspected",
                summaries[1].recovery["title"],
            )

    def test_triage_fields_for_ready_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            discovered = _discovered(plan_dir)

            summary = workbench.build_workbench_summary(
                discovered, state_dir=tmp_path / "state"
            )

            self.assertEqual("ready", summary.primary_state)
            self.assertEqual(
                workbench.URGENCY_RANK_BY_STATE["ready"], summary.urgency_rank
            )
            self.assertEqual("ready", summary.urgency_group)
            self.assertEqual(
                workbench.ACTIVITY_LABEL_BY_STATE["ready"], summary.activity_label
            )
            self.assertIn("session 1", summary.concise_reason)
            self.assertIsNone(summary.last_activity_at)
            self.assertIsNone(summary.last_activity_source)
            self.assertIn(plan_dir.name.lower(), summary.searchable_text)
            self.assertIn("ready", summary.searchable_text)
            self.assertIn("session 1", summary.searchable_text)

    def test_triage_fields_for_invalid_missing_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            missing = tmp_path / "ghost"
            discovered = _discovered(missing, exists=False)

            summary = workbench.build_workbench_summary(
                discovered, state_dir=tmp_path / "state"
            )

            self.assertEqual("invalid", summary.primary_state)
            self.assertEqual("needs-action", summary.urgency_group)
            self.assertEqual(
                workbench.URGENCY_RANK_BY_STATE["invalid"], summary.urgency_rank
            )
            self.assertEqual(
                workbench.ACTIVITY_LABEL_BY_STATE["invalid"],
                summary.activity_label,
            )
            self.assertIn(str(missing), summary.concise_reason)
            self.assertIsNone(summary.last_activity_at)
            self.assertIsNone(summary.last_activity_source)
            self.assertIn("invalid", summary.searchable_text)
            self.assertIn("repair-plan", summary.searchable_text)

    def test_triage_fields_for_running_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            state_root = tmp_path / "state"
            _write_state_payload(
                plan_dir,
                state_root,
                {
                    "ok": True,
                    "paused": False,
                    "run_id": "20260619T120000Z-1",
                    "harness": "claude",
                    "run_started_at": "2026-06-19T12:00:00+00:00",
                    "run_finished_at": None,
                    "active_turn": {
                        "session_id": "1",
                        "turn_index": 4,
                        "started_at": "2026-06-19T12:00:01+00:00",
                        "last_activity_at": "2026-06-19T12:01:30+00:00",
                    },
                    "active_verification": None,
                },
            )
            _write_lock_file(plan_dir, state_root, pid=os.getpid())
            discovered = _discovered(plan_dir)

            summary = workbench.build_workbench_summary(
                discovered, state_dir=state_root
            )

            self.assertEqual("running", summary.primary_state)
            self.assertEqual("in-progress", summary.urgency_group)
            self.assertEqual(
                workbench.URGENCY_RANK_BY_STATE["running"], summary.urgency_rank
            )
            self.assertEqual(
                "2026-06-19T12:01:30+00:00", summary.last_activity_at
            )
            self.assertEqual("active-turn", summary.last_activity_source)
            self.assertIn("turn 4", summary.concise_reason)
            self.assertIn("claude", summary.searchable_text)
            self.assertIn("20260619t120000z-1", summary.searchable_text)

    def test_triage_fields_for_verifying_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            state_root = tmp_path / "state"
            _write_state_payload(
                plan_dir,
                state_root,
                {
                    "ok": True,
                    "paused": False,
                    "run_id": "20260619T130000Z-1",
                    "harness": "codex",
                    "run_started_at": "2026-06-19T13:00:00+00:00",
                    "run_finished_at": None,
                    "active_turn": None,
                    "active_verification": {
                        "session_id": "1",
                        "turn_index": 2,
                        "check_count": 2,
                        "current_check_index": 1,
                        "current_check_name": "pytest-suite",
                        "current_check_kind": "command",
                        "started_at": "2026-06-19T13:00:05+00:00",
                        "current_check_started_at": "2026-06-19T13:00:20+00:00",
                    },
                },
            )
            _write_lock_file(plan_dir, state_root, pid=os.getpid())
            discovered = _discovered(plan_dir)

            summary = workbench.build_workbench_summary(
                discovered, state_dir=state_root
            )

            self.assertEqual("verifying", summary.primary_state)
            self.assertEqual("in-progress", summary.urgency_group)
            self.assertEqual(
                "2026-06-19T13:00:20+00:00", summary.last_activity_at
            )
            self.assertEqual(
                "active-verification", summary.last_activity_source
            )
            self.assertIn("pytest-suite", summary.concise_reason)
            self.assertIn("2/2", summary.concise_reason)

    def test_triage_fields_for_paused_for_reply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            state_root = tmp_path / "state"
            artifact_dir = state.plan_state_dir(plan_dir, state_root) / "runs"
            artifact_dir.mkdir(parents=True)
            last_message = artifact_dir / "last-message.txt"
            last_message.write_text(
                "Need a decision: should we drop the v1 column?",
                encoding="utf-8",
            )
            _write_state_payload(
                plan_dir,
                state_root,
                {
                    "ok": False,
                    "paused": True,
                    "reason": "agent-question",
                    "pause_reason": "agent-question",
                    "pause_message": "Awaiting confirmation",
                    "pause_details": {"category": "decision"},
                    "run_id": "20260619T140000Z-1",
                    "harness": "claude",
                    "run_started_at": "2026-06-19T14:00:00+00:00",
                    "turns": [
                        {
                            "session_id": "1",
                            "turn_index": 1,
                            "last_message_path": str(last_message),
                            "harness": "claude",
                            "started_at": "2026-06-19T14:00:00+00:00",
                        }
                    ],
                },
            )
            discovered = _discovered(plan_dir)

            summary = workbench.build_workbench_summary(
                discovered, state_dir=state_root
            )

            self.assertEqual("paused-for-reply", summary.primary_state)
            self.assertEqual("needs-action", summary.urgency_group)
            self.assertEqual(
                workbench.URGENCY_RANK_BY_STATE["paused-for-reply"],
                summary.urgency_rank,
            )
            self.assertEqual(
                "2026-06-19T14:00:00+00:00", summary.last_activity_at
            )
            self.assertEqual("run-started", summary.last_activity_source)
            self.assertEqual("Awaiting confirmation", summary.concise_reason)
            self.assertIn("v1 column", summary.searchable_text)
            self.assertIn("agent-question", summary.searchable_text)

    def test_triage_fields_for_paused_without_last_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            state_root = tmp_path / "state"
            _write_state_payload(
                plan_dir,
                state_root,
                {
                    "ok": False,
                    "paused": True,
                    "reason": "agent-question",
                    "run_id": "20260619T140000Z-1",
                    "harness": "claude",
                    "run_started_at": "2026-06-19T14:00:00+00:00",
                    "turns": [
                        {
                            "session_id": "1",
                            "turn_index": 1,
                            "harness": "claude",
                        }
                    ],
                },
            )
            discovered = _discovered(plan_dir)

            summary = workbench.build_workbench_summary(
                discovered, state_dir=state_root
            )

            self.assertEqual("paused-for-reply", summary.primary_state)
            self.assertEqual(
                "2026-06-19T14:00:00+00:00", summary.last_activity_at
            )
            self.assertEqual("run-started", summary.last_activity_source)
            self.assertEqual("agent-question", summary.concise_reason)

    def test_triage_fields_for_failed_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            state_root = tmp_path / "state"
            _write_state_payload(
                plan_dir,
                state_root,
                {
                    "ok": False,
                    "paused": False,
                    "run_id": "20260619T120000Z-2",
                    "harness": "claude",
                    "reason": "verify-failed",
                    "run_started_at": "2026-06-19T12:00:00+00:00",
                    "run_finished_at": "2026-06-19T12:05:00+00:00",
                    "active_turn": None,
                    "active_verification": None,
                },
            )
            discovered = _discovered(plan_dir)

            summary = workbench.build_workbench_summary(
                discovered, state_dir=state_root
            )

            self.assertEqual("failed", summary.primary_state)
            self.assertEqual("needs-action", summary.urgency_group)
            self.assertEqual(
                "2026-06-19T12:05:00+00:00", summary.last_activity_at
            )
            self.assertEqual("run-finished", summary.last_activity_source)
            self.assertEqual("verify-failed", summary.concise_reason)
            self.assertIn("verify-failed", summary.searchable_text)

    def test_triage_fields_for_stale_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            state_root = tmp_path / "state"
            _write_lock_file(plan_dir, state_root, pid=_WORKBENCH_DEAD_PID)
            discovered = _discovered(plan_dir)

            with mock.patch.object(
                state,
                "is_pid_running",
                side_effect=lambda pid: pid == os.getpid(),
            ):
                summary = workbench.build_workbench_summary(
                    discovered, state_dir=state_root
                )

            self.assertEqual("stale", summary.primary_state)
            self.assertEqual("needs-action", summary.urgency_group)
            self.assertEqual(
                "2026-06-19T00:00:00+00:00", summary.last_activity_at
            )
            self.assertEqual("lock-created", summary.last_activity_source)

    def test_triage_fields_for_recent_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            state_root = tmp_path / "state"
            _write_state_payload(
                plan_dir,
                state_root,
                {
                    "ok": True,
                    "paused": False,
                    "run_id": "20260619T120000Z-4",
                    "harness": "claude",
                    "run_started_at": "2026-06-19T12:00:00+00:00",
                    "run_finished_at": "2026-06-19T12:30:00+00:00",
                    "active_turn": None,
                    "active_verification": None,
                },
            )
            discovered = _discovered(plan_dir)

            summary = workbench.build_workbench_summary(
                discovered, state_dir=state_root
            )

            self.assertEqual("recent", summary.primary_state)
            self.assertEqual("ready", summary.urgency_group)
            self.assertEqual(
                "2026-06-19T12:30:00+00:00", summary.last_activity_at
            )
            self.assertEqual("run-finished", summary.last_activity_source)
            self.assertIn("12:30:00", summary.concise_reason)

    def test_triage_fields_for_complete_uses_progress_journal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = tmp_path / "done-plan"
            write_plan(plan_dir, ("complete", "complete"))
            canonical = plans.canonicalize_path(plan_dir)
            plans.append_progress_entry(
                canonical,
                plans.ProgressEntry(
                    session_id=plans.parse_session_id("1"),
                    status="complete",
                    completed_at="2026-06-19T14:22:00+00:00",
                ),
            )
            plans.append_progress_entry(
                canonical,
                plans.ProgressEntry(
                    session_id=plans.parse_session_id("2"),
                    status="complete",
                    completed_at="2026-06-19T15:10:00+00:00",
                ),
            )
            discovered = _discovered(canonical)

            summary = workbench.build_workbench_summary(
                discovered, state_dir=tmp_path / "state"
            )

            self.assertEqual("complete", summary.primary_state)
            self.assertEqual("complete", summary.urgency_group)
            self.assertEqual(
                "2026-06-19T15:10:00+00:00", summary.last_activity_at
            )
            self.assertEqual("progress-journal", summary.last_activity_source)
            self.assertEqual(
                workbench.URGENCY_RANK_BY_STATE["complete"], summary.urgency_rank
            )

    def test_searchable_text_lowercases_and_includes_label(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            discovered = workbench.DiscoveredPlan(
                plan_dir=plan_dir,
                sources=("state",),
                label="Workbench Alpha",
                profile=None,
                exists=True,
            )

            summary = workbench.build_workbench_summary(
                discovered, state_dir=tmp_path / "state"
            )

            self.assertIn("workbench alpha", summary.searchable_text)
            self.assertNotIn("Workbench Alpha", summary.searchable_text)
            self.assertIn("state", summary.searchable_text)

    def test_payload_exposes_triage_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            discovered = _discovered(plan_dir)

            summary = workbench.build_workbench_summary(
                discovered, state_dir=tmp_path / "state"
            )

            payload = summary.to_payload()

            self.assertEqual(summary.urgency_rank, payload["urgency_rank"])
            self.assertEqual(summary.urgency_group, payload["urgency_group"])
            self.assertEqual(summary.activity_label, payload["activity_label"])
            self.assertEqual(summary.concise_reason, payload["concise_reason"])
            self.assertEqual(
                summary.last_activity_at, payload["last_activity_at"]
            )
            self.assertEqual(
                summary.last_activity_source, payload["last_activity_source"]
            )
            self.assertEqual(
                summary.searchable_text, payload["searchable_text"]
            )
            self.assertEqual(summary.recovery, payload["recovery"])
            self.assertEqual("none", payload["recovery"]["kind"])


if __name__ == "__main__":
    unittest.main()
