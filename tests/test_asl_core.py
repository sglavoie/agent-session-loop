"""Event persistence, plan parsing, and state schema tests.

Split from tests/test_agent_session_loop.py."""

from __future__ import annotations

import argparse
import dataclasses
import errno
import io
import json
import os
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    agent_mux_projection,
    artifacts,
    authoring,
    commit_settlement,
    contracts,
    durability,
    events,
    harnesses,
    mailbox,
    mailbox_rendering,
    pause_policy,
    plans,
    preflight,
    readiness_gate,
    responses,
    review,
    review_cycle,
    review_transitions,
    run_commit,
    run_inputs,
    run_persistence,
    run_termination,
    state,
    tail,
    turn_execution,
)
from agent_session_loop.formatting import (
    format_duration_seconds,
    format_turn_finished_message,
    format_turn_started_message,
)
from agent_session_loop.workspaces import Repository, RepositoryRegistry
from tests.support import (
    _write_lock_file,
    _write_state_payload,
    fail_open_for_path,
    read_jsonl,
    write_plan,
    write_plan_with_ids,
)


class PlanMetadataTests(unittest.TestCase):
    def test_round_trip_uses_canonical_absolute_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_dir = root / "plan"
            cwd = root / "repo"
            plan_dir.mkdir()
            cwd.mkdir()

            path = plans.save_plan_metadata(plan_dir, cwd / ".." / "repo")
            metadata = plans.load_plan_metadata(plan_dir)

            self.assertEqual(
                plans.canonicalize_path(plan_dir) / plans.PLAN_METADATA_FILE_NAME,
                path,
            )
            self.assertEqual(plans.PlanMetadata(cwd=str(cwd.resolve())), metadata)
            self.assertEqual(
                {"cwd": str(cwd.resolve()), "version": 1},
                json.loads(path.read_text(encoding="utf-8")),
            )

    def test_missing_returns_none_and_malformed_metadata_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            self.assertIsNone(plans.load_plan_metadata(plan_dir))
            path = plan_dir / plans.PLAN_METADATA_FILE_NAME
            path.write_text('{"version": 1, "cwd": "relative"}\n', encoding="utf-8")
            with self.assertRaisesRegex(plans.PlanError, "canonical absolute"):
                plans.load_plan_metadata(plan_dir)

    def test_metadata_does_not_change_plan_or_readiness_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_dir = root / "plan"
            cwd = root / "repo"
            write_plan(plan_dir, ("pending",))
            cwd.mkdir()
            content_before = plans.plan_content_sha256(plan_dir)
            readiness_before = plans.stable_readiness_sha256(plan_dir)

            plans.save_plan_metadata(plan_dir, cwd)

            self.assertEqual(content_before, plans.plan_content_sha256(plan_dir))
            self.assertEqual(readiness_before, plans.stable_readiness_sha256(plan_dir))


class EventPersistenceTests(unittest.TestCase):
    def test_durable_atomic_write_fullsyncs_file_and_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "state.json"
            with (
                mock.patch.object(artifacts.durability, "fullsync") as fullsync,
            ):
                artifacts.atomic_write_bytes(target, b"{}\n", durable=True)

            self.assertEqual(b"{}\n", target.read_bytes())
            self.assertEqual(2, fullsync.call_count)

    def test_fullsync_uses_device_barrier_on_darwin(self) -> None:
        with (
            mock.patch.object(durability.sys, "platform", "darwin"),
            mock.patch.object(durability.fcntl, "F_FULLFSYNC", 123, create=True),
            mock.patch.object(durability.fcntl, "fcntl") as full_fsync,
            mock.patch.object(durability.os, "fsync") as fsync,
        ):
            durability.fullsync(17)

        full_fsync.assert_called_once_with(17, 123)
        fsync.assert_not_called()

    def test_fullsync_falls_back_for_unsupported_darwin_filesystem(self) -> None:
        def unsupported(_fd: int, _command: int) -> None:
            raise OSError(errno.ENOTSUP, "Operation not supported")

        original_fsync = os.fsync
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / events.EVENTS_FILE_NAME
            artifact_path = Path(tmp) / "artifact.json"
            with (
                mock.patch.object(durability.sys, "platform", "darwin"),
                mock.patch.object(durability.fcntl, "F_FULLFSYNC", 123, create=True),
                mock.patch.object(durability.fcntl, "fcntl", side_effect=unsupported),
                mock.patch.object(
                    durability.os, "fsync", wraps=original_fsync
                ) as fsync,
            ):
                events.append_event(event_path, events.Event(name="probe", payload={}))
                artifacts.atomic_write_bytes(artifact_path, b"{}\n", durable=True)

            self.assertTrue(event_path.read_text(encoding="utf-8").strip())
            self.assertEqual(b"{}\n", artifact_path.read_bytes())
            self.assertEqual(3, fsync.call_count)

    def test_append_event_writes_complete_json_lines_under_concurrency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / events.EVENTS_FILE_NAME
            # A worker that outlives the test body would keep calling
            # append_event -- and therefore fullsync -- while the next two tests
            # patch it and assert exact call counts. The stop flag is what makes
            # the hang guard below able to actually reclaim the threads.
            stop = threading.Event()

            def append_many(worker: int) -> None:
                for index in range(50):
                    if stop.is_set():
                        return
                    events.append_event(
                        event_path,
                        events.Event(
                            name="threaded",
                            payload={"worker": worker, "index": index},
                        ),
                    )

            threads = [
                threading.Thread(target=append_many, args=(worker,))
                for worker in range(8)
            ]
            for thread in threads:
                thread.start()
            try:
                # Darwin uses one device-level F_FULLFSYNC per record, and all
                # 400 barriers serialize behind one lock. This is a hang guard,
                # not a latency budget: the full suite runs this shard alongside
                # a hundred others, so budget generously.
                deadline = time.monotonic() + 180.0
                for thread in threads:
                    thread.join(timeout=max(0.0, deadline - time.monotonic()))
            finally:
                stop.set()
                for thread in threads:
                    thread.join(timeout=30)
            self.assertEqual([], [t.name for t in threads if t.is_alive()])

            lines = event_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(400, len(lines))
            records = [json.loads(line) for line in lines]
            observed = {
                (record["payload"]["worker"], record["payload"]["index"])
                for record in records
            }
            expected = {(worker, index) for worker in range(8) for index in range(50)}
            self.assertEqual(expected, observed)


class ParserAndStateTests(unittest.TestCase):
    def test_format_duration_seconds_uses_seconds_minutes_and_hours(self) -> None:
        cases = [
            (None, "unknown"),
            ("not-a-number", "unknown"),
            (float("nan"), "unknown"),
            (0, "0.0s"),
            (12.34, "12.3s"),
            (59.94, "59.9s"),
            (59.95, "1m00s"),
            (60, "1m00s"),
            (65.4, "1m05s"),
            (65.5, "1m06s"),
            (5405, "1h30m05s"),
        ]

        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(expected, format_duration_seconds(value))

    def test_format_turn_started_message_marks_review_sessions(self) -> None:
        self.assertEqual(
            "review session started (7/7)",
            format_turn_started_message(
                {
                    "session_id": "7",
                    "total_sessions": 7,
                    "session_kind": plans.SESSION_KIND_REVIEW,
                }
            ),
        )
        self.assertEqual(
            "session 7/7 started",
            format_turn_started_message(
                {
                    "session_id": "7",
                    "total_sessions": 7,
                    "session_kind": plans.SESSION_KIND_IMPLEMENTATION,
                }
            ),
        )
        self.assertEqual(
            "session 7/7 started",
            format_turn_started_message({"session_id": "7", "total_sessions": 7}),
        )

    def test_format_turn_finished_message_includes_running_total_when_available(
        self,
    ) -> None:
        self.assertEqual(
            "session 2/7 finished "
            "(status complete, return code 0, duration 6m13s, total 20m15s)",
            format_turn_finished_message(
                {
                    "session_id": "2",
                    "total_sessions": 7,
                    "status_result": "complete",
                    "returncode": 0,
                    "duration_seconds": 373,
                    "total_duration_seconds": 1215,
                }
            ),
        )
        self.assertEqual(
            "session 2/7 finished (status complete, return code 0, duration 6m13s)",
            format_turn_finished_message(
                {
                    "session_id": "2",
                    "total_sessions": 7,
                    "status_result": "complete",
                    "returncode": 0,
                    "duration_seconds": 373,
                }
            ),
        )

    def test_tail_turn_started_event_marks_review_sessions(self) -> None:
        self.assertEqual(
            "review session started (7/7)",
            tail._event_message(
                "turn_started",
                {
                    "session_id": "7",
                    "total_sessions": 7,
                    "session_kind": plans.SESSION_KIND_REVIEW,
                },
            ),
        )

    def test_tail_event_messages_format_long_durations(self) -> None:
        heartbeat = tail._event_message(
            events.TURN_HEARTBEAT_EVENT,
            {
                "session": 2,
                "turn_index": 3,
                "elapsed_seconds": 5405,
                "idle_seconds": 65,
                "last_activity_at": None,
                "last_activity_stream": "stderr",
            },
        )
        self.assertIn("elapsed 1h30m05s", heartbeat)
        self.assertIn("idle 1m05s", heartbeat)

        finished = tail._event_message(
            "turn_finished",
            {
                "session": 2,
                "turn_index": 3,
                "status_result": "complete",
                "returncode": 0,
                "duration_seconds": 90,
                "total_duration_seconds": 5405,
            },
        )
        self.assertIn("duration 1m30s", finished)
        self.assertIn("total 1h30m05s", finished)

    def test_tail_event_messages_format_verification_started(self) -> None:
        message = tail._event_message(
            events.VERIFICATION_STARTED_EVENT,
            {
                "session": 2,
                "session_id": "2",
                "total_sessions": 5,
                "turn_index": 3,
                "check_count": 2,
            },
        )
        self.assertEqual(
            "verification started for session 2/5 turn 3 (2 checks)",
            message,
        )

    def test_tail_event_messages_format_verification_check_started(self) -> None:
        message = tail._event_message(
            events.VERIFICATION_CHECK_STARTED_EVENT,
            {
                "session": 2,
                "session_id": "2",
                "total_sessions": 5,
                "turn_index": 3,
                "check_index": 0,
                "check_count": 2,
                "name": "operator-surface-tests",
                "kind": "command",
            },
        )
        self.assertIn("verification check 1/2 started", message)
        self.assertIn("operator-surface-tests", message)
        self.assertIn("(command)", message)
        self.assertIn("session 2/5", message)
        self.assertIn("turn 3", message)

    def test_tail_event_messages_format_verification_heartbeat(self) -> None:
        message = tail._event_message(
            events.VERIFICATION_HEARTBEAT_EVENT,
            {
                "session": 2,
                "session_id": "2",
                "total_sessions": 5,
                "turn_index": 3,
                "check_index": 1,
                "check_count": 2,
                "name": "operator-surface-compile",
                "elapsed_seconds": 65,
                "check_elapsed_seconds": 5.0,
                "heartbeat_count": 2,
            },
        )
        self.assertIn("verification heartbeat for session 2/5", message)
        self.assertIn("check 2/2 operator-surface-compile", message)
        self.assertIn("elapsed 1m05s", message)
        self.assertIn("check elapsed 5", message)

    def test_tail_event_messages_format_verification_check_finished(self) -> None:
        passed = tail._event_message(
            events.VERIFICATION_CHECK_FINISHED_EVENT,
            {
                "session": 2,
                "session_id": "2",
                "total_sessions": 5,
                "turn_index": 3,
                "check_index": 0,
                "check_count": 2,
                "name": "operator-surface-tests",
                "kind": "command",
                "status": "pass",
                "duration_seconds": 1.5,
                "failure_reason": None,
                "returncode": 0,
            },
        )
        self.assertIn("verification check 1/2 finished", passed)
        self.assertIn("status pass", passed)
        self.assertNotIn("failure", passed)

        failed = tail._event_message(
            events.VERIFICATION_CHECK_FINISHED_EVENT,
            {
                "session": 2,
                "session_id": "2",
                "total_sessions": 5,
                "turn_index": 3,
                "check_index": 1,
                "check_count": 2,
                "name": "operator-surface-compile",
                "kind": "command",
                "status": "fail",
                "duration_seconds": 0.5,
                "failure_reason": "command-failed",
                "returncode": 1,
            },
        )
        self.assertIn("status fail", failed)
        self.assertIn("failure command-failed", failed)

    def test_tail_event_messages_format_verification_check_skipped(self) -> None:
        message = tail._event_message(
            events.VERIFICATION_CHECK_SKIPPED_EVENT,
            {
                "session": 2,
                "session_id": "2",
                "total_sessions": 5,
                "turn_index": 3,
                "check_index": 1,
                "check_count": 2,
                "name": "focused-tests",
                "kind": "command",
                "on_failure_of": ["full-suite"],
                "reason": "condition-not-met",
            },
        )
        self.assertIn("verification check 2/2 skipped", message)
        self.assertIn("focused-tests (command)", message)
        self.assertIn("condition condition-not-met", message)
        self.assertIn("on failure of full-suite", message)

    def test_tail_event_messages_format_verification_finished(self) -> None:
        passed = tail._event_message(
            events.VERIFICATION_FINISHED_EVENT,
            {
                "session": 2,
                "session_id": "2",
                "total_sessions": 5,
                "turn_index": 3,
                "check_count": 2,
                "passed_count": 2,
                "passed": True,
                "reason": "ok",
                "duration_seconds": 7.5,
            },
        )
        self.assertIn("verification passed for session 2/5", passed)
        self.assertIn("2/2 checks passed", passed)
        self.assertIn("reason ok", passed)

        failed = tail._event_message(
            events.VERIFICATION_FINISHED_EVENT,
            {
                "session": 2,
                "session_id": "2",
                "total_sessions": 5,
                "turn_index": 3,
                "check_count": 2,
                "passed_count": 1,
                "passed": False,
                "reason": "verify-failed",
                "duration_seconds": 4.0,
            },
        )
        self.assertIn("verification failed for session 2/5", failed)
        self.assertIn("1/2 checks passed", failed)
        self.assertIn("reason verify-failed", failed)

    def test_tail_does_not_add_log_targets_for_verification_events(
        self,
    ) -> None:
        record = {
            "name": events.VERIFICATION_CHECK_STARTED_EVENT,
            "payload": {
                "run_id": "run-1",
                "session": 2,
                "session_id": "2",
                "turn_index": 3,
                "check_index": 0,
                "check_count": 1,
                "name": "operator-surface-tests",
                "kind": "command",
                "argv": ["python3", "-m", "pytest", "-q"],
                "cwd": "/tmp/repo",
            },
        }
        targets = tail._discover_targets([record], None)
        self.assertEqual({}, targets)

    def test_tail_event_messages_format_run_controls(self) -> None:
        requested = tail._event_message(
            events.RUN_CONTROL_EVENT,
            {
                "action": "stop-after-current-requested",
                "message": "stop-after-current requested",
                "stop_after_current": True,
                "session": 2,
                "turn_index": 3,
                "total_sessions": 5,
            },
        )
        self.assertEqual(
            "run control: stop-after-current requested "
            "(stop-after-current on, session 2/5, turn 3)",
            requested,
        )

        canceled = tail._event_message(
            events.RUN_CONTROL_EVENT,
            {
                "action": "stop-after-current-canceled",
                "message": "stop-after-current canceled",
                "stop_after_current": False,
            },
        )
        self.assertEqual(
            "run control: stop-after-current canceled (stop-after-current off)",
            canceled,
        )

    def test_session_id_helpers_parse_sort_and_reject_invalid_ids(self) -> None:
        parsed = [
            plans.parse_session_id(value) for value in ("2.10", "2", "10", "2.1", "2.2")
        ]

        self.assertEqual(
            ["2", "2.1", "2.2", "2.10", "10"],
            [
                session_id.value
                for session_id in sorted(parsed, key=lambda item: item.sort_key)
            ],
        )
        self.assertEqual((2, 1), plans.parse_session_id("2.1").components)
        self.assertEqual("session-2.1.md", plans.session_id_to_filename("2.1"))
        self.assertEqual(
            "session-2.1.expand.md",
            plans.session_id_to_expansion_filename("2.1"),
        )
        self.assertEqual("Session 2.1", plans.session_id_display_label("2.1"))

        for value in ("0", "01", "1.0", "1.", "1..2", "1.a", "a"):
            with self.subTest(value=value):
                self.assertFalse(plans.is_valid_session_id(value))
                with self.assertRaises(ValueError):
                    plans.parse_session_id(value)

    def test_session_filename_helpers_match_dotted_files_exactly(self) -> None:
        session_id = plans.session_id_from_filename("session-2.1.md")
        expansion_id = plans.session_id_from_expansion_filename("session-2.1.expand.md")

        self.assertIsNotNone(session_id)
        self.assertIsNotNone(expansion_id)
        self.assertEqual("2.1", session_id.value)
        self.assertEqual("2.1", expansion_id.value)

        self.assertIsNone(plans.session_id_from_filename("session-2.1.expand.md"))
        self.assertIsNone(plans.session_id_from_filename("session-02.md"))
        self.assertIsNone(plans.session_id_from_expansion_filename("session-2.1.md"))
        self.assertIsNone(
            plans.session_id_from_expansion_filename("session-2.01.expand.md")
        )

    def test_inspect_plan_reports_noncanonical_leading_zero_session_files(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir(parents=True)
            (plan_dir / "context.md").write_text("# Context\n", encoding="utf-8")
            (plan_dir / "manifest.md").write_text("# Manifest\n", encoding="utf-8")
            (plan_dir / "session-01.md").write_text(
                "# Session 01\n\n## Handoff\n\n- **Status**: pending\n",
                encoding="utf-8",
            )
            (plan_dir / "session-2.01.md").write_text(
                "# Session 2.01\n\n## Handoff\n\n- **Status**: pending\n",
                encoding="utf-8",
            )
            (plan_dir / "session-01.expand.md").write_text(
                "ignored for session discovery\n", encoding="utf-8"
            )

            with self.assertRaises(plans.PlanError) as raised:
                plans.inspect_plan(plan_dir)

            message = str(raised.exception)
            self.assertIn("no canonical session-<id>.md files found", message)
            self.assertIn("leading zeroes are invalid", message)
            self.assertIn("session-01.md -> session-1.md", message)
            self.assertIn("session-2.01.md -> session-2.1.md", message)

    def test_inspect_plan_sorts_exact_session_files_and_finds_next_pending(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("complete", "pending", "complete"))
            (plan_dir / "session-10.expand.md").write_text(
                "ignored\n", encoding="utf-8"
            )
            (plan_dir / "session-10.md").write_text(
                "# Session 10\n\n## Handoff\n\n- **Status**: pending\n",
                encoding="utf-8",
            )

            snapshot = plans.inspect_plan(plan_dir)

            self.assertEqual(
                [1, 2, 3, 10], [session.number for session in snapshot.sessions]
            )
            self.assertEqual(
                ["complete", "pending", "complete", "pending"],
                [session.status for session in snapshot.sessions],
            )
            self.assertEqual(2, snapshot.next_pending.number)
            self.assertEqual(plans.path_sha256(plan_dir), snapshot.path_hash)

    def test_inspect_plan_uses_manifest_title_for_status_projection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            (plan_dir / "context.md").write_text("# Context decoy\n", encoding="utf-8")
            (plan_dir / "manifest.md").write_text(
                "# Durable plan title\n", encoding="utf-8"
            )

            snapshot = plans.inspect_plan(plan_dir)
            payload = state.status_payload(snapshot, state_dir)
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                state._print_text_status(payload)

            self.assertEqual("Durable plan title", snapshot.plan_title)
            self.assertEqual("Durable plan title", payload["plan_title"])
            self.assertEqual(
                [
                    "Plan title: Durable plan title",
                    f"Plan: {plans.canonicalize_path(plan_dir)}",
                ],
                buffer.getvalue().splitlines()[:2],
            )

    def test_inspect_plan_title_falls_back_to_directory_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "legacy-plan"
            write_plan(plan_dir, ("pending",))
            (plan_dir / "manifest.md").write_text("**Sessions**: 1\n", encoding="utf-8")

            snapshot = plans.inspect_plan(plan_dir)

            self.assertEqual("legacy-plan", snapshot.plan_title)

    def test_inspect_plan_sorts_dotted_session_files_and_finds_nested_next_pending(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan_with_ids(
                plan_dir,
                (
                    ("1", "complete"),
                    ("2.10", "pending"),
                    ("2.1", "complete"),
                    ("2", "complete"),
                    ("10", "pending"),
                    ("2.2", "pending"),
                ),
            )
            (plan_dir / "session-2.1.expand.md").write_text(
                "ignored for discovery\n", encoding="utf-8"
            )
            (plan_dir / "session-2.01.md").write_text(
                "invalid id ignored\n", encoding="utf-8"
            )

            snapshot = plans.inspect_plan(plan_dir)

            self.assertEqual(
                ["1", "2", "2.1", "2.2", "2.10", "10"],
                [session.id for session in snapshot.sessions],
            )
            self.assertEqual("2.2", snapshot.next_pending.id)
            self.assertEqual(
                ["complete", "complete", "complete", "pending", "pending", "pending"],
                [session.status for session in snapshot.sessions],
            )

    def test_inspect_plan_reads_status_from_final_handoff_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))
            (plan_dir / "session-1.md").write_text(
                "\n".join(
                    [
                        "# Session 1",
                        "",
                        "Instructions.",
                        "",
                        "**Handoff (fill on completion)**:",
                        "",
                        "- **Status**:",
                        "- **Files modified**:",
                        "",
                        "## Handoff",
                        "",
                        "- **Status**: complete",
                        "- **Files modified**: done",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            snapshot = plans.inspect_plan(plan_dir)

            self.assertEqual("complete", snapshot.sessions[0].status)
            self.assertEqual("complete", snapshot.sessions[0].raw_value)
            self.assertEqual(12, snapshot.sessions[0].line_number)

    def _session_file(self, plan_dir: Path, session_id: str = "1") -> plans.SessionFile:
        return plans.SessionFile(
            session_id=plans.parse_session_id(session_id),
            path=plan_dir / f"session-{session_id}.md",
        )

    def test_manifest_registry_parser_ignores_fenced_example_for_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("complete",))
            (plan_dir / "manifest.md").write_text(
                "# Manifest\n\n```markdown\n## Repositories\n\n"
                "| Alias | Path | Origin |\n"
                "| --- | --- | --- |\n"
                "| Example | example | https://example.com/example.git |\n"
                "```\n",
                encoding="utf-8",
            )

            registry = plans.read_repository_registry(plan_dir)
            handoff = plans.read_session_handoff(self._session_file(plan_dir))

            self.assertIsNone(registry)
            self.assertEqual("complete", handoff.status)

    def test_manifest_registry_parser_preserves_legacy_repositories_prose(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))
            (plan_dir / "manifest.md").write_text(
                "# Plan\n\n## Sessions\n\n"
                "| Session | Title | Status |\n"
                "| --- | --- | --- |\n"
                "| 1 | First | pending |\n\n"
                "## Repositories\n\n"
                "We touch SVC and OAK by hand.\n",
                encoding="utf-8",
            )

            registry = plans.read_repository_registry(plan_dir)
            snapshot = plans.inspect_plan(plan_dir)

            self.assertIsNone(registry)
            self.assertEqual(["1"], [session.id for session in snapshot.sessions])

    def test_session_scope_and_qualified_paths_are_canonical(self) -> None:
        scope = plans.parse_session_scope(
            "**Reads repositories**: SVC, OAK\n"
            "**Writes repositories**: SVC\n"
            "```markdown\n"
            "**Reads repositories**: Example\n"
            "**Writes repositories**: Example\n"
            "```\n",
            ("SVC", "OAK"),
        )

        self.assertEqual(("SVC", "OAK"), scope.reads)
        self.assertEqual(("SVC",), scope.writes)
        self.assertEqual(
            "**Reads repositories**: SVC, OAK\n**Writes repositories**: SVC\n",
            plans.render_session_scope(scope),
        )
        qualified = plans.parse_qualified_path("SVC:internal/foo.go", scope.reads)
        self.assertEqual("SVC", qualified.alias)
        self.assertEqual("internal/foo.go", qualified.path)
        self.assertEqual("SVC:internal/foo.go", plans.render_qualified_path(qualified))
        for invalid in (
            "internal/foo.go",
            "SVC:/tmp/foo",
            "SVC:../foo",
            "OAK:./foo",
        ):
            with (
                self.subTest(invalid=invalid),
                self.assertRaises(ValueError),
            ):
                plans.parse_qualified_path(invalid, ("SVC",))

    def test_completed_multi_repo_handoff_requires_qualified_write_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir()
            (plan_dir / "manifest.md").write_text(
                "# Manifest\n\n## Repositories\n\n"
                "| Alias | Path | Origin |\n"
                "| --- | --- | --- |\n"
                "| SVC | svc | https://example.com/svc.git |\n"
                "| OAK | oak | https://example.com/oak.git |\n",
                encoding="utf-8",
            )
            session_path = plan_dir / "session-1.md"
            template = (
                "# Session 1\n\n"
                "**Reads repositories**: SVC, OAK\n"
                "**Writes repositories**: SVC\n\n"
                "Do work.\n\n## Handoff\n\n"
                "- **Status**: complete\n"
                "- **Files modified**: {files}\n"
                "- **Key decisions**: none\n"
                "- **Blockers for next session**: none\n"
                "- **Notes**:\n"
            )
            session_path.write_text(
                template.format(files="internal/foo.go"), encoding="utf-8"
            )

            with self.assertRaisesRegex(plans.PlanError, "Alias:path"):
                plans.read_session_handoff(self._session_file(plan_dir))

            session_path.write_text(
                template.format(files="OAK:internal/foo.go"), encoding="utf-8"
            )
            with self.assertRaisesRegex(plans.PlanError, "outside Writes repositories"):
                plans.read_session_handoff(self._session_file(plan_dir))

            session_path.write_text(
                template.format(files="SVC:internal/foo.go, SVC:README.md"),
                encoding="utf-8",
            )
            handoff = plans.read_session_handoff(self._session_file(plan_dir))
            self.assertEqual(
                ("SVC:internal/foo.go, SVC:README.md",),
                handoff.files_modified,
            )

    def test_generated_review_artifacts_are_not_repository_handoff_paths(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir()
            (plan_dir / "context.md").write_text("# Context\n", encoding="utf-8")
            (plan_dir / "manifest.md").write_text(
                "# Manifest\n\n## Repositories\n\n"
                "| Alias | Path | Origin |\n"
                "| --- | --- | --- |\n"
                "| SVC | svc | https://example.com/svc.git |\n",
                encoding="utf-8",
            )
            session_path = plan_dir / "session-1.md"
            template = (
                "# Session 1: Final Review\n\n"
                "**Reads repositories**: SVC\n"
                "**Writes repositories**: (none)\n\n"
                "<!-- asl:generated-review-session -->\n\n"
                "## Handoff\n\n"
                "- **Status**: complete\n"
                "- **Files modified**: {files}\n"
                "- **Key decisions**: reviewed the repository\n"
                "- **Blockers for next session**: (none)\n"
                "- **Notes**:\n"
            )
            session_path.write_text(
                template.format(files="final-review.md, final-review.json"),
                encoding="utf-8",
            )

            handoff = plans.read_session_handoff(self._session_file(plan_dir))
            snapshot = plans.inspect_plan(plan_dir)

            self.assertEqual("complete", handoff.status)
            self.assertEqual(("(none)",), handoff.files_modified)
            self.assertEqual("complete", snapshot.sessions[0].status)
            self.assertIn(
                "- **Files modified**: final-review.md, final-review.json",
                session_path.read_text(encoding="utf-8"),
            )

            self.assertEqual(
                (plans.canonicalize_path(session_path),),
                plans.normalize_generated_review_handoff_artifacts(plan_dir),
            )
            self.assertIn(
                "- **Files modified**: (none)",
                session_path.read_text(encoding="utf-8"),
            )
            self.assertEqual(
                (), plans.normalize_generated_review_handoff_artifacts(plan_dir)
            )

            session_path.write_text(
                template.format(files="final-review.md, README.md"),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(plans.PlanError, "Alias:path"):
                plans.read_session_handoff(self._session_file(plan_dir))

    def test_completed_handoff_omits_files_modified_only_when_writes_are_empty(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir()
            (plan_dir / "context.md").write_text("# Context\n", encoding="utf-8")
            (plan_dir / "manifest.md").write_text(
                "# Manifest\n\n## Repositories\n\n"
                "| Alias | Path | Origin |\n"
                "| --- | --- | --- |\n"
                "| SVC | svc | https://example.com/svc.git |\n",
                encoding="utf-8",
            )
            session_path = plan_dir / "session-1.md"
            template = (
                "# Session 1\n\n"
                "**Reads repositories**: SVC\n"
                "**Writes repositories**: {writes}\n\n"
                "Do work.\n\n## Handoff\n\n"
                "- **Status**: complete\n"
                "{files}"
                "- **Key decisions**: none\n"
                "- **Blockers for next session**: none\n"
                "- **Notes**:\n"
            )
            for files in ("", "- **Files modified**:\n"):
                with self.subTest(files=files):
                    session_path.write_text(
                        template.format(writes="(none)", files=files),
                        encoding="utf-8",
                    )

                    handoff = plans.read_session_handoff(self._session_file(plan_dir))

                    self.assertEqual("complete", handoff.status)
                    self.assertEqual((), handoff.files_modified)
                    self.assertIsNone(
                        plans.completed_handoff_registry_blocker(
                            handoff,
                            scope=plans.parse_session_scope(
                                "**Reads repositories**: SVC\n"
                                "**Writes repositories**: (none)\n",
                                ("SVC",),
                            ),
                            registry_aliases=("SVC",),
                        )
                    )

                    session_path.write_text(
                        template.format(writes="SVC", files=files),
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(
                        plans.PlanError, "missing Files modified"
                    ):
                        plans.read_session_handoff(self._session_file(plan_dir))

    def test_session_scope_participates_in_readiness_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            roots = (Path(tmp) / "left", Path(tmp) / "right")
            for root, writes in zip(roots, ("SVC", "(none)"), strict=True):
                root.mkdir()
                (root / "context.md").write_text("# Context\n", encoding="utf-8")
                (root / "manifest.md").write_text(
                    "# Manifest\n\n## Repositories\n\n"
                    "| Alias | Path | Origin |\n"
                    "| --- | --- | --- |\n"
                    "| SVC | svc | https://example.com/svc.git |\n",
                    encoding="utf-8",
                )
                (root / "session-1.md").write_text(
                    "# Session 1\n\n"
                    "**Reads repositories**: SVC\n"
                    f"**Writes repositories**: {writes}\n\n"
                    "Do work.\n\n## Handoff\n\n- **Status**: pending\n",
                    encoding="utf-8",
                )

            self.assertNotEqual(
                plans.stable_readiness_sha256(roots[0]),
                plans.stable_readiness_sha256(roots[1]),
            )

    def test_read_session_handoff_round_trip_captures_all_bullets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir()
            session_path = plan_dir / "session-1.md"
            session_path.write_text(
                "\n".join(
                    [
                        "# Session 1",
                        "",
                        "Instructions.",
                        "",
                        "## Handoff",
                        "",
                        "- **Status**: complete",
                        "- **Files modified**: plans.py, runner.py",
                        "- **Key decisions**: chose option A over B",
                        "- **Blockers for next session**: none",
                        "- **Notes**: verified hash idempotency",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            handoff = plans.read_session_handoff(self._session_file(plan_dir))

            self.assertEqual("complete", handoff.status)
            self.assertEqual("complete", handoff.raw_value)
            self.assertEqual(7, handoff.line_number)
            self.assertEqual(("plans.py, runner.py",), handoff.files_modified)
            self.assertEqual("chose option A over B", handoff.key_decisions)
            self.assertEqual("none", handoff.blockers)
            self.assertEqual("verified hash idempotency", handoff.notes)
            self.assertEqual((), handoff.discovered_facts)
            self.assertEqual((), handoff.open_questions)

    def test_read_session_handoff_returns_empty_for_missing_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir()
            session_path = plan_dir / "session-1.md"
            session_path.write_text(
                "# Session 1\n\nNo handoff section here.\n",
                encoding="utf-8",
            )

            handoff = plans.read_session_handoff(self._session_file(plan_dir))

            self.assertEqual("empty", handoff.status)
            self.assertEqual("", handoff.raw_value)
            self.assertIsNone(handoff.line_number)
            self.assertEqual((), handoff.files_modified)
            self.assertIsNone(handoff.key_decisions)
            self.assertIsNone(handoff.blockers)
            self.assertIsNone(handoff.notes)
            self.assertEqual((), handoff.discovered_facts)
            self.assertEqual((), handoff.open_questions)

    def test_read_session_handoff_block_without_status_line_yields_empty_status(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir()
            session_path = plan_dir / "session-1.md"
            session_path.write_text(
                "\n".join(
                    [
                        "# Session 1",
                        "",
                        "## Handoff",
                        "",
                        "- **Files modified**: only files captured",
                        "- **Notes**: still parses",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            handoff = plans.read_session_handoff(self._session_file(plan_dir))

            self.assertEqual("empty", handoff.status)
            self.assertEqual("", handoff.raw_value)
            self.assertIsNone(handoff.line_number)
            self.assertEqual(("only files captured",), handoff.files_modified)
            self.assertEqual("still parses", handoff.notes)

    def test_read_session_handoff_uses_only_final_handoff_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir()
            session_path = plan_dir / "session-1.md"
            session_path.write_text(
                "\n".join(
                    [
                        "# Session 1",
                        "",
                        "## Handoff",
                        "",
                        "- **Status**: pending",
                        "- **Notes**: stale",
                        "",
                        "## Handoff",
                        "",
                        "- **Status**: complete",
                        "- **Notes**: real",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            handoff = plans.read_session_handoff(self._session_file(plan_dir))

            self.assertEqual("complete", handoff.status)
            self.assertEqual("real", handoff.notes)

    def test_read_session_handoff_body_fields_do_not_leak_across_subsection_heading(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir()
            session_path = plan_dir / "session-1.md"
            session_path.write_text(
                "\n".join(
                    [
                        "# Session 1",
                        "",
                        "## Handoff",
                        "",
                        "- **Status**: complete",
                        "- **Notes**:",
                        "",
                        "### Discovered Facts",
                        "- a captured fact",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            handoff = plans.read_session_handoff(self._session_file(plan_dir))

            self.assertEqual("complete", handoff.status)
            self.assertIsNone(handoff.notes)
            self.assertEqual(("a captured fact",), handoff.discovered_facts)
            self.assertEqual((), handoff.open_questions)

    def test_read_session_handoff_captures_subsections_in_canonical_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir()
            session_path = plan_dir / "session-1.md"
            session_path.write_text(
                "\n".join(
                    [
                        "# Session 1",
                        "",
                        "## Handoff",
                        "",
                        "- **Status**: complete",
                        "- **Notes**: body notes",
                        "",
                        "### Discovered Facts",
                        "- lockfile uses tar format",
                        "- retry path is foo/bar.py:142",
                        "",
                        "### Open Questions",
                        "- should we cache parser output?",
                        "- defer to perf owner",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            handoff = plans.read_session_handoff(self._session_file(plan_dir))

            self.assertEqual("complete", handoff.status)
            self.assertEqual("body notes", handoff.notes)
            self.assertEqual(
                (
                    "lockfile uses tar format",
                    "retry path is foo/bar.py:142",
                ),
                handoff.discovered_facts,
            )
            self.assertEqual(
                (
                    "should we cache parser output?",
                    "defer to perf owner",
                ),
                handoff.open_questions,
            )

    def test_read_session_handoff_accepts_reversed_subsection_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir()
            session_path = plan_dir / "session-1.md"
            session_path.write_text(
                "\n".join(
                    [
                        "# Session 1",
                        "",
                        "## Handoff",
                        "",
                        "- **Status**: complete",
                        "",
                        "### Open Questions",
                        "- question one",
                        "",
                        "### Discovered Facts",
                        "- fact one",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            handoff = plans.read_session_handoff(self._session_file(plan_dir))

            self.assertEqual(("fact one",), handoff.discovered_facts)
            self.assertEqual(("question one",), handoff.open_questions)

    def test_read_session_handoff_only_one_subsection_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir()
            session_path = plan_dir / "session-1.md"
            session_path.write_text(
                "\n".join(
                    [
                        "# Session 1",
                        "",
                        "## Handoff",
                        "",
                        "- **Status**: complete",
                        "",
                        "### Discovered Facts",
                        "- the only fact",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            handoff = plans.read_session_handoff(self._session_file(plan_dir))

            self.assertEqual(("the only fact",), handoff.discovered_facts)
            self.assertEqual((), handoff.open_questions)

    def test_read_session_handoff_subsection_without_bullets_yields_empty_tuple(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir()
            session_path = plan_dir / "session-1.md"
            session_path.write_text(
                "\n".join(
                    [
                        "# Session 1",
                        "",
                        "## Handoff",
                        "",
                        "- **Status**: complete",
                        "",
                        "### Discovered Facts",
                        "",
                        "### Open Questions",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            handoff = plans.read_session_handoff(self._session_file(plan_dir))

            self.assertEqual((), handoff.discovered_facts)
            self.assertEqual((), handoff.open_questions)

    def test_read_session_handoff_skips_nested_subsection_bullets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir()
            session_path = plan_dir / "session-1.md"
            session_path.write_text(
                "\n".join(
                    [
                        "# Session 1",
                        "",
                        "## Handoff",
                        "",
                        "- **Status**: complete",
                        "",
                        "### Discovered Facts",
                        "- top-level fact",
                        "  - nested detail ignored",
                        "    - deeper nesting also ignored",
                        "- another top-level fact",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            handoff = plans.read_session_handoff(self._session_file(plan_dir))

            self.assertEqual(
                ("top-level fact", "another top-level fact"),
                handoff.discovered_facts,
            )

    def test_read_session_handoff_ignores_unknown_subsection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir()
            session_path = plan_dir / "session-1.md"
            session_path.write_text(
                "\n".join(
                    [
                        "# Session 1",
                        "",
                        "## Handoff",
                        "",
                        "- **Status**: complete",
                        "",
                        "### Future Notes",
                        "- bullet under unknown heading",
                        "",
                        "### Discovered Facts",
                        "- a real fact",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            handoff = plans.read_session_handoff(self._session_file(plan_dir))

            self.assertEqual("complete", handoff.status)
            self.assertEqual(("a real fact",), handoff.discovered_facts)
            self.assertEqual((), handoff.open_questions)

    def test_read_session_handoff_subsections_stop_at_h2_heading(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir()
            session_path = plan_dir / "session-1.md"
            session_path.write_text(
                "\n".join(
                    [
                        "# Session 1",
                        "",
                        "## Handoff",
                        "",
                        "- **Status**: complete",
                        "",
                        "### Discovered Facts",
                        "- fact inside the block",
                        "",
                        "## Trailing Section",
                        "- bullet after the block",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            handoff = plans.read_session_handoff(self._session_file(plan_dir))

            self.assertEqual(("fact inside the block",), handoff.discovered_facts)

    def test_read_session_status_delegates_to_read_session_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir()
            session_path = plan_dir / "session-1.md"
            session_path.write_text(
                "\n".join(
                    [
                        "# Session 1",
                        "",
                        "## Handoff",
                        "",
                        "- **Status**: pending",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            session_file = self._session_file(plan_dir)

            status = plans.read_session_status(session_file)
            handoff = plans.read_session_handoff(session_file)

            self.assertEqual(handoff.status, status.status)
            self.assertEqual(handoff.raw_value, status.raw_value)
            self.assertEqual(handoff.line_number, status.line_number)
            self.assertEqual(session_file, status.session)

    def test_plan_content_hash_tracks_matching_dotted_expansion_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan_with_ids(plan_dir, (("1", "complete"), ("1.1", "pending")))
            ignored = plan_dir / "session-1.1.expand.md.bak"
            ignored.write_text("ignored\n", encoding="utf-8")

            without_expansion = plans.plan_content_sha256(plan_dir)
            expansion = plan_dir / "session-1.1.expand.md"
            expansion.write_text("first\n", encoding="utf-8")
            with_expansion = plans.plan_content_sha256(plan_dir)
            expansion.write_text("second\n", encoding="utf-8")
            changed_expansion = plans.plan_content_sha256(plan_dir)

            self.assertNotEqual(without_expansion, with_expansion)
            self.assertNotEqual(with_expansion, changed_expansion)
            ignored_hash = plans.plan_content_sha256(plan_dir)
            ignored.write_text("still ignored\n", encoding="utf-8")
            self.assertEqual(ignored_hash, plans.plan_content_sha256(plan_dir))

    def test_sync_derived_status_ledgers_updates_clear_status_tables(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("complete", "pending"))
            manifest_path = plan_dir / "manifest.md"
            context_path = plan_dir / "context.md"
            manifest_path.write_text(
                "\n".join(
                    [
                        "# Manifest",
                        "",
                        "| # | Title | Status | File |",
                        "|---|---|---|---|",
                        "| 1 | One | pending | [session-1.md](session-1.md) |",
                        "| 2 | Two | complete | session-2.md |",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            context_path.write_text(
                "\n".join(
                    [
                        "# Context",
                        "",
                        "| Session | Title | Status |",
                        "|---|---|---|",
                        "| S1 | One | pending |",
                        "| S2 | Two | complete |",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            snapshot = plans.inspect_plan(plan_dir)
            changed = plans.sync_derived_status_ledgers(snapshot)

            self.assertEqual(
                {
                    plans.canonicalize_path(manifest_path),
                    plans.canonicalize_path(context_path),
                },
                set(changed),
            )
            self.assertIn(
                "| 1 | One | complete | [session-1.md](session-1.md) |",
                manifest_path.read_text(encoding="utf-8"),
            )
            self.assertIn(
                "| 2 | Two | pending | session-2.md |",
                manifest_path.read_text(encoding="utf-8"),
            )
            self.assertIn(
                "| S1 | One | complete |",
                context_path.read_text(encoding="utf-8"),
            )
            self.assertIn(
                "| S2 | Two | pending |",
                context_path.read_text(encoding="utf-8"),
            )

    def test_sync_derived_status_ledgers_updates_dotted_session_cells(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan_with_ids(
                plan_dir,
                (("1", "complete"), ("1.1", "pending"), ("2", "pending")),
            )
            manifest_path = plan_dir / "manifest.md"
            context_path = plan_dir / "context.md"
            manifest_path.write_text(
                "\n".join(
                    [
                        "# Manifest",
                        "",
                        "| Session | Title | Status | File |",
                        "|---|---|---|---|",
                        "| S1 | One | pending | session-1.md |",
                        "| S1.1 | Nested | complete | session-1.1.md |",
                        "| 2 | Two | complete | session-2.md |",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            context_path.write_text(
                "\n".join(
                    [
                        "# Context",
                        "",
                        "| Session | Title | Status |",
                        "|---|---|---|",
                        "| `S1` | One | pending |",
                        "| `S1.1` | Nested | complete |",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            snapshot = plans.inspect_plan(plan_dir)
            changed = plans.sync_derived_status_ledgers(snapshot)

            self.assertEqual(
                {
                    plans.canonicalize_path(manifest_path),
                    plans.canonicalize_path(context_path),
                },
                set(changed),
            )
            self.assertIn(
                "| S1 | One | complete | session-1.md |",
                manifest_path.read_text(encoding="utf-8"),
            )
            self.assertIn(
                "| S1.1 | Nested | pending | session-1.1.md |",
                manifest_path.read_text(encoding="utf-8"),
            )
            self.assertIn(
                "| 2 | Two | pending | session-2.md |",
                manifest_path.read_text(encoding="utf-8"),
            )
            self.assertIn(
                "| `S1.1` | Nested | pending |",
                context_path.read_text(encoding="utf-8"),
            )

    def test_sync_derived_status_ledgers_accepts_id_headers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan_with_ids(plan_dir, (("1.1", "complete"),))
            manifest_path = plan_dir / "manifest.md"
            context_path = plan_dir / "context.md"
            manifest_path.write_text(
                "\n".join(
                    [
                        "# Manifest",
                        "",
                        "| Session ID | Status |",
                        "|---|---|",
                        "| 1.1 | pending |",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            context_path.write_text(
                "\n".join(
                    [
                        "# Context",
                        "",
                        "| ID | Status |",
                        "|---|---|",
                        "| 1.1 | pending |",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            snapshot = plans.inspect_plan(plan_dir)
            changed = plans.sync_derived_status_ledgers(snapshot)

            self.assertEqual(
                {
                    plans.canonicalize_path(manifest_path),
                    plans.canonicalize_path(context_path),
                },
                set(changed),
            )
            self.assertIn(
                "| 1.1 | complete |",
                manifest_path.read_text(encoding="utf-8"),
            )
            self.assertIn(
                "| 1.1 | complete |",
                context_path.read_text(encoding="utf-8"),
            )

    def test_sync_derived_status_ledgers_leaves_ambiguous_context_tables(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("complete",))
            context_path = plan_dir / "context.md"
            original_context = "\n".join(
                [
                    "# Context",
                    "",
                    "| Item | Status |",
                    "|---|---|",
                    "| Draft -> review | pending |",
                    "",
                ]
            )
            context_path.write_text(original_context, encoding="utf-8")

            snapshot = plans.inspect_plan(plan_dir)
            changed = plans.sync_derived_status_ledgers(snapshot)

            self.assertEqual((), changed)
            self.assertEqual(original_context, context_path.read_text(encoding="utf-8"))

    def test_unknown_and_empty_statuses_are_reported_as_invalid_pauses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))
            (plan_dir / "session-1.md").write_text(
                "# Session 1\n\n## Handoff\n\n- **Status**: maybe\n",
                encoding="utf-8",
            )
            unknown = plans.inspect_plan(plan_dir)
            self.assertEqual("unknown", unknown.sessions[0].status)
            self.assertEqual(
                "unknown-status", run_inputs._invalid_status_pause(unknown)[0]
            )

            (plan_dir / "session-1.md").write_text(
                "# Session 1\n\n## Handoff\n\n- **Files modified**: none\n",
                encoding="utf-8",
            )
            empty = plans.inspect_plan(plan_dir)
            self.assertEqual("empty", empty.sessions[0].status)
            self.assertEqual(
                "malformed-plan", run_inputs._invalid_status_pause(empty)[0]
            )

    def test_inspect_plan_wraps_plan_file_read_failures(self) -> None:
        cases = (
            "context.md",
            "manifest.md",
            "session-1.expand.md",
        )

        for relative_name in cases:
            with (
                self.subTest(relative_name=relative_name),
                tempfile.TemporaryDirectory() as tmp,
            ):
                plan_dir = Path(tmp) / "plan"
                write_plan(plan_dir, ("pending",))
                target = plan_dir / relative_name
                if not target.exists():
                    target.write_text("extra plan metadata\n", encoding="utf-8")

                with (
                    fail_open_for_path(target),
                    self.assertRaises(plans.PlanError) as raised,
                ):
                    plans.inspect_plan(plan_dir)

                message = str(raised.exception)
                self.assertIn(str(plans.canonicalize_path(target)), message)
                self.assertIn("permission denied", message.lower())
                self.assertIsInstance(raised.exception.__cause__, PermissionError)

    def test_inspect_plan_wraps_session_discovery_read_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))
            canonical_plan_dir = plans.canonicalize_path(plan_dir)
            original_iterdir = Path.iterdir

            def failing_iterdir(path: Path):
                if plans.canonicalize_path(path) == canonical_plan_dir:
                    raise PermissionError(
                        errno.EACCES, "permission denied", os.fspath(canonical_plan_dir)
                    )
                return original_iterdir(path)

            with (
                mock.patch.object(Path, "iterdir", failing_iterdir),
                self.assertRaises(plans.PlanError) as raised,
            ):
                plans.inspect_plan(plan_dir)

            message = str(raised.exception)
            self.assertIn(str(canonical_plan_dir), message)
            self.assertIn("permission denied", message.lower())
            self.assertIsInstance(raised.exception.__cause__, PermissionError)

    def test_readiness_states_cover_full_lifecycle(self) -> None:
        self.assertEqual(
            ("draft", "reviewed", "loop-ready", "running", "complete"),
            plans.READINESS_STATES,
        )
        for state_value in plans.READINESS_STATES:
            with self.subTest(state=state_value):
                self.assertEqual(state_value, plans.parse_readiness_state(state_value))
        self.assertEqual("loop-ready", plans.parse_readiness_state("  LOOP-READY  "))
        for invalid in ("", "ready", "loop_ready", "DONE"):
            with (
                self.subTest(value=invalid),
                self.assertRaises(plans.PlanError),
            ):
                plans.parse_readiness_state(invalid)

    def test_parse_readiness_metadata_reads_all_three_lines(self) -> None:
        manifest_text = "\n".join(
            [
                "# Plan",
                "",
                "**Sessions**: 2",
                "**Readiness**: reviewed",
                "**Readiness SHA-256**: `abc123`",
                "**Readiness checked at**: 2026-06-19T12:34:56Z",
                "",
                "## Sessions",
                "",
            ]
        )

        metadata = plans.parse_readiness_metadata(manifest_text)

        self.assertIsNotNone(metadata)
        self.assertEqual("reviewed", metadata.state)
        self.assertEqual("abc123", metadata.readiness_sha256)
        self.assertEqual("2026-06-19T12:34:56Z", metadata.checked_at)

    def test_parse_readiness_metadata_returns_none_when_absent(self) -> None:
        manifest_text = "# Plan\n\n**Sessions**: 1\n\n## Sessions\n"

        self.assertIsNone(plans.parse_readiness_metadata(manifest_text))

    def test_parse_readiness_metadata_rejects_partial_or_invalid_blocks(self) -> None:
        cases = (
            (
                "missing state",
                "**Readiness SHA-256**: `abc`\n**Readiness checked at**: now\n",
            ),
            (
                "missing sha",
                "**Readiness**: draft\n**Readiness checked at**: now\n",
            ),
            (
                "missing checked_at",
                "**Readiness**: draft\n**Readiness SHA-256**: `abc`\n",
            ),
            (
                "invalid state",
                "**Readiness**: green\n"
                "**Readiness SHA-256**: `abc`\n"
                "**Readiness checked at**: now\n",
            ),
            (
                "empty sha",
                "**Readiness**: draft\n"
                "**Readiness SHA-256**: ``\n"
                "**Readiness checked at**: now\n",
            ),
        )

        for label, text in cases:
            with (
                self.subTest(label=label),
                self.assertRaises(plans.PlanError),
            ):
                plans.parse_readiness_metadata(text)

    def test_parse_split_from_reads_a_backticked_pointer(self) -> None:
        manifest_text = "\n".join(
            [
                "# Plan",
                "",
                "**Split from**: `~/.claude/plans/source.md`",
                "**Sessions**: 2",
                "",
                "## Sessions",
                "",
            ]
        )

        self.assertEqual(
            "~/.claude/plans/source.md", plans.parse_split_from(manifest_text)
        )

    def test_parse_split_from_drops_prose_beside_the_pointer(self) -> None:
        cases = (
            (
                "annotated backtick span",
                "- **Split from**: `plans/source.md` (harness plan store)",
                "plans/source.md",
            ),
            (
                "bare value with trailing prose",
                "**Split from**: /srv/plans/source.md at revision abc123",
                "/srv/plans/source.md",
            ),
        )

        for label, line, expected in cases:
            with self.subTest(label=label):
                manifest_text = f"# Plan\n\n{line}\n\n## Sessions\n"
                self.assertEqual(expected, plans.parse_split_from(manifest_text))

    def test_parse_split_from_ignores_a_pointer_below_the_first_heading(self) -> None:
        manifest_text = "\n".join(
            [
                "# Plan",
                "",
                "**Sessions**: 1",
                "",
                "## Sessions",
                "",
                "**Split from**: `/quoted/in/a/narrative.md`",
                "",
            ]
        )

        self.assertIsNone(plans.parse_split_from(manifest_text))

    def test_parse_split_from_returns_none_when_absent_or_empty(self) -> None:
        cases = (
            ("absent", "# Plan\n\n**Sessions**: 1\n\n## Sessions\n"),
            ("empty value", "# Plan\n\n**Split from**:\n\n## Sessions\n"),
            ("empty backticks", "# Plan\n\n**Split from**: ``\n\n## Sessions\n"),
        )

        for label, text in cases:
            with self.subTest(label=label):
                self.assertIsNone(plans.parse_split_from(text))

    def test_read_split_from_survives_a_missing_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(plans.read_split_from(Path(tmp) / "absent"))

    def test_write_readiness_metadata_inserts_after_front_matter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))
            manifest_path = plan_dir / "manifest.md"
            manifest_path.write_text(
                "\n".join(
                    [
                        "# Plan",
                        "",
                        "**Split from**: `/source.md`",
                        "**Sessions**: 1",
                        "",
                        "## Sessions",
                        "",
                        "| # | Title | Status |",
                        "|---|---|---|",
                        "| 1 | One | pending |",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            metadata = plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256="deadbeef",
                checked_at="2026-06-19T00:00:00Z",
            )
            changed = plans.write_readiness_metadata(plan_dir, metadata)

            self.assertTrue(changed)
            text = manifest_path.read_text(encoding="utf-8")
            self.assertIn("**Readiness**: loop-ready", text)
            self.assertIn("**Readiness SHA-256**: `deadbeef`", text)
            self.assertIn("**Readiness checked at**: 2026-06-19T00:00:00Z", text)

            sessions_index = text.index("## Sessions")
            readiness_index = text.index("**Readiness**:")
            sessions_count_index = text.index("**Sessions**: 1")
            self.assertLess(sessions_count_index, readiness_index)
            self.assertLess(readiness_index, sessions_index)

            roundtrip = plans.parse_readiness_metadata(text)
            self.assertEqual(metadata, roundtrip)

            unchanged = plans.write_readiness_metadata(plan_dir, metadata)
            self.assertFalse(unchanged)

    def test_write_readiness_metadata_replaces_existing_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))
            manifest_path = plan_dir / "manifest.md"
            manifest_path.write_text(
                "\n".join(
                    [
                        "# Plan",
                        "",
                        "**Sessions**: 1",
                        "**Readiness**: draft",
                        "**Readiness SHA-256**: `oldhash`",
                        "**Readiness checked at**: 2026-01-01T00:00:00Z",
                        "",
                        "## Sessions",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            updated = plans.ReadinessMetadata(
                state="reviewed",
                readiness_sha256="newhash",
                checked_at="2026-06-19T12:00:00Z",
            )
            changed = plans.write_readiness_metadata(plan_dir, updated)

            self.assertTrue(changed)
            text = manifest_path.read_text(encoding="utf-8")
            self.assertEqual(1, text.count("**Readiness**:"))
            self.assertEqual(1, text.count("**Readiness SHA-256**:"))
            self.assertEqual(1, text.count("**Readiness checked at**:"))
            self.assertIn("**Readiness**: reviewed", text)
            self.assertIn("`newhash`", text)
            self.assertNotIn("oldhash", text)
            self.assertNotIn("2026-01-01", text)
            self.assertEqual(updated, plans.parse_readiness_metadata(text))

    def test_stable_readiness_sha256_ignores_handoff_status_mutations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending", "pending"))

            baseline = plans.stable_readiness_sha256(plan_dir)

            (plan_dir / "session-1.md").write_text(
                "\n".join(
                    [
                        "# Session 1: Test",
                        "",
                        "Instructions.",
                        "",
                        "## Handoff",
                        "",
                        "- **Status**: complete",
                        "- **Files modified**: src/foo.py",
                        "- **Key decisions**: chose option A",
                        "- **Blockers for next session**: (none)",
                        "- **Notes**: tests green",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            self.assertEqual(baseline, plans.stable_readiness_sha256(plan_dir))

    def test_stable_readiness_sha256_ignores_status_table_mutations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending", "pending"))
            manifest_path = plan_dir / "manifest.md"
            manifest_path.write_text(
                "\n".join(
                    [
                        "# Plan",
                        "",
                        "**Sessions**: 2",
                        "",
                        "## Sessions",
                        "",
                        "| # | Title | Status |",
                        "|---|---|---|",
                        "| 1 | One | pending |",
                        "| 2 | Two | pending |",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            baseline = plans.stable_readiness_sha256(plan_dir)

            manifest_path.write_text(
                "\n".join(
                    [
                        "# Plan",
                        "",
                        "**Sessions**: 2",
                        "",
                        "## Sessions",
                        "",
                        "| # | Title | Status |",
                        "|---|---|---|",
                        "| 1 | One | complete |",
                        "| 2 | Two | complete |",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            self.assertEqual(baseline, plans.stable_readiness_sha256(plan_dir))

    def test_stable_readiness_sha256_ignores_readiness_metadata_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))

            baseline = plans.stable_readiness_sha256(plan_dir)

            plans.write_readiness_metadata(
                plan_dir,
                plans.ReadinessMetadata(
                    state="loop-ready",
                    readiness_sha256=baseline,
                    checked_at="2026-06-19T00:00:00Z",
                ),
            )
            self.assertEqual(baseline, plans.stable_readiness_sha256(plan_dir))

            plans.write_readiness_metadata(
                plan_dir,
                plans.ReadinessMetadata(
                    state="reviewed",
                    readiness_sha256="differenthash",
                    checked_at="2027-01-01T00:00:00Z",
                ),
            )
            self.assertEqual(baseline, plans.stable_readiness_sha256(plan_dir))

    def test_stable_readiness_sha256_changes_when_instructions_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))

            baseline = plans.stable_readiness_sha256(plan_dir)

            session_path = plan_dir / "session-1.md"
            text = session_path.read_text(encoding="utf-8")
            session_path.write_text(
                text.replace("Instructions.", "Instructions changed."),
                encoding="utf-8",
            )

            self.assertNotEqual(baseline, plans.stable_readiness_sha256(plan_dir))

    def test_stable_readiness_sha256_tracks_expansion_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))

            baseline = plans.stable_readiness_sha256(plan_dir)

            expansion = plan_dir / "session-1.expand.md"
            expansion.write_text("notes\n", encoding="utf-8")
            with_expansion = plans.stable_readiness_sha256(plan_dir)
            self.assertNotEqual(baseline, with_expansion)

            expansion.write_text("notes changed\n", encoding="utf-8")
            self.assertNotEqual(with_expansion, plans.stable_readiness_sha256(plan_dir))

    def test_stable_readiness_sha256_ignores_parallel_validation_file(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))

            baseline = plans.stable_readiness_sha256(plan_dir)

            validation = plan_dir / "parallel-validation.md"
            validation.write_text(
                "# Parallel Validation\n\nany content at all\n",
                encoding="utf-8",
            )
            self.assertEqual(baseline, plans.stable_readiness_sha256(plan_dir))

            validation.write_text(
                "# Parallel Validation\n\nentirely different content\n",
                encoding="utf-8",
            )
            self.assertEqual(baseline, plans.stable_readiness_sha256(plan_dir))

    def test_stable_readiness_sha256_ignores_legacy_parallel_sections(
        self,
    ) -> None:
        baseline_session = "\n".join(
            [
                "# Session 1: Test",
                "",
                "## Instructions",
                "",
                "Do the thing.",
                "",
                "## Handoff",
                "",
                "- **Status**: pending",
                "",
            ]
        )
        with_parallel_session = "\n".join(
            [
                "# Session 1: Test",
                "",
                "## Parallel Metadata",
                "",
                "- **Safety classification**: serial-required",
                "",
                "## Instructions",
                "",
                "Do the thing.",
                "",
                "## Handoff",
                "",
                "- **Status**: pending",
                "",
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))
            session_path = plan_dir / "session-1.md"
            session_path.write_text(baseline_session, encoding="utf-8")
            baseline = plans.stable_readiness_sha256(plan_dir)

            manifest_path = plan_dir / "manifest.md"
            manifest_path.write_text(
                manifest_path.read_text(encoding="utf-8")
                + "\n## Parallel Execution\n\nLegacy scheduling text.\n",
                encoding="utf-8",
            )
            self.assertEqual(baseline, plans.stable_readiness_sha256(plan_dir))

            session_path.write_text(with_parallel_session, encoding="utf-8")
            self.assertEqual(baseline, plans.stable_readiness_sha256(plan_dir))

    def test_plan_content_sha256_ignores_parallel_validation_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))

            baseline = plans.plan_content_sha256(plan_dir)

            validation = plan_dir / "parallel-validation.md"
            validation.write_text("anything\n", encoding="utf-8")
            self.assertEqual(baseline, plans.plan_content_sha256(plan_dir))

            validation.write_text("anything else\n", encoding="utf-8")
            self.assertEqual(baseline, plans.plan_content_sha256(plan_dir))

    def test_plan_content_sha256_ignores_legacy_parallel_sections(self) -> None:
        baseline_session = "\n".join(
            [
                "# Session 1: Test",
                "",
                "## Instructions",
                "",
                "Do the thing.",
                "",
                "## Handoff",
                "",
                "- **Status**: pending",
                "",
            ]
        )
        with_parallel_session = "\n".join(
            [
                "# Session 1: Test",
                "",
                "## Parallel Metadata",
                "",
                "- **Safety classification**: serial-required",
                "",
                "## Instructions",
                "",
                "Do the thing.",
                "",
                "## Handoff",
                "",
                "- **Status**: pending",
                "",
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))
            session_path = plan_dir / "session-1.md"
            session_path.write_text(baseline_session, encoding="utf-8")
            baseline = plans.plan_content_sha256(plan_dir)

            manifest_path = plan_dir / "manifest.md"
            manifest_path.write_text(
                manifest_path.read_text(encoding="utf-8")
                + "\n## Parallel Execution\n\nLegacy scheduling text.\n",
                encoding="utf-8",
            )
            self.assertEqual(baseline, plans.plan_content_sha256(plan_dir))

            session_path.write_text(with_parallel_session, encoding="utf-8")
            self.assertEqual(baseline, plans.plan_content_sha256(plan_dir))

    def test_plan_content_sha256_ignores_readiness_bookkeeping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))
            baseline = plans.plan_content_sha256(plan_dir)

            for state, checked_at in (
                ("loop-ready", "2026-06-19T00:00:00Z"),
                ("running", "2026-06-19T00:00:01Z"),
                ("complete", "2026-06-19T00:00:02Z"),
            ):
                plans.write_readiness_metadata(
                    plan_dir,
                    plans.ReadinessMetadata(
                        state=state,
                        readiness_sha256=plans.stable_readiness_sha256(plan_dir),
                        checked_at=checked_at,
                    ),
                )
                self.assertEqual(baseline, plans.plan_content_sha256(plan_dir))

    def test_plan_content_sha256_still_moves_on_a_manifest_status_change(self) -> None:
        manifest = "\n".join(
            [
                "# Manifest",
                "",
                "| Session | Status |",
                "| --- | --- |",
                "| 1 | {status} |",
                "",
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))
            manifest_path = plan_dir / "manifest.md"
            manifest_path.write_text(
                manifest.format(status="pending"), encoding="utf-8"
            )
            baseline = plans.plan_content_sha256(plan_dir)
            readiness_baseline = plans.stable_readiness_sha256(plan_dir)

            manifest_path.write_text(
                manifest.format(status="complete"), encoding="utf-8"
            )
            self.assertNotEqual(baseline, plans.plan_content_sha256(plan_dir))
            self.assertEqual(
                readiness_baseline, plans.stable_readiness_sha256(plan_dir)
            )

    def test_parse_completion_contract_returns_none_when_section_absent(self) -> None:
        text = "\n".join(
            [
                "# Session 1: No contract",
                "",
                "Instructions.",
                "",
                "## Handoff",
                "",
                "- **Status**: pending",
                "",
            ]
        )

        self.assertIsNone(contracts.parse_completion_contract(text, session_id="1"))

    def test_parse_completion_contract_parses_done_when_bullets_and_recipe(
        self,
    ) -> None:
        text = "\n".join(
            [
                "# Session 2: Contract Parser",
                "",
                "Instructions.",
                "",
                "## Completion Contract",
                "",
                "### Done when",
                "- contracts.py exists",
                "- parser returns CompletionContract",
                "",
                "### Verify",
                "",
                "```json",
                "{",
                '  "session": "2",',
                '  "checks": [',
                "    {",
                '      "name": "module-exists",',
                '      "type": "file_exists",',
                '      "path": "agent_session_loop/contracts.py"',
                "    },",
                "    {",
                '      "type": "command",',
                '      "argv": ["python3", "-m", "unittest", "tests.t"],',
                '      "timeout_seconds": 30',
                "    },",
                "    {",
                '      "type": "regex_in_file",',
                '      "path": "agent_session_loop/contracts.py",',
                r'      "pattern": "^def parse_completion_contract"',
                "    }",
                "  ]",
                "}",
                "```",
                "",
                "## Handoff",
                "",
                "- **Status**: pending",
                "",
            ]
        )

        contract = contracts.parse_completion_contract(text, session_id=2)

        self.assertIsNotNone(contract)
        self.assertEqual("2", contract.session_id.value)
        self.assertEqual(
            ("contracts.py exists", "parser returns CompletionContract"),
            contract.done_when,
        )
        kinds = [check.kind for check in contract.verify.checks]
        self.assertEqual(["file_exists", "command", "regex_in_file"], kinds)
        file_check, command_check, regex_check = contract.verify.checks
        self.assertEqual("module-exists", file_check.name)
        self.assertEqual("agent_session_loop/contracts.py", file_check.path)
        self.assertEqual("check-2", command_check.name)
        self.assertEqual(("python3", "-m", "unittest", "tests.t"), command_check.argv)
        self.assertEqual(30.0, command_check.timeout_seconds)
        self.assertIsNone(command_check.cwd)
        self.assertEqual("check-3", regex_check.name)
        self.assertEqual("^def parse_completion_contract", regex_check.pattern)

    def test_repository_aware_contract_parses_every_check_kind(self) -> None:
        registry = RepositoryRegistry(
            (
                Repository("SVC", "svc", "https://example.com/svc.git"),
                Repository("OAK", "oak", "git@example.com:org/oak.git"),
            )
        )
        payload = {
            "checks": [
                {
                    "name": "command",
                    "type": "command",
                    "repo": "SVC",
                    "argv": ["python3", "-V"],
                    "cwd": "tools",
                },
                {
                    "name": "file",
                    "type": "file_exists",
                    "repo": "OAK",
                    "path": "README.md",
                },
                {
                    "name": "regex",
                    "type": "regex_in_file",
                    "repo": "SVC",
                    "path": "src/main.py",
                    "pattern": "main",
                },
            ]
        }
        text = (
            "## Completion Contract\n\n### Done when\n\n- done\n\n"
            "### Verify\n\n```json\n" + json.dumps(payload) + "\n```\n"
        )

        contract = contracts.parse_completion_contract(
            text, session_id="1", repository_registry=registry
        )

        self.assertEqual(
            ["SVC", "OAK", "SVC"],
            [check.repo for check in contract.verify.checks],
        )

    def test_repository_aware_contract_requires_known_repo_in_multi_registry(
        self,
    ) -> None:
        registry = RepositoryRegistry(
            (
                Repository("SVC", "svc", "https://example.com/svc.git"),
                Repository("OAK", "oak", "https://example.com/oak.git"),
            )
        )
        base = {
            "type": "file_exists",
            "path": "README.md",
        }
        for label, check, pattern in (
            ("missing", base, "repo.*required"),
            ("unknown", {**base, "repo": "OWL"}, "unknown repository alias"),
            ("malformed", {**base, "repo": "bad alias"}, "repo.*invalid"),
        ):
            with (
                self.subTest(label=label),
                self.assertRaisesRegex(plans.PlanError, pattern),
            ):
                contracts._parse_verify_payload(
                    {"checks": [check]},
                    session_id=plans.parse_session_id("1"),
                    repository_registry=registry,
                )

    def test_single_repository_contract_resolves_omitted_repo(self) -> None:
        registry = RepositoryRegistry(
            (Repository("SVC", "svc", "https://example.com/svc.git"),)
        )
        recipe = contracts._parse_verify_payload(
            {
                "checks": [
                    {"type": "file_exists", "path": "README.md"},
                    {"type": "command", "argv": ["true"]},
                ]
            },
            session_id=plans.parse_session_id("1"),
            repository_registry=registry,
        )

        self.assertEqual(["SVC", "SVC"], [check.repo for check in recipe.checks])

    def test_repository_aware_contract_rejects_escaping_paths_and_cwds(self) -> None:
        registry = RepositoryRegistry(
            (Repository("SVC", "svc", "https://example.com/svc.git"),)
        )
        cases = (
            ({"type": "file_exists", "path": "/tmp/x"}, "path"),
            ({"type": "regex_in_file", "path": "../x", "pattern": "x"}, "path"),
            ({"type": "command", "argv": ["true"], "cwd": "a/../../b"}, "cwd"),
            ({"type": "command", "argv": ["true"], "cwd": "C:\\tmp"}, "cwd"),
        )
        for check, field in cases:
            with (
                self.subTest(check=check),
                self.assertRaisesRegex(plans.PlanError, field + ".*beneath"),
            ):
                contracts._parse_verify_payload(
                    {"checks": [check]},
                    session_id=plans.parse_session_id("1"),
                    repository_registry=registry,
                )

    def test_legacy_contract_keeps_absolute_paths_and_omitted_repo(self) -> None:
        recipe = contracts._parse_verify_payload(
            {
                "checks": [
                    {"type": "file_exists", "path": "/tmp/legacy"},
                    {"type": "command", "argv": ["true"], "cwd": "/tmp"},
                ]
            },
            session_id=plans.parse_session_id("1"),
        )

        self.assertEqual([None, None], [check.repo for check in recipe.checks])

    def test_parse_completion_contract_requires_done_when_bullets(self) -> None:
        text = "\n".join(
            [
                "## Completion Contract",
                "",
                "### Done when",
                "",
                "### Verify",
                "",
                "```json",
                '{"checks": [{"type": "file_exists", "path": "a"}]}',
                "```",
                "",
                "## Handoff",
                "- **Status**: pending",
            ]
        )

        with self.assertRaisesRegex(plans.PlanError, "Done when"):
            contracts.parse_completion_contract(text, session_id="1")

    def test_parse_completion_contract_requires_json_fenced_block(self) -> None:
        text = "\n".join(
            [
                "## Completion Contract",
                "",
                "### Done when",
                "- something",
                "",
                "### Verify",
                "",
                "We forgot to add a fenced JSON block.",
                "",
                "## Handoff",
                "- **Status**: pending",
            ]
        )

        with self.assertRaisesRegex(plans.PlanError, "fenced code block"):
            contracts.parse_completion_contract(text, session_id="1")

    def test_parse_completion_contract_rejects_non_json_fence_language(self) -> None:
        text = "\n".join(
            [
                "## Completion Contract",
                "",
                "### Done when",
                "- something",
                "",
                "### Verify",
                "",
                "```yaml",
                "checks: []",
                "```",
                "",
                "## Handoff",
                "- **Status**: pending",
            ]
        )

        with self.assertRaisesRegex(plans.PlanError, "json"):
            contracts.parse_completion_contract(text, session_id="1")

    def test_parse_completion_contract_validates_session_id_field(self) -> None:
        text = "\n".join(
            [
                "## Completion Contract",
                "",
                "### Done when",
                "- something",
                "",
                "### Verify",
                "",
                "```json",
                "{",
                '  "session": "3",',
                '  "checks": [{"type": "file_exists", "path": "a"}]',
                "}",
                "```",
                "",
                "## Handoff",
                "- **Status**: pending",
            ]
        )

        with self.assertRaisesRegex(plans.PlanError, "expected '2'"):
            contracts.parse_completion_contract(text, session_id="2")

    def test_parse_completion_contract_requires_non_empty_checks_list(self) -> None:
        text = "\n".join(
            [
                "## Completion Contract",
                "",
                "### Done when",
                "- something",
                "",
                "### Verify",
                "",
                "```json",
                '{"checks": []}',
                "```",
                "",
                "## Handoff",
                "- **Status**: pending",
            ]
        )

        with self.assertRaisesRegex(plans.PlanError, "non-empty list"):
            contracts.parse_completion_contract(text, session_id="1")

    def test_parse_completion_contract_rejects_unknown_check_type(self) -> None:
        text = "\n".join(
            [
                "## Completion Contract",
                "",
                "### Done when",
                "- something",
                "",
                "### Verify",
                "",
                "```json",
                '{"checks": [{"type": "shell", "argv": ["echo", "hi"]}]}',
                "```",
                "",
                "## Handoff",
                "- **Status**: pending",
            ]
        )

        with self.assertRaisesRegex(plans.PlanError, "'type' must be one of"):
            contracts.parse_completion_contract(text, session_id="1")

    def test_parse_completion_contract_rejects_an_absence_gate(self) -> None:
        # An unknown key is dropped on the way to the executor, so an absence
        # flag on a presence check would run as its exact inverse.
        text = "\n".join(
            [
                "## Completion Contract",
                "",
                "### Done when",
                "- something",
                "",
                "### Verify",
                "",
                "```json",
                '{"checks": [{"type": "regex_in_file", "name": "no-todo", '
                '"path": "a.md", "pattern": "TODO", "expect_absent": true}]}',
                "```",
                "",
                "## Handoff",
                "- **Status**: pending",
            ]
        )

        with self.assertRaises(plans.PlanError) as caught:
            contracts.parse_completion_contract(text, session_id="1")

        message = str(caught.exception)
        self.assertIn("contract check #1 'no-todo'", message)
        self.assertIn("'expect_absent'", message)
        self.assertIn(
            "regex_in_file asserts the pattern is PRESENT and has no absence semantics",
            message,
        )

    def test_parse_completion_contract_rejects_claimed_gate_provenance(self) -> None:
        # ``generated_gate`` says the runner rendered the command itself, which
        # is the whole basis for promoting that command's rejection as ASL
        # schema. A contract that could set it would be a session file
        # authenticating its own text.
        text = "\n".join(
            [
                "## Completion Contract",
                "",
                "### Done when",
                "- something",
                "",
                "### Verify",
                "",
                "```json",
                '{"checks": [{"type": "command", '
                '"name": "final-review-artifact-consistent", '
                '"argv": ["python3", "-c", "pass"], "generated_gate": true}]}',
                "```",
                "",
                "## Handoff",
                "- **Status**: pending",
            ]
        )

        with self.assertRaises(plans.PlanError) as caught:
            contracts.parse_completion_contract(text, session_id="1")

        self.assertIn("'generated_gate'", str(caught.exception))

    def test_allowed_check_keys_match_what_check_payload_emits(self) -> None:
        # The allowlist is the fence a fail-closed parser stands behind, so it
        # has to be exactly the round-trip surface and nothing narrower.
        checks = (
            contracts.CommandCheck(
                name="c1",
                argv=("python3", "-V"),
                timeout_seconds=10.0,
                cwd="sub",
                baseline_expectation="fail",
                on_failure_of=("gate",),
                repo="SVC",
                review_replay=True,
            ),
            contracts.FileExistsCheck(
                name="f1",
                path="README.md",
                baseline_expectation="pass",
                on_failure_of=("gate",),
                repo="SVC",
                review_replay=True,
            ),
            contracts.RegexInFileCheck(
                name="r1",
                path="main.py",
                pattern="main",
                baseline_expectation="pass",
                on_failure_of=("gate",),
                repo="SVC",
                review_replay=True,
            ),
        )
        for check in checks:
            with self.subTest(kind=check.kind):
                self.assertEqual(
                    set(contracts.check_payload(check)),
                    set(contracts._ALLOWED_CHECK_KEYS[check.kind]),
                )

    def test_parse_completion_contract_validates_command_argv_and_timeout(
        self,
    ) -> None:
        cases = (
            (
                "empty argv",
                '{"checks": [{"type": "command", "argv": []}]}',
                "non-empty list",
            ),
            (
                "non-string argv element",
                '{"checks": [{"type": "command", "argv": ["py", 3]}]}',
                "non-empty string",
            ),
            (
                "zero timeout",
                '{"checks": [{"type": "command", "argv": ["x"], '
                '"timeout_seconds": 0}]}',
                "positive number",
            ),
            (
                "negative timeout",
                '{"checks": [{"type": "command", "argv": ["x"], '
                '"timeout_seconds": -1}]}',
                "positive number",
            ),
            (
                "timeout too large",
                '{"checks": [{"type": "command", "argv": ["x"], '
                '"timeout_seconds": 99999}]}',
                "<=",
            ),
            (
                "non-numeric timeout",
                '{"checks": [{"type": "command", "argv": ["x"], '
                '"timeout_seconds": "fast"}]}',
                "positive number",
            ),
            (
                "boolean timeout",
                '{"checks": [{"type": "command", "argv": ["x"], '
                '"timeout_seconds": true}]}',
                "positive number",
            ),
        )

        for label, json_block, pattern in cases:
            with self.subTest(label=label):
                text = "\n".join(
                    [
                        "## Completion Contract",
                        "",
                        "### Done when",
                        "- something",
                        "",
                        "### Verify",
                        "",
                        "```json",
                        json_block,
                        "```",
                        "",
                        "## Handoff",
                        "- **Status**: pending",
                    ]
                )
                with self.assertRaisesRegex(plans.PlanError, pattern):
                    contracts.parse_completion_contract(text, session_id="1")

    def test_parse_completion_contract_command_check_defaults_and_cwd(self) -> None:
        text = "\n".join(
            [
                "## Completion Contract",
                "",
                "### Done when",
                "- something",
                "",
                "### Verify",
                "",
                "```json",
                "{",
                '  "checks": [{',
                '    "type": "command",',
                '    "argv": ["python3", "-V"],',
                '    "cwd": "subdir"',
                "  }]",
                "}",
                "```",
                "",
                "## Handoff",
                "- **Status**: pending",
            ]
        )

        contract = contracts.parse_completion_contract(text, session_id="1")

        check = contract.verify.checks[0]
        self.assertEqual(
            contracts.DEFAULT_COMMAND_TIMEOUT_SECONDS, check.timeout_seconds
        )
        self.assertEqual("subdir", check.cwd)
        self.assertFalse(check.review_replay)

    def test_completion_contract_review_replay_is_explicit_and_round_trips(
        self,
    ) -> None:
        recipe = contracts._parse_verify_payload(
            {
                "checks": [
                    {
                        "type": "command",
                        "name": "durable-command",
                        "argv": ["python3", "-V"],
                        "review_replay": True,
                    },
                    {
                        "type": "file_exists",
                        "name": "session-local-file",
                        "path": "scratch.txt",
                    },
                    {
                        "type": "regex_in_file",
                        "name": "durable-regex",
                        "path": "README.md",
                        "pattern": "ready",
                        "review_replay": True,
                    },
                ]
            },
            session_id=plans.parse_session_id("1"),
        )
        contract = contracts.CompletionContract(
            session_id=plans.parse_session_id("1"),
            done_when=("done",),
            verify=recipe,
        )

        rendered = authoring.render_completion_contract(contract)
        reparsed = contracts.parse_completion_contract(rendered, session_id="1")

        self.assertEqual(
            [True, False, True],
            [check.review_replay for check in reparsed.verify.checks],
        )
        self.assertEqual(2, rendered.count('"review_replay": true'))

    def test_completion_contract_rejects_invalid_review_replay(self) -> None:
        with self.assertRaisesRegex(plans.PlanError, "review_replay.*boolean"):
            contracts._parse_verify_payload(
                {
                    "checks": [
                        {
                            "type": "file_exists",
                            "path": "README.md",
                            "review_replay": "yes",
                        }
                    ]
                },
                session_id=plans.parse_session_id("1"),
            )

    def test_completion_contract_rejects_review_replay_on_diagnostic(self) -> None:
        with self.assertRaisesRegex(plans.PlanError, "diagnostic.*on_failure_of"):
            contracts._parse_verify_payload(
                {
                    "checks": [
                        {
                            "type": "command",
                            "name": "gate",
                            "argv": ["true"],
                        },
                        {
                            "type": "command",
                            "name": "diagnostic",
                            "argv": ["false"],
                            "on_failure_of": ["gate"],
                            "review_replay": True,
                        },
                    ]
                },
                session_id=plans.parse_session_id("1"),
            )

    def test_parse_completion_contract_validates_file_exists_check(self) -> None:
        cases = (
            (
                "missing path",
                '{"checks": [{"type": "file_exists"}]}',
                "non-empty string",
            ),
            (
                "non-string path",
                '{"checks": [{"type": "file_exists", "path": 42}]}',
                "non-empty string",
            ),
            (
                "empty path",
                '{"checks": [{"type": "file_exists", "path": ""}]}',
                "non-empty string",
            ),
        )

        for label, json_block, pattern in cases:
            with self.subTest(label=label):
                text = "\n".join(
                    [
                        "## Completion Contract",
                        "",
                        "### Done when",
                        "- something",
                        "",
                        "### Verify",
                        "",
                        "```json",
                        json_block,
                        "```",
                        "",
                        "## Handoff",
                        "- **Status**: pending",
                    ]
                )
                with self.assertRaisesRegex(plans.PlanError, pattern):
                    contracts.parse_completion_contract(text, session_id="1")

    def test_parse_completion_contract_validates_regex_in_file_check(self) -> None:
        cases = (
            (
                "missing path",
                '{"checks": [{"type": "regex_in_file", "pattern": "x"}]}',
                "'path'",
            ),
            (
                "missing pattern",
                '{"checks": [{"type": "regex_in_file", "path": "a"}]}',
                "'pattern'",
            ),
            (
                "invalid regex",
                '{"checks": [{"type": "regex_in_file", "path": "a", "pattern": "("}]}',
                "not a valid regex",
            ),
        )

        for label, json_block, pattern in cases:
            with self.subTest(label=label):
                text = "\n".join(
                    [
                        "## Completion Contract",
                        "",
                        "### Done when",
                        "- something",
                        "",
                        "### Verify",
                        "",
                        "```json",
                        json_block,
                        "```",
                        "",
                        "## Handoff",
                        "- **Status**: pending",
                    ]
                )
                with self.assertRaisesRegex(plans.PlanError, pattern):
                    contracts.parse_completion_contract(text, session_id="1")

    def test_parse_completion_contract_rejects_duplicate_check_names(self) -> None:
        text = "\n".join(
            [
                "## Completion Contract",
                "",
                "### Done when",
                "- something",
                "",
                "### Verify",
                "",
                "```json",
                "{",
                '  "checks": [',
                '    {"name": "same", "type": "file_exists", "path": "a"},',
                '    {"name": "same", "type": "file_exists", "path": "b"}',
                "  ]",
                "}",
                "```",
                "",
                "## Handoff",
                "- **Status**: pending",
            ]
        )

        with self.assertRaisesRegex(plans.PlanError, "duplicated"):
            contracts.parse_completion_contract(text, session_id="1")

    def _parallel_contract_text(self, parallel_literal: str | None) -> str:
        parallel_line = (
            [] if parallel_literal is None else [f'  "parallel": {parallel_literal},']
        )
        return "\n".join(
            [
                "## Completion Contract",
                "",
                "### Done when",
                "- something",
                "",
                "### Verify",
                "",
                "```json",
                "{",
                *parallel_line,
                '  "checks": [{"type": "file_exists", "path": "a"}]',
                "}",
                "```",
                "",
                "## Handoff",
                "- **Status**: pending",
            ]
        )

    def test_parse_completion_contract_reads_parallel_flag(self) -> None:
        for literal, expected in (("true", True), ("false", False), (None, False)):
            with self.subTest(parallel=literal):
                contract = contracts.parse_completion_contract(
                    self._parallel_contract_text(literal), session_id="1"
                )
                self.assertIs(expected, contract.verify.parallel)

    def test_parse_completion_contract_rejects_non_bool_parallel(self) -> None:
        for literal in ('"true"', "1", "null", "[]"):
            with (
                self.subTest(parallel=literal),
                self.assertRaisesRegex(plans.PlanError, "'parallel' must be a boolean"),
            ):
                contracts.parse_completion_contract(
                    self._parallel_contract_text(literal), session_id="1"
                )

    def test_render_completion_contract_round_trips_parallel_flag(self) -> None:
        contract = authoring.contract_from_payload(
            "1",
            ["something"],
            {
                "parallel": True,
                "checks": [
                    {"type": "command", "argv": ["python3", "-c", "pass"]},
                    {"type": "command", "argv": ["python3", "-c", "pass"]},
                ],
            },
        )

        rendered = authoring.render_completion_contract(contract)
        reparsed = contracts.parse_completion_contract(rendered, session_id="1")

        self.assertIn('"parallel": true', rendered)
        self.assertTrue(reparsed.verify.parallel)

    def test_render_completion_contract_omits_default_parallel_flag(self) -> None:
        contract = authoring.contract_from_payload(
            "1", ["something"], {"checks": [{"type": "file_exists", "path": "a"}]}
        )

        self.assertNotIn("parallel", authoring.render_completion_contract(contract))

    def test_render_completion_contract_round_trips_failure_diagnostic(self) -> None:
        contract = authoring.contract_from_payload(
            "1",
            ["something"],
            {
                "checks": [
                    {"name": "suite", "type": "file_exists", "path": "a"},
                    {
                        "name": "focused",
                        "type": "command",
                        "argv": ["python3", "-c", "pass"],
                        "on_failure_of": ["suite"],
                    },
                ]
            },
        )

        rendered = authoring.render_completion_contract(contract)
        reparsed = contracts.parse_completion_contract(rendered, session_id="1")

        self.assertIn('"on_failure_of": [', rendered)
        self.assertEqual((), reparsed.verify.checks[0].on_failure_of)
        self.assertEqual(("suite",), reparsed.verify.checks[1].on_failure_of)

    def test_parse_completion_contract_rejects_invalid_failure_diagnostics(
        self,
    ) -> None:
        invalid_recipes = (
            (
                {
                    "checks": [
                        {"name": "suite", "type": "file_exists", "path": "a"},
                        {
                            "name": "focused",
                            "type": "file_exists",
                            "path": "b",
                            "on_failure_of": [],
                        },
                    ]
                },
                "must be a non-empty list",
            ),
            (
                {
                    "checks": [
                        {"name": "suite", "type": "file_exists", "path": "a"},
                        {
                            "name": "focused",
                            "type": "file_exists",
                            "path": "b",
                            "on_failure_of": ["missing"],
                        },
                    ]
                },
                "references unknown check",
            ),
            (
                {
                    "checks": [
                        {
                            "name": "suite",
                            "type": "file_exists",
                            "path": "a",
                            "on_failure_of": ["suite"],
                        }
                    ]
                },
                "at least one unconditional gate",
            ),
            (
                {
                    "checks": [
                        {"name": "suite", "type": "file_exists", "path": "a"},
                        {
                            "name": "diagnostic-1",
                            "type": "file_exists",
                            "path": "b",
                            "on_failure_of": ["suite"],
                        },
                        {
                            "name": "diagnostic-2",
                            "type": "file_exists",
                            "path": "c",
                            "on_failure_of": ["diagnostic-1"],
                        },
                    ]
                },
                "diagnostics may depend only on unconditional gates",
            ),
        )
        for payload, message in invalid_recipes:
            with (
                self.subTest(message=message),
                self.assertRaisesRegex(plans.PlanError, message),
            ):
                authoring.contract_from_payload("1", ["something"], payload)

    def test_parse_completion_contract_reports_malformed_json(self) -> None:
        text = "\n".join(
            [
                "## Completion Contract",
                "",
                "### Done when",
                "- something",
                "",
                "### Verify",
                "",
                "```json",
                "{not valid json}",
                "```",
                "",
                "## Handoff",
                "- **Status**: pending",
            ]
        )

        with self.assertRaisesRegex(plans.PlanError, "Verify JSON is invalid"):
            contracts.parse_completion_contract(text, session_id="1")

    def test_parse_completion_contract_ignores_handoff_section_content(self) -> None:
        text = "\n".join(
            [
                "## Completion Contract",
                "",
                "### Done when",
                "- something",
                "",
                "### Verify",
                "",
                "```json",
                '{"checks": [{"type": "file_exists", "path": "a"}]}',
                "```",
                "",
                "## Handoff",
                "",
                "- **Status**: pending",
                "- **Notes**: some prose that mentions ### Verify and ### Done when",
                "",
            ]
        )

        contract = contracts.parse_completion_contract(text, session_id="1")
        self.assertEqual(("something",), contract.done_when)
        self.assertEqual(1, len(contract.verify.checks))

    def test_read_completion_contract_loads_from_plan_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            contract_block = "\n".join(
                [
                    "## Completion Contract",
                    "",
                    "### Done when",
                    "- tests pass",
                    "",
                    "### Verify",
                    "",
                    "```json",
                    "{",
                    '  "checks": [',
                    '    {"type": "file_exists", "path": '
                    '"agent_session_loop/contracts.py"}',
                    "  ]",
                    "}",
                    "```",
                ]
            )
            write_plan(
                plan_dir,
                ("pending",),
                contracts_by_id={"1": contract_block},
            )

            contract = contracts.read_completion_contract(plan_dir, "1")

            self.assertIsNotNone(contract)
            self.assertEqual("1", contract.session_id.value)
            self.assertEqual(("tests pass",), contract.done_when)
            self.assertEqual(1, len(contract.verify.checks))

    def test_read_completion_contract_raises_for_missing_session_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))

            with self.assertRaisesRegex(plans.PlanError, "missing session file"):
                contracts.read_completion_contract(plan_dir, "9")

    def test_acquire_lock_rejects_active_lock_and_replaces_stale_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))

            lock_info = state.acquire_lock(plan_dir, state_dir)
            self.assertTrue(lock_info.active)
            with self.assertRaises(state.StateError):
                state.acquire_lock(plan_dir, state_dir)
            state.release_lock(plan_dir, state_dir)

            lock_path = state.lock_path_for_plan(plan_dir, state_dir)
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_path.write_text(
                json.dumps(
                    {"pid": 0, "plan_dir": str(plans.canonicalize_path(plan_dir))}
                ),
                encoding="utf-8",
            )
            replaced = state.acquire_lock(plan_dir, state_dir)
            self.assertTrue(replaced.active)
            state.release_lock(plan_dir, state_dir)

    def test_status_payload_includes_json_artifact_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))

            payload = state.status_payload(plans.inspect_plan(plan_dir), state_dir)

            self.assertTrue(payload["state_path"].endswith("/state.json"))
            self.assertTrue(payload["events_path"].endswith("/events.jsonl"))
            self.assertEqual(1, payload["counts"]["pending"])
            self.assertEqual("initial-review", payload["review_resume"]["action"])
            self.assertFalse(payload["review_resume"]["eligible"])
            self.assertEqual(
                "review-harness-required",
                payload["review_resume"]["blocker"],
            )

    def test_status_payload_projects_absent_mailbox_without_creating_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            plan_state = state.plan_state_dir(plan_dir, state_dir)

            payload = state.status_payload(plans.inspect_plan(plan_dir), state_dir)

            status = payload["mailbox"]
            self.assertFalse(status["present"])
            self.assertFalse(status["active"])
            self.assertFalse(status["lease_present"])
            self.assertEqual(0, status["size_bytes"])
            self.assertEqual(mailbox.MAILBOX_MAX_BYTES, status["max_bytes"])
            self.assertFalse(status["full"])
            self.assertTrue(status["accepts_questions"])
            self.assertTrue(status["accepts_replies"])
            self.assertTrue(status["accepts_resolutions"])
            self.assertEqual(0, status["last_seq"])
            self.assertEqual(0, status["pending_guidance"])
            self.assertEqual(0, status["unanswered_questions"])
            self.assertEqual(0, status["resolutions"])
            self.assertEqual(0, status["notices"])
            self.assertEqual(0, status["rejected_records"])
            self.assertIsNone(status["error"])
            self.assertIsNone(status["lease_updated_at"])
            self.assertFalse(status["lease_fresh"])
            self.assertIsNone(status["lease_reason"])
            self.assertIsNone(status["lease_error"])
            self.assertFalse(plan_state.exists())

    def test_status_payload_projects_active_mailbox_and_fresh_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            mailbox.append_guidance(plan_dir, "keep the API small", state_dir=state_dir)
            mailbox.append_question(plan_dir, "Which mode?", state_dir=state_dir)
            mailbox.write_lease(
                plan_dir,
                "00000000-0000-4000-8000-000000000001",
                state_dir=state_dir,
            )
            mailbox_path = mailbox.mailbox_path_for_plan(plan_dir, state_dir)
            lease_path = mailbox.lease_path_for_plan(plan_dir, state_dir)
            mailbox_before = mailbox_path.read_bytes()
            lease_before = lease_path.read_bytes()

            payload = state.status_payload(plans.inspect_plan(plan_dir), state_dir)

            status = payload["mailbox"]
            self.assertTrue(status["present"])
            self.assertTrue(status["active"])
            self.assertEqual(str(mailbox_path), status["path"])
            self.assertTrue(status["lease_present"])
            self.assertEqual(str(lease_path), status["lease_path"])
            self.assertGreater(status["size_bytes"], 0)
            self.assertFalse(status["full"])
            self.assertTrue(status["accepts_questions"])
            self.assertTrue(status["accepts_replies"])
            self.assertTrue(status["accepts_resolutions"])
            self.assertEqual(2, status["last_seq"])
            self.assertEqual(1, status["pending_guidance"])
            self.assertEqual(1, status["unanswered_questions"])
            self.assertEqual(0, status["resolutions"])
            self.assertEqual(0, status["notices"])
            # No live run guards this plan, so the question is history.
            self.assertEqual(0, status["open_questions"])
            self.assertEqual(0, status["rejected_records"])
            self.assertIsNone(status["error"])
            self.assertIsNotNone(status["lease_updated_at"])
            self.assertTrue(status["lease_fresh"])
            self.assertIsNone(status["lease_reason"])
            self.assertIsNone(status["lease_error"])
            self.assertEqual(mailbox_before, mailbox_path.read_bytes())
            self.assertEqual(lease_before, lease_path.read_bytes())
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                state._print_text_status(payload)
            output = buffer.getvalue()
            self.assertIn("Mailbox: active", output)
            self.assertIn("1 pending guidance", output)
            self.assertIn("0 open questions (1 unanswered historical)", output)
            self.assertIn("Mailbox lease: fresh", output)

    def test_status_projects_untouched_guidance_as_queued(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            guidance = mailbox.append_guidance(
                plan_dir, "keep the API small", state_dir=state_dir
            )

            payload = state.status_payload(plans.inspect_plan(plan_dir), state_dir)

            [entry] = payload["mailbox"]["guidance"]
            self.assertEqual(guidance.id, entry["id"])
            self.assertEqual(guidance.seq, entry["seq"])
            self.assertEqual(mailbox.GUIDANCE_STATE_QUEUED, entry["state"])
            # Nothing has ever attempted it, which is exactly what queued means.
            self.assertIsNone(entry["latest_attempt"])
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                state._print_text_status(payload)
            self.assertIn(
                f"Latest guidance: {guidance.id} (seq {guidance.seq}) "
                "queued but not delivered",
                buffer.getvalue(),
            )

    def test_status_projects_the_latest_attempt_behind_each_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            deferred_guidance = mailbox.append_guidance(
                plan_dir, "steer me", state_dir=state_dir
            )
            delivered_guidance = mailbox.append_guidance(
                plan_dir, "and me", state_dir=state_dir
            )
            mailbox.append_guidance_delivery(
                plan_dir,
                deferred_guidance.id,
                delivery_state=mailbox.GUIDANCE_STATE_DEFERRED,
                delivery_reason=mailbox.GUIDANCE_REASON_UNSUPPORTED_ACTIVE_STEER,
                channel=mailbox.GUIDANCE_CHANNEL_LIVE,
                run_id="run-1",
                session_id="5",
                turn_index=2,
                harness="copilot",
                state_dir=state_dir,
            )
            delivery = mailbox.append_guidance_delivery(
                plan_dir,
                delivered_guidance.id,
                delivery_state=mailbox.GUIDANCE_STATE_DELIVERED_LIVE,
                delivery_reason=mailbox.GUIDANCE_REASON_DELIVERED,
                channel=mailbox.GUIDANCE_CHANNEL_LIVE,
                run_id="run-1",
                session_id="5",
                turn_index=3,
                harness="claude",
                state_dir=state_dir,
            )

            payload = state.status_payload(plans.inspect_plan(plan_dir), state_dir)

            first, second = payload["mailbox"]["guidance"]
            self.assertEqual(mailbox.GUIDANCE_STATE_DEFERRED, first["state"])
            self.assertEqual(
                {
                    "delivery_state": mailbox.GUIDANCE_STATE_DEFERRED,
                    "reason": mailbox.GUIDANCE_REASON_UNSUPPORTED_ACTIVE_STEER,
                    "channel": mailbox.GUIDANCE_CHANNEL_LIVE,
                    "harness": "copilot",
                    "run_id": "run-1",
                    "session_id": "5",
                    "turn_index": 2,
                },
                {
                    key: first["latest_attempt"][key]
                    for key in (
                        "delivery_state",
                        "reason",
                        "channel",
                        "harness",
                        "run_id",
                        "session_id",
                        "turn_index",
                    )
                },
            )
            self.assertEqual(mailbox.GUIDANCE_STATE_DELIVERED_LIVE, second["state"])
            self.assertEqual(delivery.id, second["latest_attempt"]["delivery_id"])
            self.assertEqual(
                delivery.created_at, second["latest_attempt"]["recorded_at"]
            )
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                state._print_text_status(payload)
            self.assertIn("delivered live to claude", buffer.getvalue())

    def test_status_guidance_projection_never_carries_a_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            mailbox.append_guidance(
                plan_dir, "a body nobody else may read", state_dir=state_dir
            )

            payload = state.status_payload(plans.inspect_plan(plan_dir), state_dir)

            self.assertNotIn(
                "a body nobody else may read", json.dumps(payload["mailbox"])
            )

    def test_status_guidance_projection_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            appended = [
                mailbox.append_guidance(plan_dir, f"note {index}", state_dir=state_dir)
                for index in range(state.MAILBOX_STATUS_GUIDANCE_CAP + 4)
            ]

            payload = state.status_payload(plans.inspect_plan(plan_dir), state_dir)

            entries = payload["mailbox"]["guidance"]
            self.assertEqual(state.MAILBOX_STATUS_GUIDANCE_CAP, len(entries))
            # The recent end is kept: "did what I just said get through?".
            self.assertEqual(appended[-1].id, entries[-1]["id"])

    def test_status_payload_counts_open_questions_only_for_the_live_run(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            mailbox.append_question(
                plan_dir,
                "from a run that already ended",
                state_dir=state_dir,
                run_id="RUN-OLD",
            )
            mailbox.append_question(
                plan_dir,
                "predates run attribution",
                state_dir=state_dir,
            )
            mailbox.append_question(
                plan_dir,
                "the live run is blocked on this",
                state_dir=state_dir,
                run_id="RUN-LIVE",
            )
            resolved = mailbox.append_question(
                plan_dir,
                "the live run already closed this",
                state_dir=state_dir,
                run_id="RUN-LIVE",
            )
            mailbox.append_resolution(
                plan_dir,
                resolved.id,
                mailbox.RESOLUTION_ACTION_CONTINUE,
                expected_run_id="RUN-LIVE",
                state_dir=state_dir,
            )
            mailbox.append_notice(
                plan_dir,
                "checkpoint",
                run_id="RUN-LIVE",
                session_id="1",
                turn_index=1,
                state_dir=state_dir,
            )
            _write_state_payload(
                plan_dir,
                state_dir,
                {"run_id": "RUN-LIVE", "ok": True, "paused": False},
            )
            _write_lock_file(plan_dir, state_dir, pid=os.getpid())

            payload = state.status_payload(plans.inspect_plan(plan_dir), state_dir)

            status = payload["mailbox"]
            self.assertEqual(3, status["unanswered_questions"])
            self.assertEqual(1, status["open_questions"])
            self.assertEqual(1, status["resolutions"])
            self.assertEqual(1, status["notices"])
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                state._print_text_status(payload)
            output = buffer.getvalue()
            self.assertIn("1 open questions (2 unanswered historical)", output)
            self.assertIn("1 resolutions, 1 notices", output)

    def test_status_payload_projects_supervision_ends(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            mailbox.append_supervision_end(
                plan_dir,
                "reason=complete",
                outcome=mailbox.SUPERVISION_OUTCOME_COMPLETE,
                run_id="RUN-LIVE",
                state_dir=state_dir,
            )

            payload = state.status_payload(plans.inspect_plan(plan_dir), state_dir)

            status = payload["mailbox"]
            self.assertEqual(1, status["supervision_ends"])
            self.assertEqual(0, status["rejected_records"])
            self.assertIsNone(status["error"])
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                state._print_text_status(payload)
            output = buffer.getvalue()
            self.assertIn("0 notices, 1 supervision ends", output)

    def test_status_payload_retires_open_questions_once_the_run_ends(
        self,
    ) -> None:
        cases = (
            ("paused", {"run_id": "RUN-LIVE", "paused": True}),
            (
                "finished",
                {
                    "run_id": "RUN-LIVE",
                    "paused": False,
                    "run_finished_at": "2026-07-29T01:45:51+00:00",
                },
            ),
        )
        for label, runner_payload in cases:
            with self.subTest(label), tempfile.TemporaryDirectory() as tmp:
                plan_dir = Path(tmp) / "plan"
                state_dir = Path(tmp) / "state"
                write_plan(plan_dir, ("pending",))
                mailbox.append_question(
                    plan_dir,
                    "abandoned when the wait timed out",
                    state_dir=state_dir,
                    run_id="RUN-LIVE",
                )
                _write_state_payload(plan_dir, state_dir, runner_payload)
                _write_lock_file(plan_dir, state_dir, pid=os.getpid())

                payload = state.status_payload(plans.inspect_plan(plan_dir), state_dir)

                status = payload["mailbox"]
                self.assertEqual(1, status["unanswered_questions"])
                self.assertEqual(0, status["open_questions"])

    def test_live_run_id_requires_a_readable_unfinished_lock_guarded_run(
        self,
    ) -> None:
        active = state.LockInfo(
            path=Path("/tmp/lock.json"),
            pid=os.getpid(),
            plan_dir="/tmp/plan",
            created_at="2026-07-29T00:00:00+00:00",
            stale=False,
            reason="active",
            raw={},
        )
        stale = dataclasses.replace(active, stale=True, reason="stale")
        live = {"present": True, "error": None, "run_id": "RUN-LIVE"}

        self.assertEqual("RUN-LIVE", state._live_run_id(live, active))
        self.assertIsNone(state._live_run_id(live, stale))
        self.assertIsNone(state._live_run_id(live, None))
        self.assertIsNone(state._live_run_id({**live, "paused": True}, active))
        self.assertIsNone(
            state._live_run_id(
                {**live, "run_finished_at": "2026-07-29T01:00:00+00:00"},
                active,
            )
        )
        self.assertIsNone(state._live_run_id({**live, "error": "unreadable"}, active))
        self.assertIsNone(state._live_run_id({**live, "present": False}, active))
        self.assertIsNone(state._live_run_id({"present": True}, active))

    def test_min_append_bytes_matches_generated_acknowledgement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            message_id = "00000000-0000-4000-8000-000000000001"
            now = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
            created_at = "2026-07-25T12:00:00.000000+00:00"

            with (
                mock.patch.object(mailbox, "_new_id", return_value=message_id),
                mock.patch.object(mailbox, "_utc_now", return_value=now),
            ):
                appended = mailbox.append_acknowledgement(
                    plan_dir,
                    0,
                    state_dir=state_dir,
                )

            mailbox_path = mailbox.mailbox_path_for_plan(plan_dir, state_dir)
            self.assertEqual(message_id, appended.id)
            self.assertEqual(created_at, appended.created_at)
            self.assertEqual(
                mailbox.MAILBOX_MIN_APPEND_BYTES,
                len(mailbox_path.read_bytes()),
            )

    def test_min_reply_bytes_matches_generated_minimal_reply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            question_id = "00000000-0000-4000-8000-000000000001"
            reply_id = "00000000-0000-4000-8000-000000000002"
            now = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
            question = mailbox.append_question(
                plan_dir,
                "x",
                state_dir=state_dir,
                message_id=question_id,
                created_at="2026-07-25T12:00:00.000000+00:00",
            )
            mailbox_path = mailbox.mailbox_path_for_plan(plan_dir, state_dir)
            size_before = mailbox_path.stat().st_size

            with (
                mock.patch.object(mailbox, "_new_id", return_value=reply_id),
                mock.patch.object(mailbox, "_utc_now", return_value=now),
            ):
                appended = mailbox.append_reply(
                    plan_dir,
                    question.id,
                    "x",
                    state_dir=state_dir,
                )

            self.assertEqual(reply_id, appended.id)
            self.assertEqual(
                mailbox.MAILBOX_MIN_REPLY_BYTES,
                mailbox_path.stat().st_size - size_before,
            )

    def test_min_resolution_bytes_matches_generated_minimal_resolution(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            question_id = "00000000-0000-4000-8000-000000000001"
            resolution_id = "00000000-0000-4000-8000-000000000002"
            now = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
            question = mailbox.append_question(
                plan_dir,
                "x",
                state_dir=state_dir,
                message_id=question_id,
                created_at="2026-07-25T12:00:00.000000+00:00",
                run_id="x",
            )
            mailbox_path = mailbox.mailbox_path_for_plan(plan_dir, state_dir)
            size_before = mailbox_path.stat().st_size

            with (
                mock.patch.object(mailbox, "_new_id", return_value=resolution_id),
                mock.patch.object(mailbox, "_utc_now", return_value=now),
            ):
                appended = mailbox.append_resolution(
                    plan_dir,
                    question.id,
                    mailbox.RESOLUTION_ACTION_PAUSE,
                    expected_run_id="x",
                    state_dir=state_dir,
                )

            self.assertEqual(resolution_id, appended.id)
            self.assertEqual(
                mailbox.MAILBOX_MIN_RESOLUTION_BYTES,
                mailbox_path.stat().st_size - size_before,
            )

    def test_resolution_empty_note_and_omitted_note_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            empty_note_question = mailbox.append_question(
                plan_dir,
                "Continue with an empty note?",
                state_dir=state_dir,
                run_id="RUN-LIVE",
            )
            omitted_note_question = mailbox.append_question(
                plan_dir,
                "Pause without a note?",
                state_dir=state_dir,
                run_id="RUN-LIVE",
            )
            mailbox.append_resolution(
                plan_dir,
                empty_note_question.id,
                mailbox.RESOLUTION_ACTION_CONTINUE,
                expected_run_id="RUN-LIVE",
                note="",
                state_dir=state_dir,
            )
            mailbox.append_resolution(
                plan_dir,
                omitted_note_question.id,
                mailbox.RESOLUTION_ACTION_PAUSE,
                expected_run_id="RUN-LIVE",
                state_dir=state_dir,
            )

            view = mailbox.read_mailbox(plan_dir, state_dir=state_dir)
            empty_note_closure = view.closure_for(empty_note_question.id)
            empty_note_resolution = view.resolution_for(empty_note_question.id)
            omitted_note_closure = view.closure_for(omitted_note_question.id)
            omitted_note_resolution = view.resolution_for(omitted_note_question.id)

            self.assertIsNotNone(empty_note_closure)
            self.assertIsNotNone(empty_note_resolution)
            self.assertEqual("", empty_note_closure.note)
            self.assertEqual("", empty_note_resolution.note)
            self.assertIsNotNone(omitted_note_closure)
            self.assertIsNotNone(omitted_note_resolution)
            self.assertIsNone(omitted_note_closure.note)
            self.assertIsNone(omitted_note_resolution.note)

    def test_status_payload_surfaces_exhausted_and_malformed_mailboxes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            created_at = "0001-01-01T00:00:00+00:00"
            question_id = "00000000-0000-4000-8000-000000000001"
            reply_id = "00000000-0000-4000-8000-000000000002"
            question = mailbox.append_question(
                plan_dir,
                "x",
                state_dir=state_dir,
                message_id=question_id,
                created_at=created_at,
            )
            empty_text_record = {
                "created_at": created_at,
                "id": reply_id,
                "kind": mailbox.KIND_REPLY,
                "text": "",
                "to": question.id,
                "version": mailbox.MAILBOX_SCHEMA_VERSION,
            }
            record_overhead = len(
                json.dumps(
                    empty_text_record,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
                + b"\n"
            )
            target_size = (
                mailbox.MAILBOX_MAX_BYTES - mailbox.MAILBOX_MIN_APPEND_BYTES + 1
            )
            current_size = mailbox.read_mailbox(
                plan_dir, state_dir=state_dir
            ).size_bytes
            mailbox.append_reply(
                plan_dir,
                question.id,
                "x" * (target_size - current_size - record_overhead),
                state_dir=state_dir,
                message_id=reply_id,
                created_at=created_at,
            )

            exhausted_view = mailbox.read_mailbox(
                plan_dir,
                state_dir=state_dir,
            )
            self.assertEqual(target_size, exhausted_view.size_bytes)
            self.assertTrue(exhausted_view.full)
            self.assertFalse(exhausted_view.accepts_questions)
            self.assertFalse(exhausted_view.accepts_replies)
            for append in (mailbox.append_guidance, mailbox.append_question):
                with self.assertRaises(mailbox.MailboxError) as raised:
                    append(plan_dir, "x", state_dir=state_dir)
                self.assertEqual(
                    mailbox.ERROR_MAILBOX_FULL,
                    raised.exception.reason,
                )

            full_payload = state.status_payload(plans.inspect_plan(plan_dir), state_dir)

            full_status = full_payload["mailbox"]
            self.assertTrue(full_status["full"])
            self.assertFalse(full_status["accepts_questions"])
            self.assertFalse(full_status["accepts_replies"])
            self.assertFalse(full_status["accepts_resolutions"])
            self.assertEqual(target_size, full_status["size_bytes"])
            self.assertEqual(
                mailbox.MAILBOX_MAX_BYTES,
                full_status["max_bytes"],
            )

            mailbox_path = mailbox.mailbox_path_for_plan(plan_dir, state_dir)
            mailbox_path.write_text("not-json\n", encoding="utf-8")
            lease_path = mailbox.lease_path_for_plan(plan_dir, state_dir)
            lease_path.write_text("not-json\n", encoding="utf-8")

            malformed_payload = state.status_payload(
                plans.inspect_plan(plan_dir), state_dir
            )

            malformed = malformed_payload["mailbox"]
            self.assertTrue(malformed["present"])
            self.assertEqual(1, malformed["last_seq"])
            self.assertEqual(1, malformed["rejected_records"])
            self.assertIn("rejected mailbox record", malformed["error"])
            self.assertTrue(malformed["lease_present"])
            self.assertFalse(malformed["lease_fresh"])
            self.assertEqual(mailbox.LEASE_INVALID_SIZE, malformed["lease_reason"])
            self.assertEqual(mailbox.LEASE_INVALID_SIZE, malformed["lease_error"])

    def test_oversized_integer_line_is_rejected_and_append_continues(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            mailbox_path = mailbox.mailbox_path_for_plan(plan_dir, state_dir)
            mailbox_path.parent.mkdir(parents=True, exist_ok=True)
            hostile_line = '{"upto": ' + "1" * 5000 + "}\n"
            mailbox_path.write_text(hostile_line, encoding="utf-8")

            view = mailbox.read_mailbox(plan_dir, state_dir=state_dir)

            self.assertEqual(1, view.last_seq)
            self.assertEqual((), view.messages)
            self.assertEqual(1, len(view.rejected))
            self.assertEqual(mailbox.REJECT_INVALID_JSON, view.rejected[0].reason)
            question = mailbox.append_question(
                plan_dir,
                "post-hostile append",
                state_dir=state_dir,
                run_id="run-check",
            )
            updated = mailbox.read_mailbox(plan_dir, state_dir=state_dir)
            self.assertEqual(2, updated.last_seq)
            self.assertEqual((question,), updated.messages)
            self.assertEqual(1, len(updated.rejected))

    def test_oversized_turn_index_raises_mailbox_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))

            with self.assertRaises(mailbox.MailboxError) as raised:
                mailbox.append_question(
                    plan_dir,
                    "question",
                    state_dir=state_dir,
                    run_id="run-check",
                    session_id="1",
                    turn_index=10**5000,
                    waiter_origin=mailbox.WAITER_ORIGIN_WORKER,
                )

            self.assertEqual(
                mailbox.REJECT_INVALID_TURN_INDEX,
                raised.exception.reason,
            )
            self.assertFalse(
                mailbox.mailbox_path_for_plan(plan_dir, state_dir).exists()
            )

    def test_oversized_acknowledgement_raises_mailbox_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))

            with self.assertRaises(mailbox.MailboxError) as raised:
                mailbox.append_acknowledgement(
                    plan_dir,
                    10**5000,
                    state_dir=state_dir,
                )

            self.assertEqual(
                mailbox.REJECT_INVALID_ACKNOWLEDGEMENT,
                raised.exception.reason,
            )
            self.assertFalse(
                mailbox.mailbox_path_for_plan(plan_dir, state_dir).exists()
            )

    def test_supervision_end_round_trips_with_and_without_a_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))

            unattributed = mailbox.append_supervision_end(
                plan_dir,
                "reason=complete",
                outcome=mailbox.SUPERVISION_OUTCOME_COMPLETE,
                state_dir=state_dir,
            )
            attributed = mailbox.append_supervision_end(
                plan_dir,
                "reason=worker_hung",
                outcome=mailbox.SUPERVISION_OUTCOME_BLOCKED,
                run_id="run-A",
                state_dir=state_dir,
            )

            view = mailbox.read_mailbox(plan_dir, state_dir=state_dir)
            self.assertEqual((), view.rejected)
            self.assertEqual((unattributed, attributed), view.messages)
            self.assertIsNone(unattributed.run_id)
            self.assertEqual("run-A", attributed.run_id)
            self.assertEqual(
                mailbox.SUPERVISION_OUTCOME_COMPLETE,
                unattributed.supervision_outcome,
            )
            self.assertEqual(attributed, view.latest_supervision_end)
            self.assertEqual(
                attributed,
                view.messages_since(
                    unattributed.seq,
                    kinds=(mailbox.KIND_SUPERVISION_END,),
                )[0],
            )
            payload = unattributed.to_payload()
            self.assertEqual(mailbox.SUPERVISION_OUTCOME_COMPLETE, payload["outcome"])
            self.assertEqual("reason=complete", payload["text"])
            self.assertNotIn("run_id", payload)
            self.assertNotIn("supervision_id", payload)
            self.assertEqual(mailbox.MAILBOX_SCHEMA_VERSION, payload["version"])

    def test_supervision_end_round_trips_a_launch_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            launch = mailbox.new_supervision_id()

            appended = mailbox.append_supervision_end(
                plan_dir,
                "reason=complete",
                outcome=mailbox.SUPERVISION_OUTCOME_COMPLETE,
                supervision_id=launch,
                state_dir=state_dir,
            )

            view = mailbox.read_mailbox(plan_dir, state_dir=state_dir)
            self.assertEqual((), view.rejected)
            self.assertEqual((appended,), view.messages)
            self.assertEqual(launch, view.messages[0].supervision_id)
            self.assertEqual(launch, appended.to_payload()["supervision_id"])
            self.assertEqual(
                launch,
                json.loads(mailbox_rendering.message_json(appended))["supervision_id"],
            )

    def test_supervision_end_appends_reject_malformed_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            cases = (
                (
                    mailbox.REJECT_INVALID_SUPERVISION_OUTCOME,
                    {"text": "done", "outcome": "finished"},
                ),
                (
                    mailbox.REJECT_INVALID_SUPERVISION_OUTCOME,
                    {"text": "done", "outcome": None},
                ),
                (
                    mailbox.REJECT_MISSING_TEXT,
                    {"text": "   ", "outcome": "complete"},
                ),
                (
                    mailbox.REJECT_MISSING_TEXT,
                    {"text": None, "outcome": "complete"},
                ),
                (
                    mailbox.REJECT_INVALID_RUN_ID,
                    {"text": "done", "outcome": "complete", "run_id": " "},
                ),
                (
                    mailbox.REJECT_INVALID_RUN_ID,
                    {
                        "text": "done",
                        "outcome": "complete",
                        "run_id": "r" * (mailbox.MAILBOX_MAX_RUN_ID_CHARS + 1),
                    },
                ),
                (
                    mailbox.REJECT_INVALID_SUPERVISION_ID,
                    {
                        "text": "done",
                        "outcome": "complete",
                        "supervision_id": "helper-1",
                    },
                ),
            )

            for reason, kwargs in cases:
                with self.subTest(reason=reason, kwargs=kwargs):
                    with self.assertRaises(mailbox.MailboxError) as raised:
                        mailbox.append_supervision_end(
                            plan_dir,
                            kwargs["text"],
                            outcome=kwargs["outcome"],
                            run_id=kwargs.get("run_id"),
                            supervision_id=kwargs.get("supervision_id"),
                            state_dir=state_dir,
                        )
                    self.assertEqual(reason, raised.exception.reason)

            self.assertFalse(
                mailbox.mailbox_path_for_plan(plan_dir, state_dir).exists()
            )

    def test_persisted_supervision_ends_reject_malformed_and_old_versions(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            mailbox_path = mailbox.mailbox_path_for_plan(plan_dir, state_dir)
            mailbox_path.parent.mkdir(parents=True, exist_ok=True)
            created_at = "0001-01-01T00:00:00+00:00"
            expected: list[str] = []
            lines: list[str] = []
            cases = (
                (
                    mailbox.REJECT_UNKNOWN_KIND,
                    {"version": mailbox.MAILBOX_LEGACY_SCHEMA_VERSION},
                ),
                (
                    mailbox.REJECT_UNKNOWN_KIND,
                    {"version": mailbox.MAILBOX_PREVIOUS_SCHEMA_VERSION},
                ),
                (
                    mailbox.REJECT_INVALID_SUPERVISION_OUTCOME,
                    {"outcome": "finished"},
                ),
                (mailbox.REJECT_INVALID_SUPERVISION_OUTCOME, {"outcome": None}),
                (mailbox.REJECT_MISSING_TEXT, {"text": "  "}),
                (mailbox.REJECT_MISSING_TEXT, {"text": None}),
                (mailbox.REJECT_INVALID_RUN_ID, {"run_id": ""}),
                (mailbox.REJECT_INVALID_RUN_ID, {"run_id": 7}),
                (
                    mailbox.REJECT_INVALID_SUPERVISION_ID,
                    {"supervision_id": "helper-1"},
                ),
                (mailbox.REJECT_INVALID_SUPERVISION_ID, {"supervision_id": 7}),
            )
            for index, (reason, overrides) in enumerate(cases):
                record = {
                    "created_at": created_at,
                    "id": f"00000000-0000-4000-8000-{index:012d}",
                    "kind": mailbox.KIND_SUPERVISION_END,
                    "outcome": mailbox.SUPERVISION_OUTCOME_COMPLETE,
                    "text": "supervision over",
                    "version": mailbox.MAILBOX_SCHEMA_VERSION,
                }
                record.update(overrides)
                lines.append(
                    json.dumps(
                        {
                            key: value
                            for key, value in record.items()
                            if value is not None
                        },
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                )
                expected.append(reason)
            mailbox_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

            view = mailbox.read_mailbox(plan_dir, state_dir=state_dir)

            self.assertEqual((), view.messages)
            self.assertEqual(expected, [record.reason for record in view.rejected])
            self.assertIsNone(view.latest_supervision_end)

    def test_supervision_end_appends_where_a_notice_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            created_at = "0001-01-01T00:00:00+00:00"
            message_id = "00000000-0000-4000-8000-000000000001"
            empty_text_record = {
                "created_at": created_at,
                "id": message_id,
                "kind": mailbox.KIND_GUIDANCE,
                "text": "",
                "version": mailbox.MAILBOX_SCHEMA_VERSION,
            }
            record_overhead = len(
                json.dumps(
                    empty_text_record,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
                + b"\n"
            )
            ordinary_cap = (
                mailbox.MAILBOX_MAX_BYTES
                - mailbox.MAILBOX_CLOSURE_RESERVE_BYTES
                - mailbox.MAILBOX_GUIDANCE_DELIVERY_RESERVE_BYTES
            )
            mailbox.append_guidance(
                plan_dir,
                "x" * (ordinary_cap - record_overhead),
                state_dir=state_dir,
                message_id=message_id,
                created_at=created_at,
            )
            self.assertEqual(
                ordinary_cap,
                mailbox.read_mailbox(plan_dir, state_dir=state_dir).size_bytes,
            )

            with self.assertRaises(mailbox.MailboxError) as raised:
                mailbox.append_notice(
                    plan_dir,
                    "supervision over",
                    run_id="run-A",
                    session_id="1",
                    turn_index=1,
                    state_dir=state_dir,
                )
            self.assertEqual(mailbox.ERROR_MAILBOX_FULL, raised.exception.reason)

            appended = mailbox.append_supervision_end(
                plan_dir,
                "reason=complete",
                outcome=mailbox.SUPERVISION_OUTCOME_COMPLETE,
                state_dir=state_dir,
            )

            view = mailbox.read_mailbox(plan_dir, state_dir=state_dir)
            self.assertEqual(appended, view.latest_supervision_end)
            self.assertGreater(view.size_bytes, ordinary_cap)

    def test_supervision_end_counts_extend_the_legacy_counts_payload(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            mailbox.append_guidance(plan_dir, "keep going", state_dir=state_dir)

            legacy_payload = mailbox.read_mailbox(
                plan_dir, state_dir=state_dir
            ).counts.to_payload()
            self.assertEqual(
                {
                    "acknowledgements",
                    "guidance",
                    "pending_guidance",
                    "questions",
                    "rejected",
                    "replies",
                    "resolutions",
                    "notices",
                    "unanswered_questions",
                },
                set(legacy_payload),
            )

            mailbox.append_supervision_end(
                plan_dir,
                "reason=complete",
                outcome=mailbox.SUPERVISION_OUTCOME_COMPLETE,
                state_dir=state_dir,
            )

            counts = mailbox.read_mailbox(plan_dir, state_dir=state_dir).counts
            self.assertEqual(1, counts.supervision_ends)
            payload = counts.to_payload()
            self.assertEqual(1, payload["supervision_ends"])
            self.assertEqual(0, payload["wait_starts"])
            self.assertEqual(0, payload["wait_ends"])
            self.assertEqual(0, payload["receipts"])
            self.assertEqual(
                legacy_payload,
                {key: value for key, value in payload.items() if key in legacy_payload},
            )

    def test_supervision_end_outcome_can_be_resolved_at_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))

            appended = mailbox.append_supervision_end(
                plan_dir,
                "supervision ended: complete",
                outcome=mailbox.SUPERVISION_OUTCOME_COMPLETE,
                supervision_id=mailbox.new_supervision_id(),
                state_dir=state_dir,
                resolve_at_commit=lambda: (
                    mailbox.SUPERVISION_OUTCOME_ERROR,
                    "supervision ended: error - SIGTERM",
                ),
            )

            self.assertEqual(
                mailbox.SUPERVISION_OUTCOME_ERROR, appended.supervision_outcome
            )
            self.assertEqual("supervision ended: error - SIGTERM", appended.text)
            view = mailbox.read_mailbox(plan_dir, state_dir=state_dir)
            self.assertEqual((), view.rejected)
            self.assertEqual(appended, view.latest_supervision_end)

    def test_commit_time_resolution_keeps_the_record_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            message_id = "00000000-0000-4000-8000-00000000000a"
            created_at = "0001-01-01T00:00:00+00:00"

            appended = mailbox.append_supervision_end(
                plan_dir,
                "supervision ended: complete",
                outcome=mailbox.SUPERVISION_OUTCOME_COMPLETE,
                state_dir=state_dir,
                message_id=message_id,
                created_at=created_at,
                resolve_at_commit=lambda: (
                    mailbox.SUPERVISION_OUTCOME_ERROR,
                    "supervision ended: error - SIGINT",
                ),
            )

            self.assertEqual(message_id, appended.id)
            self.assertEqual(created_at, appended.created_at)

    def test_commit_time_resolution_is_validated_like_any_append(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))

            with self.assertRaises(mailbox.MailboxError) as raised:
                mailbox.append_supervision_end(
                    plan_dir,
                    "supervision ended: complete",
                    outcome=mailbox.SUPERVISION_OUTCOME_COMPLETE,
                    state_dir=state_dir,
                    resolve_at_commit=lambda: ("not-an-outcome", "late text"),
                )

            self.assertEqual(
                mailbox.REJECT_INVALID_SUPERVISION_OUTCOME,
                raised.exception.reason,
            )
            self.assertEqual(
                (),
                mailbox.read_mailbox(plan_dir, state_dir=state_dir).messages,
            )

    def test_resolution_that_moves_at_the_write_is_rolled_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            written: list[int] = []
            real_write = mailbox.os.write

            def writing(fd: int, data: bytes) -> int:
                # The resolution moves inside the write itself, the seam no
                # pre-write decision can observe.
                if b'"kind":"supervision-end"' in data:
                    written.append(fd)
                return real_write(fd, data)

            def resolve() -> tuple:
                if written:
                    return (
                        mailbox.SUPERVISION_OUTCOME_ERROR,
                        "supervision ended: error - SIGTERM",
                    )
                return (
                    mailbox.SUPERVISION_OUTCOME_COMPLETE,
                    "supervision ended: complete",
                )

            with mock.patch.object(mailbox.os, "write", writing):
                appended = mailbox.append_supervision_end(
                    plan_dir,
                    "supervision ended: complete",
                    outcome=mailbox.SUPERVISION_OUTCOME_COMPLETE,
                    supervision_id=mailbox.new_supervision_id(),
                    state_dir=state_dir,
                    resolve_at_commit=resolve,
                )

            self.assertEqual(
                mailbox.SUPERVISION_OUTCOME_ERROR, appended.supervision_outcome
            )
            self.assertEqual("supervision ended: error - SIGTERM", appended.text)
            view = mailbox.read_mailbox(plan_dir, state_dir=state_dir)
            self.assertEqual((), view.rejected)
            self.assertEqual((appended,), view.messages)
            self.assertEqual(2, len(written))

    def test_resolution_that_moves_at_the_sync_is_rolled_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            synced: list[int] = []
            real_fullsync = mailbox.durability.fullsync

            def fullsync(fd: int) -> None:
                # The resolution moves inside the durability barrier, after the
                # bytes are written and after every decision that precedes it.
                synced.append(fd)
                real_fullsync(fd)

            def resolve() -> tuple:
                if synced:
                    return (
                        mailbox.SUPERVISION_OUTCOME_ERROR,
                        "supervision ended: error - SIGTERM",
                    )
                return (
                    mailbox.SUPERVISION_OUTCOME_COMPLETE,
                    "supervision ended: complete",
                )

            with mock.patch.object(mailbox.durability, "fullsync", fullsync):
                appended = mailbox.append_supervision_end(
                    plan_dir,
                    "supervision ended: complete",
                    outcome=mailbox.SUPERVISION_OUTCOME_COMPLETE,
                    supervision_id=mailbox.new_supervision_id(),
                    state_dir=state_dir,
                    resolve_at_commit=resolve,
                )

            self.assertEqual(
                mailbox.SUPERVISION_OUTCOME_ERROR, appended.supervision_outcome
            )
            self.assertEqual("supervision ended: error - SIGTERM", appended.text)
            view = mailbox.read_mailbox(plan_dir, state_dir=state_dir)
            self.assertEqual((), view.rejected)
            self.assertEqual((appended,), view.messages)
            # The corrected line clears its own barrier rather than riding out
            # the lock unsynced.
            self.assertEqual(2, len(synced))

    def test_resolution_that_never_settles_still_writes_one_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            calls: list[int] = []

            def resolve() -> tuple:
                calls.append(len(calls))
                return (
                    mailbox.SUPERVISION_OUTCOME_ERROR,
                    f"supervision ended: error - {len(calls)}",
                )

            appended = mailbox.append_supervision_end(
                plan_dir,
                "supervision ended: complete",
                outcome=mailbox.SUPERVISION_OUTCOME_COMPLETE,
                state_dir=state_dir,
                resolve_at_commit=resolve,
            )

            view = mailbox.read_mailbox(plan_dir, state_dir=state_dir)
            self.assertEqual((), view.rejected)
            self.assertEqual((appended,), view.messages)

    def test_message_json_renders_supervision_end_outcome_as_ascii_json(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            message = mailbox.append_supervision_end(
                plan_dir,
                "reason=terminé",
                outcome=mailbox.SUPERVISION_OUTCOME_PAUSED,
                state_dir=state_dir,
            )

            rendered = mailbox_rendering.message_json(message)

            self.assertTrue(rendered.isascii())
            self.assertNotIn("\n", rendered)
            self.assertEqual(
                {
                    "id": message.id,
                    "seq": message.seq,
                    "text": "reason=terminé",
                    "outcome": mailbox.SUPERVISION_OUTCOME_PAUSED,
                },
                json.loads(rendered),
            )

    def test_closure_reserve_cannot_be_consumed_by_non_closure_records(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            question = mailbox.append_question(
                plan_dir,
                "Which mode?",
                state_dir=state_dir,
                message_id="00000000-0000-4000-8000-000000000001",
                created_at="0001-01-01T00:00:00+00:00",
            )
            resolution_question = mailbox.append_question(
                plan_dir,
                "Continue?",
                state_dir=state_dir,
                run_id="run-A",
                message_id="00000000-0000-4000-8000-000000000003",
                created_at="0001-01-01T00:00:00+00:00",
            )
            created_at = "0001-01-01T00:00:00+00:00"
            message_id = "00000000-0000-4000-8000-000000000002"
            empty_text_record = {
                "created_at": created_at,
                "id": message_id,
                "kind": mailbox.KIND_GUIDANCE,
                "text": "",
                "version": mailbox.MAILBOX_SCHEMA_VERSION,
            }
            record_overhead = len(
                json.dumps(
                    empty_text_record,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
                + b"\n"
            )
            # Fill exactly to the ordinary-write cap. Below it sit the
            # guidance-delivery reserve and then the closure/reply reserve, so
            # ordinary records stop short of both.
            ordinary_cap = (
                mailbox.MAILBOX_MAX_BYTES
                - mailbox.MAILBOX_REPLY_RESERVE_BYTES
                - mailbox.MAILBOX_GUIDANCE_DELIVERY_RESERVE_BYTES
            )
            current_size = mailbox.read_mailbox(
                plan_dir, state_dir=state_dir
            ).size_bytes
            mailbox.append_guidance(
                plan_dir,
                "x" * (ordinary_cap - current_size - record_overhead),
                state_dir=state_dir,
                message_id=message_id,
                created_at=created_at,
            )

            view = mailbox.read_mailbox(plan_dir, state_dir=state_dir)
            self.assertEqual(ordinary_cap, view.size_bytes)
            self.assertFalse(view.full)
            self.assertFalse(view.accepts_questions)
            self.assertTrue(view.accepts_replies)
            self.assertTrue(view.accepts_resolutions)
            self.assertTrue(view.accepts_guidance_deliveries)
            view_payload = view.to_payload()
            self.assertFalse(view_payload["full"])
            self.assertFalse(view_payload["accepts_questions"])
            self.assertTrue(view_payload["accepts_replies"])
            self.assertTrue(view_payload["accepts_resolutions"])
            self.assertTrue(view_payload["accepts_guidance_deliveries"])
            self.assertEqual(
                mailbox.MAILBOX_CLOSURE_RESERVE_BYTES,
                mailbox.MAILBOX_REPLY_RESERVE_BYTES,
            )

            for append in (mailbox.append_guidance, mailbox.append_question):
                with self.assertRaises(mailbox.MailboxError) as raised:
                    append(plan_dir, "one more?", state_dir=state_dir)
                self.assertEqual(mailbox.ERROR_MAILBOX_FULL, raised.exception.reason)
            with self.assertRaises(mailbox.MailboxError) as raised:
                mailbox.append_acknowledgement(
                    plan_dir, view.last_seq, state_dir=state_dir
                )
            self.assertEqual(mailbox.ERROR_MAILBOX_FULL, raised.exception.reason)
            with self.assertRaises(mailbox.MailboxError) as raised:
                mailbox.append_notice(
                    plan_dir,
                    "one more notice",
                    run_id="run-A",
                    session_id="1",
                    turn_index=1,
                    state_dir=state_dir,
                )
            self.assertEqual(mailbox.ERROR_MAILBOX_FULL, raised.exception.reason)

            # Guidance still owes an honest delivery outcome, so its reserve
            # survives the ordinary-write refusals above.
            delivery = mailbox.append_guidance_delivery(
                plan_dir,
                message_id,
                delivery_state=mailbox.GUIDANCE_STATE_DEFERRED,
                delivery_reason=mailbox.GUIDANCE_REASON_STALE_TURN,
                channel=mailbox.GUIDANCE_CHANNEL_LIVE,
                state_dir=state_dir,
            )
            self.assertEqual(message_id, delivery.to)

            reply = mailbox.append_reply(
                plan_dir, question.id, "x", state_dir=state_dir
            )
            self.assertEqual(question.id, reply.to)
            resolution = mailbox.append_resolution(
                plan_dir,
                resolution_question.id,
                mailbox.RESOLUTION_ACTION_CONTINUE,
                expected_run_id="run-A",
                state_dir=state_dir,
            )
            self.assertEqual(resolution_question.id, resolution.to)

            payload = state.status_payload(plans.inspect_plan(plan_dir), state_dir)
            status = payload["mailbox"]
            self.assertFalse(status["full"])
            self.assertFalse(status["accepts_questions"])
            self.assertTrue(status["accepts_replies"])
            self.assertTrue(status["accepts_resolutions"])

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                state._print_text_status(payload)
            output = buffer.getvalue()
            self.assertIn(
                "Mailbox: active, non-closure writes refused "
                "(replies and resolutions still accepted)",
                output,
            )
            self.assertNotIn("Mailbox: active, full", output)

    def test_status_reports_when_minimal_reply_cannot_land(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            created_at = "0001-01-01T00:00:00+00:00"
            first_question = mailbox.append_question(
                plan_dir,
                "first?",
                state_dir=state_dir,
                message_id="00000000-0000-4000-8000-000000000001",
                created_at=created_at,
            )
            second_question = mailbox.append_question(
                plan_dir,
                "second?",
                state_dir=state_dir,
                message_id="00000000-0000-4000-8000-000000000002",
                created_at=created_at,
            )
            reply_id = "00000000-0000-4000-8000-000000000003"
            empty_reply_record = {
                "created_at": created_at,
                "id": reply_id,
                "kind": mailbox.KIND_REPLY,
                "text": "",
                "to": first_question.id,
                "version": mailbox.MAILBOX_SCHEMA_VERSION,
            }
            reply_overhead = len(
                json.dumps(
                    empty_reply_record,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
                + b"\n"
            )
            target_size = (
                mailbox.MAILBOX_MAX_BYTES - mailbox.MAILBOX_MIN_REPLY_BYTES + 1
            )
            current_size = mailbox.read_mailbox(
                plan_dir, state_dir=state_dir
            ).size_bytes
            mailbox.append_reply(
                plan_dir,
                first_question.id,
                "x" * (target_size - current_size - reply_overhead),
                state_dir=state_dir,
                message_id=reply_id,
                created_at=created_at,
            )

            view = mailbox.read_mailbox(plan_dir, state_dir=state_dir)
            self.assertEqual(target_size, view.size_bytes)
            self.assertFalse(view.full)
            self.assertFalse(view.accepts_replies)
            with self.assertRaises(mailbox.MailboxError) as raised:
                mailbox.append_reply(
                    plan_dir, second_question.id, "x", state_dir=state_dir
                )
            self.assertEqual(mailbox.ERROR_MAILBOX_FULL, raised.exception.reason)

            payload = state.status_payload(plans.inspect_plan(plan_dir), state_dir)
            status = payload["mailbox"]
            self.assertFalse(status["full"])
            self.assertFalse(status["accepts_replies"])
            self.assertFalse(status["accepts_resolutions"])
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                state._print_text_status(payload)
            output = buffer.getvalue()
            self.assertIn("reply and resolution capacity exhausted", output)
            self.assertNotIn("still accepted", output)

    def test_status_payload_surfaces_mailbox_read_error_and_stale_lease(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            mailbox_path = mailbox.mailbox_path_for_plan(plan_dir, state_dir)
            mailbox_path.parent.mkdir(parents=True)
            mailbox_path.touch()
            mailbox.write_lease(
                plan_dir,
                "00000000-0000-4000-8000-000000000001",
                state_dir=state_dir,
                updated_at="2020-01-01T00:00:00+00:00",
            )

            read_error = mailbox.MailboxError(
                mailbox.ERROR_MAILBOX_READ,
                "permission denied",
                path=mailbox_path,
            )
            with mock.patch.object(mailbox, "read_mailbox", side_effect=read_error):
                payload = state.status_payload(plans.inspect_plan(plan_dir), state_dir)

            status = payload["mailbox"]
            self.assertTrue(status["present"])
            self.assertIsNone(status["size_bytes"])
            self.assertIsNone(status["full"])
            self.assertIsNone(status["accepts_questions"])
            self.assertIsNone(status["accepts_replies"])
            self.assertIsNone(status["accepts_resolutions"])
            self.assertIsNone(status["resolutions"])
            self.assertIsNone(status["notices"])
            self.assertIn(mailbox.ERROR_MAILBOX_READ, status["error"])
            self.assertTrue(status["lease_present"])
            self.assertFalse(status["lease_fresh"])
            self.assertEqual("2020-01-01T00:00:00+00:00", status["lease_updated_at"])
            self.assertEqual(mailbox.LEASE_EXPIRED, status["lease_reason"])
            self.assertIsNone(status["lease_error"])

    def test_status_projects_waiting_for_reply_without_hiding_pause_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            plan_state = state.plan_state_dir(plan_dir, state_dir)
            plan_state.mkdir(parents=True)
            waiting = {
                "question_id": "question-1",
                "session": 1,
                "session_id": "1",
                "turn_index": 2,
                "started_at": "2026-07-24T12:00:00+00:00",
                "base_deadline": "2026-07-24T12:05:00+00:00",
                "hard_deadline": "2026-07-24T12:15:00+00:00",
                "last_lease_activity_at": "2026-07-24T12:01:00+00:00",
            }
            (plan_state / events.STATE_FILE_NAME).write_text(
                json.dumps(
                    {
                        "run_id": "run-1",
                        "paused": True,
                        "reason": "blocked",
                        # The runner persists this block alongside every pause;
                        # it is what keeps the pause legible after the
                        # abandonment backfill rewrites the top-level reason.
                        "pause_policy": pause_policy.classify_pause("blocked"),
                        "run_started_at": "2026-07-24T11:59:00+00:00",
                        "run_finished_at": None,
                        "waiting_for_reply": waiting,
                    }
                ),
                encoding="utf-8",
            )

            payload = state.status_payload(plans.inspect_plan(plan_dir), state_dir)

            self.assertEqual(waiting, payload["waiting_for_reply"])
            self.assertEqual(waiting, payload["runner_state"]["waiting_for_reply"])
            self.assertEqual(
                state.WAITING_FOR_REPLY_STATUS_EXPIRED,
                payload["waiting_for_reply_status"],
            )
            self.assertTrue(payload["runner_state"]["paused"])
            self.assertEqual(
                "blocked", payload["runner_state"]["pause_policy"]["reason"]
            )
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                state._print_text_status(payload)
            output = buffer.getvalue()
            self.assertIn("Mailbox: disabled", output)
            self.assertIn(
                "Waiting for reply: question question-1 session 1 turn 2",
                output,
            )
            self.assertIn(
                "Deadlines: base 2026-07-24T12:05:00+00:00, "
                "hard 2026-07-24T12:15:00+00:00",
                output,
            )

    def test_status_projects_live_waiting_as_nonterminal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            plan_state = state.plan_state_dir(plan_dir, state_dir)
            plan_state.mkdir(parents=True)
            waiting = {
                "question_id": "question-1",
                "session": 1,
                "session_id": "1",
                "turn_index": 2,
                "started_at": "2999-07-24T12:00:00+00:00",
                "base_deadline": "2999-07-24T12:05:00+00:00",
                "hard_deadline": "2999-07-24T12:15:00+00:00",
                "last_lease_activity_at": None,
            }
            (plan_state / events.STATE_FILE_NAME).write_text(
                json.dumps(
                    {
                        "run_id": "run-1",
                        "ok": True,
                        "paused": False,
                        "reason": "",
                        "run_started_at": "2026-07-24T11:59:00+00:00",
                        "run_finished_at": None,
                        "waiting_for_reply": waiting,
                    }
                ),
                encoding="utf-8",
            )
            lock_info = state.acquire_lock(plan_dir, state_dir)

            try:
                payload = state.status_payload(
                    plans.inspect_plan(plan_dir),
                    state_dir,
                )
            finally:
                state.release_lock(plan_dir, state_dir)

            self.assertEqual(waiting, payload["waiting_for_reply"])
            self.assertEqual(waiting, payload["runner_state"]["waiting_for_reply"])
            self.assertTrue(lock_info.active)
            self.assertEqual(
                state.WAITING_FOR_REPLY_STATUS_LIVE,
                payload["waiting_for_reply_status"],
            )
            self.assertTrue(payload["runner_state"]["ok"])
            self.assertFalse(payload["runner_state"]["paused"])
            self.assertIsNone(payload["runner_state"]["pause_policy"])

    def test_runner_state_payload_requires_explicit_lock_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / events.STATE_FILE_NAME
            state_path.write_text(
                json.dumps(
                    {
                        "waiting_for_reply": {
                            "hard_deadline": "2999-07-24T12:15:00+00:00",
                        }
                    }
                ),
                encoding="utf-8",
            )

            # Bound dynamically because the assertion is about runtime argument
            # binding, which a static signature check rejects before it runs.
            payload_builder: Any = state._runner_state_payload
            with self.assertRaisesRegex(TypeError, "lock_info"):
                payload_builder(state_path)

            payload = state._runner_state_payload(
                state_path,
                lock_info=None,
            )
            self.assertEqual(
                state.WAITING_FOR_REPLY_STATUS_EXPIRED,
                payload["waiting_for_reply_status"],
            )

    def test_runner_state_payload_exposes_review_harness(self) -> None:
        # `asl control stop-after-current` reports whether the stop also
        # prevents a pending review, which it can only know from the run's
        # configured review harness.
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / events.STATE_FILE_NAME
            state_path.write_text(
                json.dumps(
                    {
                        "run_id": "run-1",
                        "harness": "codex",
                        "run_config": {"review_harness": "claude"},
                    }
                ),
                encoding="utf-8",
            )

            payload = state._runner_state_payload(
                state_path, lock_info=self._active_lock()
            )

            self.assertEqual("codex", payload["harness"])
            self.assertEqual("claude", payload["review_harness"])

            state_path.write_text(
                json.dumps({"run_id": "run-1", "harness": "codex"}),
                encoding="utf-8",
            )
            without_review = state._runner_state_payload(
                state_path, lock_info=self._active_lock()
            )
            self.assertIsNone(without_review["review_harness"])

    def test_runner_state_payload_exposes_finalization_and_control(self) -> None:
        # A supervisor reading a run that holds the lock with no turn in flight
        # needs both: the phase says the run is working, and `control` is the
        # only record of the request that ended it.
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / events.STATE_FILE_NAME
            finalization = {
                "run_id": "run-1",
                "phase": "assurance-audit",
                "elapsed_seconds": 42.0,
                "cancellable": True,
            }
            state_path.write_text(
                json.dumps(
                    {
                        "run_id": "run-1",
                        "harness": "codex",
                        "active_finalization": finalization,
                        "control": {"last_action": "finalization-abort-honored"},
                    }
                ),
                encoding="utf-8",
            )

            payload = state._runner_state_payload(
                state_path, lock_info=self._active_lock()
            )

            self.assertEqual(finalization, payload["active_finalization"])
            self.assertEqual(
                "finalization-abort-honored", payload["control"]["last_action"]
            )

            state_path.write_text(json.dumps({"run_id": "run-1"}), encoding="utf-8")
            without = state._runner_state_payload(
                state_path, lock_info=self._active_lock()
            )
            self.assertIsNone(without["active_finalization"])
            self.assertIsNone(without["control"])

    def _active_lock(self) -> state.LockInfo:
        return state.LockInfo(
            path=Path("/tmp/plan-lock.json"),
            pid=os.getpid(),
            plan_dir="/tmp/plan",
            created_at="2026-07-24T12:00:00+00:00",
            stale=False,
            reason="active",
            raw={},
        )

    def test_historical_hard_deadline_waits_classify_unchanged(self) -> None:
        # Records persisted before the ceiling began rolling carry only the
        # bare timestamp; they must classify exactly as they always did.
        lock_info = self._active_lock()
        started = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
        historical = {
            "question_id": "question-1",
            "started_at": started.isoformat(),
            "base_deadline": (started + timedelta(minutes=5)).isoformat(),
            "hard_deadline": (started + timedelta(minutes=30)).isoformat(),
            "last_lease_activity_at": (started + timedelta(minutes=29)).isoformat(),
        }

        self.assertEqual(
            state.WAITING_FOR_REPLY_STATUS_LIVE,
            state.waiting_for_reply_status(
                historical,
                lock_info=lock_info,
                now=started + timedelta(minutes=29),
            ),
        )
        # Recent lease activity cannot roll a historical record forward: it
        # never recorded the ceiling span that would justify the extension.
        self.assertEqual(
            state.WAITING_FOR_REPLY_STATUS_EXPIRED,
            state.waiting_for_reply_status(
                historical,
                lock_info=lock_info,
                now=started + timedelta(minutes=30),
            ),
        )
        self.assertEqual(
            state.WAITING_FOR_REPLY_STATUS_EXPIRED,
            state.waiting_for_reply_status(
                historical,
                lock_info=None,
                now=started + timedelta(minutes=10),
            ),
        )
        for broken in ({}, {"hard_deadline": ""}, {"hard_deadline": "nope"}):
            self.assertEqual(
                state.WAITING_FOR_REPLY_STATUS_INVALID,
                state.waiting_for_reply_status(
                    broken,
                    lock_info=lock_info,
                    now=started,
                ),
                msg=f"{broken!r} should be invalid",
            )

    def test_rolling_ceiling_keeps_supervised_wait_live(self) -> None:
        lock_info = self._active_lock()
        started = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
        supervised = {
            "question_id": "question-1",
            "started_at": started.isoformat(),
            "base_deadline": (started + timedelta(minutes=5)).isoformat(),
            "unsupervised_deadline_at": (started + timedelta(minutes=30)).isoformat(),
            "unsupervised_ceiling_seconds": 1800.0,
            "last_lease_activity_at": (started + timedelta(minutes=90)).isoformat(),
        }

        # Two hours in, the persisted anchor is 90 minutes stale, but the last
        # lease proved a supervisor alive 30 minutes ago.
        self.assertEqual(
            state.WAITING_FOR_REPLY_STATUS_LIVE,
            state.waiting_for_reply_status(
                supervised,
                lock_info=lock_info,
                now=started + timedelta(minutes=119),
            ),
        )
        self.assertEqual(
            state.WAITING_FOR_REPLY_STATUS_EXPIRED,
            state.waiting_for_reply_status(
                supervised,
                lock_info=lock_info,
                now=started + timedelta(minutes=120),
            ),
        )
        # An absent lock still expires the wait immediately: the projected
        # ceiling only says nobody proved supervision, not that a runner
        # remains to accept the reply.
        self.assertEqual(
            state.WAITING_FOR_REPLY_STATUS_EXPIRED,
            state.waiting_for_reply_status(
                supervised,
                lock_info=None,
                now=started + timedelta(minutes=119),
            ),
        )

    def test_unwatched_wait_expires_at_its_persisted_ceiling(self) -> None:
        lock_info = self._active_lock()
        started = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
        unwatched = {
            "question_id": "question-1",
            "started_at": started.isoformat(),
            "base_deadline": (started + timedelta(minutes=5)).isoformat(),
            "unsupervised_deadline_at": (started + timedelta(minutes=30)).isoformat(),
            "unsupervised_ceiling_seconds": 1800.0,
            "last_lease_activity_at": None,
        }

        self.assertEqual(
            state.WAITING_FOR_REPLY_STATUS_LIVE,
            state.waiting_for_reply_status(
                unwatched,
                lock_info=lock_info,
                now=started + timedelta(minutes=29),
            ),
        )
        self.assertEqual(
            state.WAITING_FOR_REPLY_STATUS_EXPIRED,
            state.waiting_for_reply_status(
                unwatched,
                lock_info=lock_info,
                now=started + timedelta(minutes=30),
            ),
        )
        self.assertEqual(
            state.WAITING_FOR_REPLY_STATUS_INVALID,
            state.waiting_for_reply_status(
                {**unwatched, "unsupervised_deadline_at": "nope"},
                lock_info=lock_info,
                now=started,
            ),
        )
        # A malformed span degrades to the persisted anchor rather than
        # discarding a wait that is otherwise well formed.
        for bad_ceiling in (0, -1.0, float("inf"), "1800", True, None):
            self.assertEqual(
                (started + timedelta(minutes=30)),
                state.waiting_for_reply_ceiling(
                    {
                        **unwatched,
                        "unsupervised_ceiling_seconds": bad_ceiling,
                        "last_lease_activity_at": (
                            started + timedelta(minutes=90)
                        ).isoformat(),
                    }
                ),
                msg=f"ceiling {bad_ceiling!r} should not roll the deadline",
            )

    def test_status_text_labels_the_rolling_ceiling(self) -> None:
        # The historical `hard ...` rendering is pinned by
        # test_status_projects_waiting_for_reply_without_hiding_pause_state.
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            plan_state = state.plan_state_dir(plan_dir, state_dir)
            plan_state.mkdir(parents=True)
            (plan_state / events.STATE_FILE_NAME).write_text(
                json.dumps(
                    {
                        "run_id": "run-1",
                        "ok": True,
                        "paused": False,
                        "reason": "",
                        "run_started_at": "2026-07-24T11:59:00+00:00",
                        "run_finished_at": None,
                        "waiting_for_reply": {
                            "question_id": "question-1",
                            "session_id": "1",
                            "turn_index": 2,
                            "started_at": "2026-07-24T12:00:00+00:00",
                            "base_deadline": "2026-07-24T12:05:00+00:00",
                            "unsupervised_deadline_at": ("2026-07-24T12:30:00+00:00"),
                            "unsupervised_ceiling_seconds": 1800.0,
                            "last_lease_activity_at": None,
                        },
                    }
                ),
                encoding="utf-8",
            )

            payload = state.status_payload(
                plans.inspect_plan(plan_dir),
                state_dir,
            )
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                state._print_text_status(payload)

            self.assertIn(
                "Deadlines: base 2026-07-24T12:05:00+00:00, "
                "unsupervised 2026-07-24T12:30:00+00:00",
                buffer.getvalue(),
            )

    def test_status_payload_progress_journal_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))

            payload = state.status_payload(plans.inspect_plan(plan_dir), state_dir)

            journal = payload["progress_journal"]
            self.assertFalse(journal["present"])
            self.assertIsNone(journal["entry_count"])
            self.assertIsNone(journal["last_appended"])
            self.assertIsNone(journal["error"])
            self.assertTrue(journal["path"].endswith("/progress.md"))

    def test_status_payload_progress_journal_with_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("complete", "pending"))

            plans.append_progress_entry(
                plan_dir,
                plans.ProgressEntry(
                    session_id=plans.parse_session_id("1"),
                    status="complete",
                    completed_at="2026-06-19T14:22:00Z",
                    files_modified=("cli.py",),
                ),
            )

            payload = state.status_payload(plans.inspect_plan(plan_dir), state_dir)

            journal = payload["progress_journal"]
            self.assertTrue(journal["present"])
            self.assertEqual(1, journal["entry_count"])
            self.assertEqual("2026-06-19T14:22:00Z", journal["last_appended"])
            self.assertIsNone(journal["error"])

    def test_status_payload_progress_journal_malformed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            plans.progress_path(plan_dir).write_text(
                "# Progress Journal\n\nmissing version comment\n",
                encoding="utf-8",
            )

            payload = state.status_payload(plans.inspect_plan(plan_dir), state_dir)

            journal = payload["progress_journal"]
            self.assertTrue(journal["present"])
            self.assertIsNone(journal["entry_count"])
            self.assertIsNone(journal["last_appended"])
            self.assertIsNotNone(journal["error"])

    def test_status_from_args_text_output_renders_journal_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))

            args = argparse.Namespace(
                plan_dir=str(plan_dir),
                state_dir=str(state_dir),
                json=False,
            )
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                exit_code = state.status_from_args(args)

            self.assertEqual(0, exit_code)
            output = buffer.getvalue()
            self.assertIn("Progress journal: not initialized", output)

            plans.append_progress_entry(
                plan_dir,
                plans.ProgressEntry(
                    session_id=plans.parse_session_id("1"),
                    status="complete",
                    completed_at="2026-06-19T14:22:00Z",
                ),
            )

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                exit_code = state.status_from_args(args)

            self.assertEqual(0, exit_code)
            output = buffer.getvalue()
            self.assertIn(
                "Progress journal: 1 entry, last appended 2026-06-19T14:22:00Z",
                output,
            )

    def test_status_payload_backfills_old_no_lock_runner_state_as_abandoned(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(
                plan_dir,
                ("complete", "complete", "complete", "complete", "complete"),
            )
            plan_state = state.plan_state_dir(plan_dir, state_dir)
            plan_state.mkdir(parents=True)
            state_path = plan_state / events.STATE_FILE_NAME
            state_path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "paused": False,
                        "run_id": "20260619T120000Z-1",
                        "harness": "claude",
                        "reason": "",
                        "run_started_at": "2026-06-19T12:00:00+00:00",
                        "run_finished_at": None,
                        "duration_seconds": None,
                        "active_turn": None,
                        "final_snapshot": {
                            "counts": {
                                "pending": 0,
                                "complete": 5,
                                "empty": 0,
                                "unknown": 0,
                            },
                            "next_pending_id": "5",
                        },
                    }
                ),
                encoding="utf-8",
            )

            payload = state.status_payload(plans.inspect_plan(plan_dir), state_dir)

            self.assertIsNone(payload["next_pending"])
            self.assertIsNone(payload["next_pending_id"])
            self.assertEqual(0, payload["counts"]["pending"])
            self.assertEqual(5, payload["counts"]["complete"])

            # The same call that found the orphan reports the terminal state
            # it wrote, so no second `asl status` is needed to see it.
            runner_state = payload["runner_state"]
            self.assertTrue(runner_state["present"])
            self.assertFalse(runner_state["stale_unfinished"])
            self.assertIsNone(runner_state["stale_reason"])
            self.assertFalse(runner_state["ok"])
            self.assertEqual(state.ABANDONED_REASON, runner_state["reason"])
            self.assertIsNotNone(runner_state["run_finished_at"])
            abandoned = runner_state["abandoned"]
            self.assertEqual(state.ABANDONED_TRIGGER_STATUS, abandoned["trigger"])
            self.assertEqual(os.getpid(), abandoned["detected_by_pid"])
            self.assertIsNone(abandoned["lock_reason"])

            records = read_jsonl(plan_state / events.EVENTS_FILE_NAME)
            self.assertEqual(1, len(records))
            self.assertEqual(events.RUN_FINISHED_EVENT, records[0]["name"])
            self.assertEqual(events.EVENT_SCHEMA_VERSION, records[0]["version"])
            self.assertTrue(records[0]["payload"]["synthetic"])
            self.assertEqual(
                events.RUN_OUTCOME_ABANDONED, records[0]["payload"]["outcome"]
            )
            self.assertEqual("20260619T120000Z-1", records[0]["payload"]["run_id"])
            self.assertEqual(
                {"pending": 0, "complete": 5, "empty": 0, "unknown": 0},
                records[0]["payload"]["counts"],
            )

    def test_status_payload_leaves_fresh_no_lock_runner_state_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            plan_state = state.plan_state_dir(plan_dir, state_dir)
            plan_state.mkdir(parents=True)
            # A runner that just started writes state.json before its lock, so
            # the missing lock inside the grace window proves nothing.
            (plan_state / events.STATE_FILE_NAME).write_text(
                json.dumps(
                    {
                        "ok": True,
                        "paused": False,
                        "run_id": "20260619T120000Z-9",
                        "harness": "claude",
                        "reason": "",
                        "run_started_at": datetime.now(UTC).isoformat(),
                        "run_finished_at": None,
                        "active_turn": None,
                    }
                ),
                encoding="utf-8",
            )

            payload = state.status_payload(plans.inspect_plan(plan_dir), state_dir)

            runner_state = payload["runner_state"]
            self.assertTrue(runner_state["stale_unfinished"])
            self.assertIn("no lock file is present", runner_state["stale_reason"])
            self.assertIsNone(runner_state["abandoned"])
            self.assertIsNone(runner_state["run_finished_at"])
            self.assertFalse((plan_state / events.EVENTS_FILE_NAME).exists())

    def test_status_payload_does_not_flag_stale_when_finished(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("complete", "complete"))
            plan_state = state.plan_state_dir(plan_dir, state_dir)
            plan_state.mkdir(parents=True)
            state_path = plan_state / events.STATE_FILE_NAME
            state_path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "paused": False,
                        "run_id": "20260619T120000Z-2",
                        "harness": "claude",
                        "reason": "",
                        "run_started_at": "2026-06-19T12:00:00+00:00",
                        "run_finished_at": "2026-06-19T12:30:00+00:00",
                        "duration_seconds": 1800.0,
                        "active_turn": None,
                    }
                ),
                encoding="utf-8",
            )

            payload = state.status_payload(plans.inspect_plan(plan_dir), state_dir)

            runner_state = payload["runner_state"]
            self.assertTrue(runner_state["present"])
            self.assertFalse(runner_state["stale_unfinished"])
            self.assertIsNone(runner_state["stale_reason"])

    def test_status_payload_does_not_flag_stale_when_active_lock_held(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending", "pending"))
            plan_state = state.plan_state_dir(plan_dir, state_dir)
            plan_state.mkdir(parents=True)
            state_path = plan_state / events.STATE_FILE_NAME
            state_path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "paused": False,
                        "run_id": "20260619T120000Z-3",
                        "harness": "claude",
                        "reason": "",
                        "run_started_at": "2026-06-19T12:00:00+00:00",
                        "run_finished_at": None,
                        "duration_seconds": None,
                        "active_turn": None,
                    }
                ),
                encoding="utf-8",
            )
            lock_info = state.acquire_lock(plan_dir, state_dir)
            try:
                payload = state.status_payload(plans.inspect_plan(plan_dir), state_dir)
            finally:
                state.release_lock(plan_dir, state_dir)

            self.assertTrue(payload["lock"]["active"])
            self.assertEqual(lock_info.pid, payload["lock"]["pid"])
            runner_state = payload["runner_state"]
            self.assertTrue(runner_state["present"])
            self.assertFalse(runner_state["stale_unfinished"])
            self.assertIsNone(runner_state["stale_reason"])

    def test_status_payload_exposes_active_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            plan_state = state.plan_state_dir(plan_dir, state_dir)
            plan_state.mkdir(parents=True)
            state_path = plan_state / events.STATE_FILE_NAME
            active_verification = {
                "run_id": "20260620T100000Z-1",
                "turn_index": 1,
                "session": 1,
                "session_id": "1",
                "total_sessions": 1,
                "check_count": 2,
                "check_names": ["operator-surface-tests", "operator-surface-compile"],
                "check_kinds": ["command", "command"],
                "started_at": "2026-06-20T10:00:00+00:00",
                "current_check_index": 0,
                "current_check_name": "operator-surface-tests",
                "current_check_kind": "command",
                "current_check_started_at": "2026-06-20T10:00:01+00:00",
                "elapsed_seconds": 2.0,
                "check_elapsed_seconds": 1.0,
                "heartbeat_count": 0,
            }
            state_path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "paused": False,
                        "run_id": "20260620T100000Z-1",
                        "harness": "claude",
                        "reason": "",
                        "run_started_at": "2026-06-20T10:00:00+00:00",
                        "run_finished_at": None,
                        "duration_seconds": None,
                        "active_turn": None,
                        "active_verification": active_verification,
                    }
                ),
                encoding="utf-8",
            )

            # A run with a check in flight holds its lock; without one the
            # status backfill would (correctly) declare the run abandoned.
            state.acquire_lock(plan_dir, state_dir)
            try:
                payload = state.status_payload(plans.inspect_plan(plan_dir), state_dir)
            finally:
                state.release_lock(plan_dir, state_dir)

            self.assertEqual(active_verification, payload["active_verification"])
            self.assertEqual(
                active_verification,
                payload["runner_state"]["active_verification"],
            )

    def test_status_payload_active_verification_null_when_idle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            plan_state = state.plan_state_dir(plan_dir, state_dir)
            plan_state.mkdir(parents=True)
            state_path = plan_state / events.STATE_FILE_NAME
            state_path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "paused": False,
                        "run_id": "20260620T100000Z-2",
                        "harness": "claude",
                        "reason": "",
                        "run_started_at": "2026-06-20T10:00:00+00:00",
                        "run_finished_at": None,
                        "duration_seconds": None,
                        "active_turn": None,
                        "active_verification": None,
                    }
                ),
                encoding="utf-8",
            )

            payload = state.status_payload(plans.inspect_plan(plan_dir), state_dir)

            self.assertIsNone(payload["active_verification"])
            self.assertIsNone(payload["runner_state"]["active_verification"])

    def test_status_from_args_text_output_renders_active_verification(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            plan_state = state.plan_state_dir(plan_dir, state_dir)
            plan_state.mkdir(parents=True)
            state_path = plan_state / events.STATE_FILE_NAME
            state_path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "paused": False,
                        "run_id": "20260620T100000Z-3",
                        "harness": "claude",
                        "reason": "",
                        "run_started_at": "2026-06-20T10:00:00+00:00",
                        "run_finished_at": None,
                        "duration_seconds": None,
                        "active_turn": None,
                        "active_verification": {
                            "run_id": "20260620T100000Z-3",
                            "turn_index": 1,
                            "session": 1,
                            "session_id": "1",
                            "total_sessions": 1,
                            "check_count": 2,
                            "check_names": [
                                "operator-surface-tests",
                                "operator-surface-compile",
                            ],
                            "check_kinds": ["command", "command"],
                            "started_at": "2026-06-20T10:00:00+00:00",
                            "current_check_index": 0,
                            "current_check_name": "operator-surface-tests",
                            "current_check_kind": "command",
                            "current_check_started_at": ("2026-06-20T10:00:01+00:00"),
                            "elapsed_seconds": 2.0,
                            "check_elapsed_seconds": 1.0,
                            "heartbeat_count": 0,
                        },
                    }
                ),
                encoding="utf-8",
            )

            args = argparse.Namespace(
                plan_dir=str(plan_dir),
                state_dir=str(state_dir),
                json=False,
            )
            buffer = io.StringIO()
            # A run with a check in flight holds its lock; without one the
            # status backfill would (correctly) declare the run abandoned.
            state.acquire_lock(plan_dir, state_dir)
            try:
                with redirect_stdout(buffer):
                    exit_code = state.status_from_args(args)
            finally:
                state.release_lock(plan_dir, state_dir)

            self.assertEqual(0, exit_code)
            output = buffer.getvalue()
            self.assertIn(
                "Active verification: session 1 turn 1 check 1/2 "
                "operator-surface-tests",
                output,
            )
            self.assertIn("Check kind: command", output)

    def test_status_from_args_text_output_flags_stale_unfinished(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(
                plan_dir,
                ("complete", "complete", "complete", "complete", "complete"),
            )
            plan_state = state.plan_state_dir(plan_dir, state_dir)
            plan_state.mkdir(parents=True)
            state_path = plan_state / events.STATE_FILE_NAME
            state_path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "paused": False,
                        "run_id": "20260619T120000Z-4",
                        "harness": "claude",
                        "reason": "",
                        # Inside the no-lock grace window, so the orphan is
                        # reported without being declared dead.
                        "run_started_at": datetime.now(UTC).isoformat(),
                        "run_finished_at": None,
                        "duration_seconds": None,
                        "active_turn": None,
                    }
                ),
                encoding="utf-8",
            )

            args = argparse.Namespace(
                plan_dir=str(plan_dir),
                state_dir=str(state_dir),
                json=False,
            )
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                exit_code = state.status_from_args(args)

            self.assertEqual(0, exit_code)
            output = buffer.getvalue()
            self.assertIn("Run state: stale/orphaned", output)
            self.assertNotIn("Run state: abandoned", output)
            self.assertIn("Next pending: none", output)

    # --- Schema v2 state/result/event compaction -----------------------------

    @staticmethod
    def _snapshot_dict(pending: int) -> dict[str, object]:
        return {
            "plan_dir": "/plan",
            "plan_hash": "abc",
            "content_sha256": "def",
            "path_hash": "hash",
            "counts": {"pending": pending, "complete": 1, "empty": 0, "unknown": 0},
            "next_pending": 2,
            "next_pending_id": "2",
            "sessions": [
                {
                    "id": str(index),
                    "status": "pending",
                    "raw_value": "pending",
                    "line_number": index,
                    "path": f"/plan/session-{index}.md",
                    "number": index,
                }
                for index in range(1, pending + 2)
            ],
        }

    def _verification_check(self) -> dict[str, object]:
        return {
            "name": "tests",
            "kind": "command",
            "status": "fail",
            "failure_reason": "command-failed",
            "returncode": 1,
            "duration_seconds": 1.5,
            "stdout": "x" * 8192,
            "stderr": "y" * 8192,
            "stdout_byte_count": 8192,
            "stderr_byte_count": 8192,
            "stdout_sha256": "s" * 64,
            "stderr_sha256": "t" * 64,
            "stdout_artifact_path": "/artifacts/tests-stdout.gz",
            "stderr_artifact_path": "/artifacts/tests-stderr.gz",
        }

    def _detailed_turn(self) -> dict[str, object]:
        check = self._verification_check()
        return {
            "version": turn_execution.RESULT_SCHEMA_VERSION,
            "run_id": "run-1",
            "turn_index": 1,
            "session": 1,
            "session_id": "1",
            "harness": "codex",
            "model": "gpt-5",
            "model_source": "flag",
            "argv": ["codex", "run"],
            "returncode": 0,
            "status_result": "complete",
            "stdout_path": "/artifacts/stdout.log",
            "stderr_path": "/artifacts/stderr.log",
            "result_path": "/artifacts/result.json",
            "artifact_dir": "/artifacts",
            "total_duration_seconds": 1.2,
            "before": self._snapshot_dict(2),
            "after": self._snapshot_dict(1),
            "verification": {
                "session": "1",
                "reason": "verify-failed",
                "passed": False,
                "recipe": {"check_count": 1, "kinds": ["command"], "names": ["tests"]},
                "checks": [check],
            },
        }

    def _base_state_payload(self, state_path: Path) -> dict[str, object]:
        return {
            "version_absent_is_v1": True,
            "state_path": str(state_path),
            "plan_dir": str(state_path.parent),
            "run_id": "run-1",
            "harness": "codex",
            "ok": True,
            "paused": False,
            "reason": "",
            "run_started_at": "2026-06-20T10:00:00+00:00",
            "run_finished_at": None,
            "duration_seconds": None,
            "active_turn": {"session": 1, "turn_index": 1, "harness": "codex"},
            "active_verification": None,
            "control": {"next_harness": "codex", "last_action": "harness-selected"},
            "final_snapshot": self._snapshot_dict(0),
            "completed_sessions": [1],
            "completed_session_ids": ["1"],
            "turns": [self._detailed_turn()],
        }

    def test_write_state_payload_compacts_turns_and_stamps_the_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            payload = self._base_state_payload(state_path)
            run_persistence._write_state_payload(payload)

            persisted = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(
            agent_mux_projection.STATE_SCHEMA_VERSION, persisted["version"]
        )
        # The projection rides in the same write the version names.
        self.assertEqual(
            "turn",
            persisted[agent_mux_projection.PROJECTION_KEY]["phase"],
        )
        turn = persisted["turns"][0]
        # Scalar/path fields readers depend on are preserved.
        self.assertEqual("/artifacts/stdout.log", turn["stdout_path"])
        self.assertEqual("codex", turn["harness"])
        self.assertEqual("complete", turn["status_result"])
        # Heavy snapshot session lists are dropped; counts survive.
        self.assertNotIn("sessions", turn["before"])
        self.assertNotIn("sessions", turn["after"])
        self.assertEqual(
            {"pending": 1, "complete": 1, "empty": 0, "unknown": 0},
            turn["after"]["counts"],
        )
        # No raw verification output survives, but artifact references do.
        check = turn["verification"]["checks"][0]
        self.assertNotIn("stdout", check)
        self.assertNotIn("stderr", check)
        self.assertEqual("/artifacts/tests-stdout.gz", check["stdout_artifact_path"])
        self.assertEqual(8192, check["stdout_byte_count"])
        # The in-memory payload is untouched (no aliasing of the compact copy).
        self.assertIn("sessions", payload["turns"][0]["after"])
        self.assertIn("stdout", payload["turns"][0]["verification"]["checks"][0])
        self.assertNotIn("version", payload)

    def test_state_json_stays_under_one_megabyte_for_fifty_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            payload = self._base_state_payload(state_path)
            turns = []
            for index in range(50):
                turn = self._detailed_turn()
                turn["turn_index"] = index + 1
                turn["before"] = self._snapshot_dict(50)
                turn["after"] = self._snapshot_dict(50)
                turns.append(turn)
            payload["turns"] = turns
            run_persistence._write_state_payload(payload)

            size = state_path.stat().st_size

        self.assertLess(size, 1_000_000, f"state.json was {size} bytes")

    def test_run_finished_event_is_compact_without_raw_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / "events.jsonl"
            payload = self._base_state_payload(Path(tmp) / "state.json")
            payload["events_path"] = str(event_path)
            persistence = run_persistence._RunPersistence(write_state=False)
            commit_settlement._finish_run(
                event_path,
                payload,
                persistence=persistence,
                finalize_readiness=run_termination.readiness_finalizer(
                    readiness_gate.write_readiness_state
                ),
            )

            records = [
                json.loads(line)
                for line in event_path.read_text(encoding="utf-8").splitlines()
            ]

        finished = [r for r in records if r["name"] == "run_finished"]
        self.assertEqual(1, len(finished))
        record = finished[0]
        self.assertEqual(events.EVENT_SCHEMA_VERSION, record["version"])
        event_payload = record["payload"]
        # Compact: run outcome, counts, review summary, control — but no turns.
        self.assertNotIn("turns", event_payload)
        self.assertEqual(payload["control"], event_payload["control"])
        self.assertEqual(1, event_payload["turn_count"])
        self.assertIsNotNone(event_payload["run_finished_at"])
        self.assertEqual(
            {"pending": 0, "complete": 1, "empty": 0, "unknown": 0},
            event_payload["counts"],
        )
        # Serialized event carries no embedded verification output.
        self.assertNotIn("x" * 8192, json.dumps(record))

    def test_run_finished_event_carries_observed_model_beside_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / "events.jsonl"
            payload = self._base_state_payload(Path(tmp) / "state.json")
            payload["events_path"] = str(event_path)
            payload["model"] = "native CLI default"
            payload["model_source"] = "unresolved"
            payload["observed_model"] = "claude-opus-5"
            payload["observed_model_source"] = "harness_reported"
            persistence = run_persistence._RunPersistence(write_state=False)
            commit_settlement._finish_run(
                event_path,
                payload,
                persistence=persistence,
                finalize_readiness=run_termination.readiness_finalizer(
                    readiness_gate.write_readiness_state
                ),
            )

            records = [
                json.loads(line)
                for line in event_path.read_text(encoding="utf-8").splitlines()
            ]

        finished = [r for r in records if r["name"] == "run_finished"]
        event_payload = finished[0]["payload"]
        self.assertEqual("native CLI default", event_payload["model"])
        self.assertEqual("unresolved", event_payload["model_source"])
        self.assertEqual("claude-opus-5", event_payload["observed_model"])
        self.assertEqual("harness_reported", event_payload["observed_model_source"])

    def _turn(
        self,
        *,
        harness: str = "claude",
        session_kind: str = "implementation",
        model: str = "native CLI default",
        model_source: str = "unresolved",
        observed_model: str | None = "claude-opus-5",
    ) -> dict[str, object]:
        return {
            "harness": harness,
            "session_kind": session_kind,
            "model": model,
            "model_source": model_source,
            "observed_model": observed_model,
        }

    def _fold(self, *turns: dict[str, object]) -> list[dict[str, Any]]:
        projection: list[dict[str, Any]] = []
        for turn in turns:
            projection = run_commit._merged_model_projection(projection, turn)
        return projection

    def test_an_unresolved_request_resolves_to_what_every_turn_reported(self) -> None:
        projection = self._fold(self._turn(), self._turn())

        self.assertEqual(1, len(projection))
        record = projection[0]
        self.assertEqual("claude-opus-5", record["model"])
        self.assertEqual("harness_reported", record["model_source"])
        # The request itself is preserved: resolving it is not rewriting it.
        self.assertEqual("native CLI default", record["requested_model"])
        self.assertEqual("unresolved", record["requested_model_source"])
        self.assertEqual(2, record["turns"])

    def test_a_requested_model_outranks_what_the_harness_reported(self) -> None:
        projection = self._fold(
            self._turn(model="claude-opus-4-7", model_source="flag")
        )

        self.assertEqual("claude-opus-4-7", projection[0]["model"])
        self.assertEqual("flag", projection[0]["model_source"])
        self.assertEqual(["claude-opus-5"], projection[0]["observed_models"])

    def test_disagreeing_observations_leave_the_request_standing(self) -> None:
        projection = self._fold(
            self._turn(), self._turn(observed_model="claude-opus-4-7")
        )

        # Two answers are not an answer, so the role keeps its unresolved request.
        self.assertEqual("native CLI default", projection[0]["model"])
        self.assertEqual("unresolved", projection[0]["model_source"])
        self.assertEqual(
            ["claude-opus-4-7", "claude-opus-5"], projection[0]["observed_models"]
        )

    def test_a_review_harness_keeps_its_own_record(self) -> None:
        projection = self._fold(
            self._turn(),
            self._turn(
                harness="codex",
                session_kind="review",
                observed_model="gpt-5-codex",
            ),
        )

        self.assertEqual(
            [
                ("implementation", "claude", "claude-opus-5"),
                ("review", "codex", "gpt-5-codex"),
            ],
            [
                (record["session_kind"], record["harness"], record["model"])
                for record in projection
            ],
        )

    def test_a_turn_that_reported_nothing_cannot_erase_an_observation(self) -> None:
        projection = self._fold(self._turn(), self._turn(observed_model=None))

        self.assertEqual("claude-opus-5", projection[0]["model"])
        self.assertEqual(2, projection[0]["turns"])

    def _live_action_required(self) -> dict[str, object]:
        return {
            "run_id": "run-1",
            "turn_index": 1,
            "session": 1,
            "reason": "runner-pause",
            "message": "RUNNER_PAUSE: blocked - needs a decision",
            "source": "stdout",
            "matched": events.RUNNER_PAUSE_PREFIX,
        }

    def test_action_required_clears_when_an_unpaused_run_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / "events.jsonl"
            payload = self._base_state_payload(Path(tmp) / "state.json")
            payload["events_path"] = str(event_path)
            payload["action_required"] = self._live_action_required()
            persistence = run_persistence._RunPersistence(write_state=False)

            commit_settlement._finish_run(
                event_path,
                payload,
                persistence=persistence,
                finalize_readiness=run_termination.readiness_finalizer(
                    readiness_gate.write_readiness_state
                ),
            )

        self.assertIsNone(payload["action_required"])

    def test_action_required_survives_a_run_that_finishes_paused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / "events.jsonl"
            payload = self._base_state_payload(Path(tmp) / "state.json")
            payload["events_path"] = str(event_path)
            payload["paused"] = True
            signal = self._live_action_required()
            payload["action_required"] = signal
            persistence = run_persistence._RunPersistence(write_state=False)

            commit_settlement._finish_run(
                event_path,
                payload,
                persistence=persistence,
                finalize_readiness=run_termination.readiness_finalizer(
                    readiness_gate.write_readiness_state
                ),
            )

        self.assertEqual(signal, payload["action_required"])

    def _baseline_missing_gate(self, plan_dir: Path, cwd: Path) -> dict[str, Any]:
        report = preflight.PreflightReport(
            plan_dir=plan_dir,
            findings=(
                preflight.PreflightFinding(
                    severity="red",
                    category="baseline-missing",
                    message="session 1 declares a baseline expectation",
                    session_id="1",
                    detail="no baseline evidence recorded",
                ),
            ),
        )
        return readiness_gate._readiness_gate_payload(report, cwd=cwd)

    def test_a_blocking_pause_names_the_command_that_clears_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            repo_root = Path(tmp) / "repo"
            payload = self._base_state_payload(Path(tmp) / "state.json")
            run_termination.set_pause(
                payload,
                reason="not-loop-ready",
                message="plan is not loop-ready",
                details=self._baseline_missing_gate(plan_dir, repo_root),
            )

        action = payload["action_required"]
        self.assertEqual("not-loop-ready", action["reason"])
        self.assertEqual("pause", action["source"])
        # A readiness gate is blocked on the plan and its environment, not on
        # an answer, so the ask is to inspect (see ASI-114 / policy v3).
        self.assertEqual(pause_policy.USER_ACTION_INSPECT, action["user_action"])
        hint = action["remediation"][0]
        self.assertEqual("baseline-missing.refresh", hint["check_id"])
        self.assertIn(
            f"`asl baseline {shlex.quote(str(plan_dir))} "
            f"--cwd {shlex.quote(str(repo_root))}`",
            hint["message"],
        )

    def test_a_nested_gate_payload_still_yields_its_remediation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            repo_root = Path(tmp) / "repo"
            payload = self._base_state_payload(Path(tmp) / "state.json")
            run_termination.set_pause(
                payload,
                reason=events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
                message="session 1 wrote outside its declared aliases",
                details={
                    "readiness_gate": self._baseline_missing_gate(plan_dir, repo_root),
                    "workspace_write_guard": {"violations": ["other-repo"]},
                },
            )

        remediation = payload["action_required"]["remediation"]
        self.assertEqual("baseline-missing.refresh", remediation[0]["check_id"])

    def test_a_pause_without_a_command_still_records_the_ask(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._base_state_payload(Path(tmp) / "state.json")
            run_termination.set_pause(
                payload,
                reason="blocked",
                message="RUNNER_PAUSE: blocked - needs a decision",
            )

        self.assertEqual("blocked", payload["action_required"]["reason"])
        self.assertEqual([], payload["action_required"]["remediation"])

    def test_a_retryable_pause_records_no_ask(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._base_state_payload(Path(tmp) / "state.json")
            run_termination.set_pause(
                payload,
                reason="verify-failed",
                message="verification failed",
            )

        self.assertIsNone(payload.get("action_required"))

    def test_a_live_signal_outranks_the_pause_derived_ask(self) -> None:
        # The pause names the same ask the turn already raised, so the turn's
        # record — the more specific of the two — stands. A pause the runner
        # declared instead retires it; see
        # ``run_termination._supersede_live_action_signal`` and ASI-114.
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._base_state_payload(Path(tmp) / "state.json")
            signal = self._live_action_required()
            payload["action_required"] = signal
            run_termination.set_pause(
                payload,
                reason="runner-pause",
                message="RUNNER_PAUSE: blocked - needs a decision",
            )

        self.assertEqual(signal, payload["action_required"])
        self.assertNotIn(events.ACTION_REQUIRED_SUPERSEDED_KEY, payload)

    def test_unresolved_review_findings_survive_an_unpaused_finish(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / "events.jsonl"
            payload = self._base_state_payload(Path(tmp) / "state.json")
            payload["events_path"] = str(event_path)
            payload["review_fix_disposition"] = "exhausted"
            payload["review_verdict"] = {"outcome": "changes-requested"}
            persistence = run_persistence._RunPersistence(write_state=False)

            reason = review_cycle._terminal_review_reason(payload)
            commit_settlement._finish_run(
                event_path,
                payload,
                persistence=persistence,
                finalize_readiness=run_termination.readiness_finalizer(
                    readiness_gate.write_readiness_state
                ),
            )

        self.assertEqual(review_transitions.REVIEW_FINDINGS_UNRESOLVED_REASON, reason)
        self.assertEqual(
            review_transitions.REVIEW_FINDINGS_UNRESOLVED_REASON,
            payload["action_required"]["reason"],
        )

    def test_run_paused_event_and_state_bound_verification_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            event_path = Path(tmp) / "events.jsonl"
            payload = self._base_state_payload(state_path)
            persistence = run_persistence._RunPersistence()
            details = {
                "session": "1",
                "recipe": {"check_count": 1, "kinds": ["command"], "names": ["tests"]},
                "passed": False,
                "checks": [self._verification_check()],
                "failures": [self._verification_check()],
            }
            run_termination._pause_run(
                event_path,
                payload,
                reason="verify-failed",
                message="verification failed",
                details=details,
                persistence=persistence,
            )
            run_persistence._write_state_payload(payload)

            records = [
                json.loads(line)
                for line in event_path.read_text(encoding="utf-8").splitlines()
            ]
            persisted = json.loads(state_path.read_text(encoding="utf-8"))

        paused = [r for r in records if r["name"] == events.RUN_PAUSED_EVENT][0]
        event_check = paused["payload"]["details"]["checks"][0]
        self.assertNotIn("stdout", event_check)
        self.assertEqual(
            "/artifacts/tests-stdout.gz", event_check["stdout_artifact_path"]
        )
        event_failure = paused["payload"]["details"]["failures"][0]
        self.assertNotIn("stderr", event_failure)
        # Persisted state pause details are likewise bounded.
        state_check = persisted["pause_details"]["checks"][0]
        self.assertNotIn("stdout", state_check)
        self.assertEqual("command-failed", state_check["failure_reason"])
        # In-memory pause details retain full output for the run response JSON.
        self.assertIn("stdout", payload["pause_details"]["checks"][0])

    def test_v1_state_without_version_key_remains_readable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            plan_state = state.plan_state_dir(plan_dir, state_dir)
            plan_state.mkdir(parents=True, exist_ok=True)
            state_path = plan_state / events.STATE_FILE_NAME
            # A pre-v2 state.json carries no ``version`` key.
            state_path.write_text(
                json.dumps(
                    {
                        "run_id": "legacy-run",
                        "harness": "codex",
                        "ok": True,
                        "paused": False,
                        "reason": "",
                        "run_started_at": "2026-06-20T10:00:00+00:00",
                        "run_finished_at": None,
                        "duration_seconds": None,
                        "active_turn": {"session": 1, "turn_index": 1},
                        "active_verification": None,
                    }
                ),
                encoding="utf-8",
            )

            snapshot = plans.inspect_plan(plan_dir)
            # A run with a turn in flight holds its lock; without one the
            # status backfill would (correctly) declare the run abandoned.
            state.acquire_lock(plan_dir, state_dir)
            try:
                payload = state.status_payload(snapshot, state_dir)
            finally:
                state.release_lock(plan_dir, state_dir)

        self.assertEqual("legacy-run", payload["runner_state"]["run_id"])
        self.assertEqual(1, payload["active_turn"]["session"])


# Takes the abandonment claim in a genuinely separate process, announces it,
# then holds it until stdin closes — so the exclusion under test is the
# cross-process one, not an in-process bookkeeping flag.
_HOLD_ABANDON_CLAIM_SOURCE = """
import fcntl, os, sys

fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o644)
fcntl.flock(fd, fcntl.LOCK_EX)
sys.stdout.write("held\\n")
sys.stdout.flush()
sys.stdin.readline()
"""


class MarkRunAbandonedTests(unittest.TestCase):
    """Proof-of-death guards around the synthetic terminal run record."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.plan_dir = root / "plan"
        self.state_dir = root / "state"
        write_plan(self.plan_dir, ("pending",))
        self.plan_state = state.plan_state_dir(self.plan_dir, self.state_dir)
        self.plan_state.mkdir(parents=True)
        self.state_path = self.plan_state / events.STATE_FILE_NAME
        self.events_path = self.plan_state / events.EVENTS_FILE_NAME
        self.lock_path = self.plan_state / state.LOCK_FILE_NAME

    def dead_pid(self) -> int:
        """A pid that has exited and been reaped, so it is provably gone."""
        process = subprocess.Popen([sys.executable, "-c", ""])
        process.wait()
        return process.pid

    def write_unfinished_state(
        self,
        *,
        run_id: str = "20260728T123131255926Z-77262",
        run_started_at: str = "2026-06-19T12:00:00+00:00",
        assurance: dict[str, object] | None = None,
        run_config: dict[str, object] | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "ok": True,
            "paused": False,
            "run_id": run_id,
            "harness": "claude",
            "reason": "",
            "run_started_at": run_started_at,
            "run_finished_at": None,
            "duration_seconds": None,
            "active_turn": {"session": 1, "turn_index": 1},
            "active_verification": {"session": 1, "turn_index": 1},
        }
        if assurance is not None:
            payload["assurance"] = assurance
        if run_config is not None:
            # Shaped like `run_payloads._initial_payload`: the plan directory
            # is a top-level field and the workspace binding lives in
            # `run_config`.
            payload["plan_dir"] = str(plans.canonicalize_path(self.plan_dir))
            payload["run_config"] = {
                "workspace_root": str(plans.canonicalize_path(self.plan_dir)),
                **run_config,
            }
        self.state_path.write_text(json.dumps(payload), encoding="utf-8")

    def write_lock(self, *, pid: int, hostname: str | None) -> None:
        payload: dict[str, object] = {
            "version": 1,
            "owner": "agent-session-loop",
            "pid": pid,
            "plan_dir": str(plans.canonicalize_path(self.plan_dir)),
            "created_at": "2026-06-19T12:00:00+00:00",
        }
        if hostname is not None:
            payload["hostname"] = hostname
        self.lock_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

    def read_lock_info(self) -> state.LockInfo:
        lock_info = state.read_lock_info(self.plan_dir, self.state_dir)
        assert lock_info is not None
        return lock_info

    def persisted_state(self) -> dict[str, object]:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def assertUntouched(self) -> None:
        persisted = self.persisted_state()
        self.assertIsNone(persisted["run_finished_at"])
        self.assertNotIn("abandoned", persisted)
        self.assertFalse(self.events_path.exists())

    def test_dead_pid_lock_marks_abandoned_exactly_once(self) -> None:
        pid = self.dead_pid()
        self.write_unfinished_state()
        self.write_lock(pid=pid, hostname=socket.gethostname())
        lock_info = self.read_lock_info()
        self.assertTrue(lock_info.stale)

        self.assertTrue(
            state.mark_run_abandoned(
                self.plan_state, lock_info=lock_info, trigger="test"
            )
        )

        persisted = self.persisted_state()
        self.assertFalse(persisted["ok"])
        self.assertEqual(state.ABANDONED_REASON, persisted["reason"])
        self.assertIsNotNone(persisted["run_finished_at"])
        self.assertIsNone(persisted["active_turn"])
        self.assertIsNone(persisted["active_verification"])
        self.assertEqual("test", persisted["abandoned"]["trigger"])
        self.assertEqual(os.getpid(), persisted["abandoned"]["detected_by_pid"])
        self.assertEqual(
            f"pid {pid} is not running", persisted["abandoned"]["lock_reason"]
        )

        records = read_jsonl(self.events_path)
        self.assertEqual([events.RUN_FINISHED_EVENT], [r["name"] for r in records])
        self.assertEqual(events.EVENT_SCHEMA_VERSION, records[0]["version"])
        self.assertTrue(records[0]["payload"]["synthetic"])
        self.assertEqual(events.RUN_OUTCOME_ABANDONED, records[0]["payload"]["outcome"])
        self.assertEqual(
            persisted["run_finished_at"], records[0]["payload"]["run_finished_at"]
        )

        self.assertFalse(
            state.mark_run_abandoned(
                self.plan_state, lock_info=lock_info, trigger="test"
            )
        )
        self.assertEqual(1, len(read_jsonl(self.events_path)))

    def test_abandonment_records_the_reason_it_overwrote(self) -> None:
        # A runner killed while it finalized an operator's stop had already
        # recorded that stop; without keeping it, reclaiming the run erases the
        # only evidence that the ending was asked for.
        self.write_unfinished_state()
        persisted = self.persisted_state()
        persisted["reason"] = "stopped"
        persisted["active_finalization"] = {"phase": "assurance-audit"}
        self.state_path.write_text(json.dumps(persisted), encoding="utf-8")
        self.write_lock(pid=self.dead_pid(), hostname=socket.gethostname())

        self.assertTrue(
            state.mark_run_abandoned(
                self.plan_state, lock_info=self.read_lock_info(), trigger="test"
            )
        )

        reclaimed = self.persisted_state()
        self.assertEqual(state.ABANDONED_REASON, reclaimed["reason"])
        self.assertEqual("stopped", reclaimed["abandoned"]["prior_reason"])
        self.assertIsNone(reclaimed["active_finalization"])

    def test_concurrent_abandonment_appends_exactly_one_terminal_event(self) -> None:
        self.write_unfinished_state()
        self.write_lock(pid=self.dead_pid(), hostname=socket.gethostname())
        lock_info = self.read_lock_info()

        # Releasing both callers together at the end of the proof check puts
        # each one step from the recheck, which is the interleaving that used
        # to let both of them publish a terminal record.
        barrier = threading.Barrier(2)
        real_is_proven = state._abandonment_is_proven

        def barriered_proof(*args: object, **kwargs: object) -> bool:
            proven = real_is_proven(*args, **kwargs)
            barrier.wait(timeout=30)
            return proven

        results: list[bool] = []
        results_lock = threading.Lock()

        def probe() -> None:
            marked = state.mark_run_abandoned(
                self.plan_state, lock_info=lock_info, trigger="test"
            )
            with results_lock:
                results.append(marked)

        with mock.patch.object(state, "_abandonment_is_proven", barriered_proof):
            threads = [threading.Thread(target=probe) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=60)
                self.assertFalse(thread.is_alive())

        self.assertEqual([False, True], sorted(results))
        persisted = self.persisted_state()
        self.assertEqual(state.ABANDONED_REASON, persisted["reason"])
        records = read_jsonl(self.events_path)
        self.assertEqual([events.RUN_FINISHED_EVENT], [r["name"] for r in records])
        # The loser wrote neither a second event nor a competing timestamp.
        self.assertEqual(
            persisted["run_finished_at"], records[0]["payload"]["run_finished_at"]
        )
        self.assertEqual(
            persisted["run_finished_at"], persisted["abandoned"]["detected_at"]
        )

    def test_abandonment_declines_while_another_process_holds_the_claim(self) -> None:
        self.write_unfinished_state()
        self.write_lock(pid=self.dead_pid(), hostname=socket.gethostname())
        lock_info = self.read_lock_info()
        claim_path = self.plan_state / state.ABANDON_CLAIM_FILE_NAME

        holder = subprocess.Popen(
            [sys.executable, "-c", _HOLD_ABANDON_CLAIM_SOURCE, str(claim_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        assert holder.stdin is not None and holder.stdout is not None
        self.addCleanup(holder.stdout.close)
        self.addCleanup(holder.stdin.close)
        self.assertEqual("held\n", holder.stdout.readline())

        self.assertFalse(
            state.mark_run_abandoned(
                self.plan_state, lock_info=lock_info, trigger="test"
            )
        )
        self.assertUntouched()

        holder.stdin.close()
        self.assertEqual(0, holder.wait(timeout=30))

        self.assertTrue(
            state.mark_run_abandoned(
                self.plan_state, lock_info=lock_info, trigger="test"
            )
        )
        self.assertEqual(1, len(read_jsonl(self.events_path)))

    def test_active_lock_never_marks_abandoned(self) -> None:
        self.write_unfinished_state()
        self.write_lock(pid=os.getpid(), hostname=socket.gethostname())
        lock_info = self.read_lock_info()
        self.assertTrue(lock_info.active)

        self.assertFalse(
            state.mark_run_abandoned(
                self.plan_state, lock_info=lock_info, trigger="test"
            )
        )
        self.assertUntouched()

    def test_foreign_hostname_never_marks_abandoned(self) -> None:
        # The pid is dead in *this* host's process table, which says nothing
        # about the host that actually recorded it.
        self.write_unfinished_state()
        self.write_lock(pid=self.dead_pid(), hostname="some-other-host")

        self.assertFalse(
            state.mark_run_abandoned(
                self.plan_state, lock_info=self.read_lock_info(), trigger="test"
            )
        )
        self.assertUntouched()

    def test_ambiguous_stale_lock_without_pid_never_marks_abandoned(self) -> None:
        self.write_unfinished_state()
        self.lock_path.write_text("not json", encoding="utf-8")
        lock_info = self.read_lock_info()
        self.assertTrue(lock_info.stale)
        self.assertIsNone(lock_info.pid)

        self.assertFalse(
            state.mark_run_abandoned(
                self.plan_state, lock_info=lock_info, trigger="test"
            )
        )
        self.assertUntouched()

    def test_missing_lock_marks_abandoned_only_after_the_grace_window(self) -> None:
        self.write_unfinished_state(run_started_at=datetime.now(UTC).isoformat())
        self.assertFalse(
            state.mark_run_abandoned(self.plan_state, lock_info=None, trigger="test")
        )
        self.assertUntouched()

        self.write_unfinished_state()
        self.assertTrue(
            state.mark_run_abandoned(self.plan_state, lock_info=None, trigger="test")
        )
        self.assertIsNone(self.persisted_state()["abandoned"]["lock_reason"])

    def test_missing_lock_defers_to_a_lock_that_reappeared(self) -> None:
        self.write_unfinished_state()
        self.write_lock(pid=os.getpid(), hostname=socket.gethostname())

        self.assertFalse(
            state.mark_run_abandoned(self.plan_state, lock_info=None, trigger="test")
        )
        self.assertUntouched()

    def test_finished_run_is_never_marked(self) -> None:
        self.state_path.write_text(
            json.dumps(
                {
                    "ok": True,
                    "run_id": "finished-run",
                    "run_started_at": "2026-06-19T12:00:00+00:00",
                    "run_finished_at": "2026-06-19T12:30:00+00:00",
                }
            ),
            encoding="utf-8",
        )

        self.assertFalse(
            state.mark_run_abandoned(self.plan_state, lock_info=None, trigger="test")
        )
        self.assertFalse(self.events_path.exists())

    def test_existing_events_are_preserved_as_a_prefix(self) -> None:
        events.append_event(
            self.events_path,
            events.Event(name="turn_started", payload={"run_id": "run-dead"}),
        )
        before = self.events_path.read_bytes()
        self.write_unfinished_state(run_id="run-dead")

        self.assertTrue(
            state.mark_run_abandoned(self.plan_state, lock_info=None, trigger="test")
        )

        after = self.events_path.read_bytes()
        self.assertTrue(after.startswith(before))
        self.assertEqual(2, len(read_jsonl(self.events_path)))

    @unittest.skipIf(os.geteuid() == 0, "root ignores file permissions")
    def test_read_only_events_file_is_tolerated(self) -> None:
        self.write_unfinished_state()
        self.events_path.write_text("", encoding="utf-8")
        self.events_path.chmod(0o444)
        self.addCleanup(self.events_path.chmod, 0o644)

        # The terminal state is durable even when the event cannot be appended.
        self.assertTrue(
            state.mark_run_abandoned(self.plan_state, lock_info=None, trigger="test")
        )
        self.assertEqual(state.ABANDONED_REASON, self.persisted_state()["reason"])
        self.assertEqual("", self.events_path.read_text(encoding="utf-8"))

    def test_acquire_lock_takeover_marks_abandoned_and_records_hostname(self) -> None:
        self.write_unfinished_state(run_id="run-dead")
        self.write_lock(pid=self.dead_pid(), hostname=socket.gethostname())

        lock_info = state.acquire_lock(self.plan_dir, self.state_dir)
        self.addCleanup(state.release_lock, self.plan_dir, self.state_dir)

        self.assertEqual(os.getpid(), lock_info.pid)
        self.assertEqual(socket.gethostname(), lock_info.raw["hostname"])
        self.assertEqual(4, lock_info.raw["version"])

        persisted = self.persisted_state()
        self.assertEqual(state.ABANDONED_REASON, persisted["reason"])
        self.assertEqual(
            state.ABANDONED_TRIGGER_LOCK_TAKEOVER,
            persisted["abandoned"]["trigger"],
        )
        records = read_jsonl(self.events_path)
        self.assertEqual(1, len(records))
        self.assertEqual("run-dead", records[0]["payload"]["run_id"])

    def test_acquire_lock_takeover_reclaims_legacy_hostname_less_lock(self) -> None:
        # Locks predating the hostname field are the only route back for the
        # zombies that already exist on disk.
        self.write_unfinished_state(run_id="run-legacy")
        self.write_lock(pid=self.dead_pid(), hostname=None)

        state.acquire_lock(self.plan_dir, self.state_dir)
        self.addCleanup(state.release_lock, self.plan_dir, self.state_dir)

        self.assertEqual(state.ABANDONED_REASON, self.persisted_state()["reason"])
        records = read_jsonl(self.events_path)
        self.assertEqual(["run-legacy"], [r["payload"]["run_id"] for r in records])

    def test_acquire_lock_foreign_hostname_never_marks_abandoned(self) -> None:
        self.write_unfinished_state(run_id="run-elsewhere")
        self.write_lock(pid=self.dead_pid(), hostname="some-other-host")
        # Dead in *this* host's process table, which is not the table that
        # owns the pid.
        self.assertNotIn("is not running", self.read_lock_info().reason)
        lock_bytes = self.lock_path.read_bytes()

        with self.assertRaises(state.StateError) as raised:
            state.acquire_lock(self.plan_dir, self.state_dir)

        message = str(raised.exception)
        # The prefix is what `run_outcome._state_error_reason` reads as
        # lock-contention rather than an unrecoverable state error.
        self.assertIn("plan is locked", message)
        self.assertIn("some-other-host", message)
        self.assertEqual(lock_bytes, self.lock_path.read_bytes())
        self.assertUntouched()

    def test_acquire_lock_reclaims_pidless_lock_without_marking_abandoned(self) -> None:
        # An unparseable lock is garbage worth clearing, but it names no pid,
        # so it says nothing about whether the run that wrote it still lives.
        self.write_unfinished_state()
        self.lock_path.write_text("not json", encoding="utf-8")

        lock_info = state.acquire_lock(self.plan_dir, self.state_dir)
        self.addCleanup(state.release_lock, self.plan_dir, self.state_dir)

        self.assertEqual(os.getpid(), lock_info.pid)
        self.assertUntouched()

    def test_acquire_lock_without_takeover_leaves_unfinished_state_alone(self) -> None:
        # A fresh acquire and a post-takeover acquire share the same return
        # path, so only a stolen lock with a proven-dead owner can terminalize.
        self.write_unfinished_state(
            run_id="run-booting",
            run_started_at=datetime.now(UTC).isoformat(),
        )

        state.acquire_lock(self.plan_dir, self.state_dir)
        self.addCleanup(state.release_lock, self.plan_dir, self.state_dir)

        self.assertUntouched()

    def pending_record(self, run_id: str) -> dict[str, object]:
        path = (
            self.plan_state
            / state.RUNS_DIR_NAME
            / run_id
            / state.ASSURANCE_PENDING_FILE_NAME
        )
        return json.loads(path.read_text(encoding="utf-8"))

    def test_abandonment_records_the_audit_the_dead_run_still_owed(self) -> None:
        self.write_unfinished_state(
            run_id="run-owing",
            assurance={"run_id": "run-owing", "state": "pending", "eligible": True},
            run_config={"harness": "claude", "audit_harness": "codex"},
        )

        self.assertTrue(
            state.mark_run_abandoned(self.plan_state, lock_info=None, trigger="test")
        )

        record = self.pending_record("run-owing")
        self.assertEqual(
            state.ASSURANCE_PENDING_SCHEMA_VERSION, record["schema_version"]
        )
        self.assertEqual("run-owing", record["run_id"])
        self.assertIsNone(record["skip"])
        self.assertEqual(events.RUN_OUTCOME_ABANDONED, record["outcome"])
        # The record carries its own copy of what the audit needs, because the
        # next run of this plan overwrites the `state.json` it came from.
        self.assertEqual("codex", record["run_config"]["audit_harness"])
        self.assertEqual(
            str(plans.canonicalize_path(self.plan_dir)), record["plan_dir"]
        )
        self.assertEqual(
            str(plans.canonicalize_path(self.plan_dir)), record["workspace_root"]
        )

        persisted = self.persisted_state()
        self.assertEqual("run-owing", persisted["assurance_pending"]["run_id"])
        self.assertEqual(
            persisted["run_finished_at"],
            persisted["assurance_pending"]["recorded_at"],
        )
        records = read_jsonl(self.events_path)
        self.assertEqual(
            persisted["assurance_pending"],
            records[0]["payload"]["assurance_pending"],
        )

    def test_run_that_predates_auditing_is_recorded_as_such(self) -> None:
        self.write_unfinished_state(run_id="run-old")

        self.assertTrue(
            state.mark_run_abandoned(self.plan_state, lock_info=None, trigger="test")
        )

        self.assertEqual(
            state.ASSURANCE_SKIP_PREDATES, self.pending_record("run-old")["skip"]
        )

    def test_settled_assurance_is_recorded_as_owing_nothing(self) -> None:
        for label, assurance in (
            ("verdict-reached", {"state": "all-clear", "eligible": True}),
            ("opted-out", {"state": "pending", "eligible": False}),
        ):
            with self.subTest(label):
                self.write_unfinished_state(run_id=label, assurance=assurance)

                self.assertTrue(
                    state.mark_run_abandoned(
                        self.plan_state, lock_info=None, trigger="test"
                    )
                )

                self.assertEqual(
                    state.ASSURANCE_SKIP_SETTLED, self.pending_record(label)["skip"]
                )

    def test_abandonment_records_the_debt_without_launching_a_harness(self) -> None:
        # `asl status` and lock acquisition both reach this code, and neither
        # may block on a harness turn.
        self.write_unfinished_state(
            run_id="run-quiet",
            assurance={"state": "pending", "eligible": True},
            run_config={"harness": "claude", "audit_harness": "codex"},
        )

        with mock.patch.object(
            harnesses,
            "run_harness",
            side_effect=AssertionError("abandonment launched a harness"),
        ):
            self.assertTrue(
                state.mark_run_abandoned(
                    self.plan_state, lock_info=None, trigger="test"
                )
            )

        self.assertIsNone(self.pending_record("run-quiet")["skip"])

    def test_unwritable_pending_record_never_blocks_terminalization(self) -> None:
        self.write_unfinished_state(run_id="run-blocked")
        runs_dir = self.plan_state / state.RUNS_DIR_NAME
        runs_dir.mkdir(parents=True)
        (runs_dir / "run-blocked").write_text("not a directory", encoding="utf-8")

        self.assertTrue(
            state.mark_run_abandoned(self.plan_state, lock_info=None, trigger="test")
        )

        persisted = self.persisted_state()
        self.assertEqual(state.ABANDONED_REASON, persisted["reason"])
        self.assertNotIn("assurance_pending", persisted)
        self.assertEqual(1, len(read_jsonl(self.events_path)))


class LockRepositoryClaimTests(unittest.TestCase):
    """The registry a lock publishes, and the window in which it publishes it."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.plan_dir = root / "plan"
        self.state_dir = root / "state"
        write_plan(self.plan_dir, ("pending",))
        self.lock_path = state.lock_path_for_plan(self.plan_dir, self.state_dir)

    def read_payload(self) -> dict[str, object]:
        return json.loads(self.lock_path.read_text(encoding="utf-8"))

    def test_lock_publishes_a_current_version_sorted_repository_claim(
        self,
    ) -> None:
        lock_info = state.acquire_lock(
            self.plan_dir,
            self.state_dir,
            repository_roots=("/w/zebra", "/w/apple", "/w/zebra"),
        )
        self.addCleanup(state.release_lock, self.plan_dir, self.state_dir)

        payload = self.read_payload()
        # 3 adds `wakeup_endpoint` to 2's repository claim; 4 adds
        # `redirect_endpoint` beside it. Both fields are informational —
        # readers never validate them — so the bumps document the shape rather
        # than gating anything.
        self.assertEqual(4, payload["version"])
        self.assertIsNone(payload["redirect_endpoint"])
        self.assertEqual(["/w/apple", "/w/zebra"], payload["repositories"])
        self.assertEqual(("/w/apple", "/w/zebra"), lock_info.repository_roots)

    def test_lock_without_a_claim_publishes_an_empty_registry(self) -> None:
        lock_info = state.acquire_lock(self.plan_dir, self.state_dir)
        self.addCleanup(state.release_lock, self.plan_dir, self.state_dir)

        self.assertEqual([], self.read_payload()["repositories"])
        self.assertEqual((), lock_info.repository_roots)

    def test_version_one_lock_declares_no_repositories(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "owner": "agent-session-loop",
                    "pid": os.getpid(),
                    "hostname": socket.gethostname(),
                    "plan_dir": str(plans.canonicalize_path(self.plan_dir)),
                    "created_at": "2026-06-19T12:00:00+00:00",
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )

        lock_info = state.read_lock_info(self.plan_dir, self.state_dir)

        assert lock_info is not None
        self.assertTrue(lock_info.active)
        self.assertEqual((), lock_info.repository_roots)

    def test_a_malformed_repositories_value_reads_as_no_claim(self) -> None:
        raw = {"version": 2, "repositories": {"a": "/w/apple"}}
        lock_info = state.LockInfo(
            path=self.lock_path,
            pid=os.getpid(),
            plan_dir=str(self.plan_dir),
            created_at="2026-06-19T12:00:00+00:00",
            stale=False,
            reason="active",
            raw=raw,
        )

        self.assertEqual((), lock_info.repository_roots)

    def test_a_reader_never_observes_a_partial_lock(self) -> None:
        observed: list[state.LockInfo | None] = []
        real_replace = os.replace

        def observing_replace(src: str | Path, dst: str | Path) -> None:
            # Whatever a concurrent reader sees at the lock path, it sees it
            # here: everything before this call happened on the sibling.
            observed.append(state.read_lock_info(self.plan_dir, self.state_dir))
            real_replace(src, dst)

        with mock.patch.object(state.os, "replace", observing_replace):
            state.acquire_lock(
                self.plan_dir, self.state_dir, repository_roots=("/w/apple",)
            )
        self.addCleanup(state.release_lock, self.plan_dir, self.state_dir)

        self.assertEqual([None], observed)
        self.assertEqual(["/w/apple"], self.read_payload()["repositories"])
        self.assertEqual(
            [state.LOCK_FILE_NAME, state.LOCK_MUTEX_FILE_NAME],
            sorted(path.name for path in self.lock_path.parent.iterdir()),
        )

    def test_a_failed_payload_write_leaves_no_lock_at_the_lock_path(self) -> None:
        with (
            mock.patch.object(
                state.json, "dump", side_effect=RuntimeError("disk gave up")
            ),
            self.assertRaises(RuntimeError),
        ):
            state.acquire_lock(self.plan_dir, self.state_dir)

        self.assertFalse(self.lock_path.exists())
        self.assertEqual(
            [state.LOCK_MUTEX_FILE_NAME],
            sorted(path.name for path in self.lock_path.parent.iterdir()),
        )


def _plan_state_dir(state_root: Path, plan_dir: Path) -> Path:
    entry = state_root / plans.path_sha256(plan_dir)
    entry.mkdir(parents=True, exist_ok=True)
    return entry


def _write_state_json_entry(state_root: Path, plan_dir: Path) -> Path:
    entry = _plan_state_dir(state_root, plan_dir)
    (entry / events.STATE_FILE_NAME).write_text(
        json.dumps({"plan_dir": str(plan_dir)}, sort_keys=True),
        encoding="utf-8",
    )
    return entry


def _write_lock_json_entry(state_root: Path, plan_dir: Path) -> Path:
    entry = _plan_state_dir(state_root, plan_dir)
    (entry / state.LOCK_FILE_NAME).write_text(
        json.dumps(
            {"plan_dir": str(plan_dir), "pid": 1, "created_at": "x"},
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return entry


def _write_result_json_entry(
    state_root: Path,
    plan_dir: Path,
    *,
    section: str = "after",
    run_id: str = "run-1",
    turn_label: str = "1",
) -> Path:
    entry = _plan_state_dir(state_root, plan_dir)
    turn_dir = (
        entry / state.RUNS_DIR_NAME / run_id / f"{state.TURN_DIR_PREFIX}{turn_label}"
    )
    turn_dir.mkdir(parents=True, exist_ok=True)
    (turn_dir / state.RESULT_FILE_NAME).write_text(
        json.dumps({section: {"plan_dir": str(plan_dir)}}, sort_keys=True),
        encoding="utf-8",
    )
    return entry


class ResolvePlanReferenceTests(unittest.TestCase):
    """Focused tests for the read-only state-corpus plan-label resolver."""

    def test_existing_direct_directory_wins_without_state_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_dir = root / "compact-label"
            plan_dir.mkdir()
            # A non-directory state root would raise if scanned; the existing
            # directory must short-circuit before any lookup happens.
            not_a_dir = root / "state-file"
            not_a_dir.write_text("not a dir", encoding="utf-8")

            resolved = state.resolve_plan_reference(str(plan_dir), not_a_dir)

            self.assertEqual(plans.canonicalize_path(plan_dir), resolved)

    def test_unique_match_from_state_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_root = root / "state"
            plan_dir = plans.canonicalize_path(root / "plans" / "abc123")
            plan_dir.mkdir(parents=True)
            _write_state_json_entry(state_root, plan_dir)

            resolved = state.resolve_plan_reference("abc123", state_root)

            self.assertEqual(plan_dir, resolved)

    def test_unique_match_from_lock_only_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_root = root / "state"
            plan_dir = plans.canonicalize_path(root / "plans" / "lock-label")
            plan_dir.mkdir(parents=True)
            _write_lock_json_entry(state_root, plan_dir)

            resolved = state.resolve_plan_reference("lock-label", state_root)

            self.assertEqual(plan_dir, resolved)

    def test_unique_match_from_result_only_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_root = root / "state"
            plan_dir = plans.canonicalize_path(root / "plans" / "res-label")
            plan_dir.mkdir(parents=True)
            _write_result_json_entry(state_root, plan_dir, section="after")

            resolved = state.resolve_plan_reference("res-label", state_root)

            self.assertEqual(plan_dir, resolved)

    def test_result_before_plan_dir_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_root = root / "state"
            plan_dir = plans.canonicalize_path(root / "plans" / "before-label")
            plan_dir.mkdir(parents=True)
            _write_result_json_entry(state_root, plan_dir, section="before")

            resolved = state.resolve_plan_reference("before-label", state_root)

            self.assertEqual(plan_dir, resolved)

    def test_candidate_deduplication_across_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_root = root / "state"
            plan_dir = plans.canonicalize_path(root / "plans" / "dedup")
            plan_dir.mkdir(parents=True)
            # Same plan referenced by all three sources in one state dir.
            _write_state_json_entry(state_root, plan_dir)
            _write_lock_json_entry(state_root, plan_dir)
            _write_result_json_entry(state_root, plan_dir)

            resolved = state.resolve_plan_reference("dedup", state_root)

            self.assertEqual(plan_dir, resolved)

    def test_case_sensitive_basename_matching_misses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_root = root / "state"
            plan_dir = plans.canonicalize_path(root / "plans" / "Alpha")
            plan_dir.mkdir(parents=True)
            _write_state_json_entry(state_root, plan_dir)

            resolved = state.resolve_plan_reference("alpha", state_root)

            # No case-insensitive match: falls back to the canonical argument.
            self.assertEqual(plans.canonicalize_path("alpha"), resolved)

    def test_path_like_nonexistent_argument_bypasses_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_root = root / "state"
            plan_dir = plans.canonicalize_path(root / "plans" / "bar")
            plan_dir.mkdir(parents=True)
            _write_state_json_entry(state_root, plan_dir)

            resolved = state.resolve_plan_reference("foo/bar", state_root)

            # Directory component present: keep path-only behavior.
            self.assertEqual(plans.canonicalize_path("foo/bar"), resolved)

    def test_missing_state_root_retains_original(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "absent"

            resolved = state.resolve_plan_reference("whatever", state_root)

            self.assertEqual(plans.canonicalize_path("whatever"), resolved)

    def test_unknown_label_retains_original(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_root = root / "state"
            plan_dir = plans.canonicalize_path(root / "plans" / "known")
            plan_dir.mkdir(parents=True)
            _write_state_json_entry(state_root, plan_dir)

            resolved = state.resolve_plan_reference("missing", state_root)

            self.assertEqual(plans.canonicalize_path("missing"), resolved)

    def test_nonexistent_candidate_plan_dir_is_discarded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_root = root / "state"
            plan_dir = root / "plans" / "gone"
            # Register metadata but never create the plan directory itself.
            _write_state_json_entry(state_root, plans.canonicalize_path(plan_dir))

            resolved = state.resolve_plan_reference("gone", state_root)

            self.assertEqual(plans.canonicalize_path("gone"), resolved)

    def test_ambiguous_matches_raise_sorted_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_root = root / "state"
            first = plans.canonicalize_path(root / "b-parent" / "dup")
            second = plans.canonicalize_path(root / "a-parent" / "dup")
            first.mkdir(parents=True)
            second.mkdir(parents=True)
            _write_state_json_entry(state_root, first)
            _write_lock_json_entry(state_root, second)

            with self.assertRaises(state.AmbiguousPlanError) as caught:
                state.resolve_plan_reference("dup", state_root)

            error = caught.exception
            self.assertIsInstance(error, state.StateError)
            self.assertEqual("dup", error.label)
            self.assertEqual(state_root, error.state_root)
            self.assertEqual(sorted([first, second], key=str), error.candidates)

    def test_malformed_individual_record_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_root = root / "state"
            plan_dir = plans.canonicalize_path(root / "plans" / "good")
            plan_dir.mkdir(parents=True)
            # A malformed state.json in the same entry must not block the
            # valid result.json fallback.
            entry = _plan_state_dir(state_root, plan_dir)
            (entry / events.STATE_FILE_NAME).write_text("{ not json", encoding="utf-8")
            _write_result_json_entry(state_root, plan_dir)

            resolved = state.resolve_plan_reference("good", state_root)

            self.assertEqual(plan_dir, resolved)

    def test_non_directory_state_root_raises_state_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "root-file"
            state_root.write_text("not a directory", encoding="utf-8")

            with self.assertRaises(state.StateError):
                state.resolve_plan_reference("label", state_root)

    def test_unenumerable_state_root_raises_state_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            state_root.mkdir()

            with (
                mock.patch.object(Path, "iterdir", side_effect=OSError("boom")),
                self.assertRaises(state.StateError),
            ):
                state.resolve_plan_reference("label", state_root)


class HarnessSkillInvocationTests(unittest.TestCase):
    def test_skill_prefixes_derive_from_adapter_classes(self) -> None:
        self.assertEqual(
            {
                harnesses.CodexAdapter.name: harnesses.CodexAdapter.skill_prefix,
                harnesses.ClaudeAdapter.name: harnesses.ClaudeAdapter.skill_prefix,
                harnesses.CopilotAdapter.name: harnesses.CopilotAdapter.skill_prefix,
            },
            harnesses.SKILL_PREFIXES,
        )
        self.assertEqual("$", harnesses.SKILL_PREFIXES["codex"])
        self.assertEqual("/", harnesses.SKILL_PREFIXES["claude"])
        self.assertEqual("/", harnesses.SKILL_PREFIXES["copilot"])

    def test_detect_invoking_harness_matches_single_marker(self) -> None:
        self.assertEqual(
            "claude", harnesses.detect_invoking_harness({"CLAUDECODE": "1"})
        )
        self.assertEqual(
            "codex", harnesses.detect_invoking_harness({"CODEX_SANDBOX": "1"})
        )
        self.assertEqual(
            "copilot",
            harnesses.detect_invoking_harness({"COPILOT_AGENT": "1"}),
        )
        self.assertEqual(
            "copilot",
            harnesses.detect_invoking_harness({"GITHUB_COPILOT_TOKEN": "x"}),
        )

    def test_detect_invoking_harness_none_when_ambiguous_or_empty(self) -> None:
        self.assertIsNone(harnesses.detect_invoking_harness({}))
        self.assertIsNone(
            harnesses.detect_invoking_harness({"CLAUDECODE": "1", "CODEX_SANDBOX": "1"})
        )
        self.assertIsNone(harnesses.detect_invoking_harness({"CLAUDECODE": ""}))

    def test_skill_invocations_applies_prefix_and_argument(self) -> None:
        invocations = harnesses.skill_invocations("session-run", "/plans/p/", env={})
        self.assertEqual(
            {
                "codex": "$session-run /plans/p/",
                "claude": "/session-run /plans/p/",
                "copilot": "/session-run /plans/p/",
            },
            invocations,
        )

    def test_skill_invocations_orders_detected_harness_first(self) -> None:
        invocations = harnesses.skill_invocations(
            "session-run", "/plans/p/", env={"CLAUDECODE": "1"}
        )
        self.assertEqual("claude", next(iter(invocations)))
        # Ordering never drops harnesses.
        self.assertEqual({"codex", "claude", "copilot"}, set(invocations))

    def test_skill_invocations_without_argument_omits_suffix(self) -> None:
        self.assertEqual(
            {"codex": "$foo", "claude": "/foo", "copilot": "/foo"},
            harnesses.skill_invocations("foo", env={}),
        )


class NextStepsPayloadBuilderTests(unittest.TestCase):
    def test_invocation_and_command_steps_wrap_in_envelope(self) -> None:
        payload = responses.next_steps_payload(
            [
                responses.invocation_step(
                    "run it", {"codex": "$foo", "claude": "/foo"}
                ),
                responses.command_step("asl status"),
            ]
        )
        self.assertEqual(
            {
                "next_steps": [
                    {
                        "description": "run it",
                        "invocations": {"codex": "$foo", "claude": "/foo"},
                    },
                    {"command": "asl status"},
                ]
            },
            payload,
        )

    def test_invocation_step_copies_invocations_mapping(self) -> None:
        source = {"codex": "$foo"}
        step = responses.invocation_step("d", source)
        source["codex"] = "mutated"
        self.assertEqual("$foo", step["invocations"]["codex"])


class PlanSnapshotCacheTests(unittest.TestCase):
    @contextmanager
    def _counting_inspect(self):
        calls: list[Path] = []
        real_inspect_plan = plans.inspect_plan

        def counting(plan_dir):
            calls.append(plans.canonicalize_path(plan_dir))
            return real_inspect_plan(plan_dir)

        with mock.patch.object(plans, "inspect_plan", counting):
            yield calls

    @contextmanager
    def _seeded_cache(self, statuses=("complete", "pending")):
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, statuses)
            cache = plans.PlanSnapshotCache()
            cache.get(plan_dir)
            yield plan_dir, cache

    def test_get_reuses_snapshot_while_plan_files_are_untouched(self) -> None:
        with self._seeded_cache() as (plan_dir, cache):
            with self._counting_inspect() as calls:
                first = cache.get(plan_dir)
                second = cache.get(plan_dir)

            self.assertEqual([], calls)
            self.assertIs(first, second)

    def test_get_rereads_after_mtime_change_with_identical_size(self) -> None:
        with self._seeded_cache() as (plan_dir, cache):
            session_path = plan_dir / "session-2.md"
            before = session_path.stat()
            os.utime(
                session_path,
                ns=(before.st_atime_ns + 10**9, before.st_mtime_ns + 10**9),
            )

            with self._counting_inspect() as calls:
                cache.get(plan_dir)

            self.assertEqual(before.st_size, session_path.stat().st_size)
            self.assertEqual(1, len(calls))

    def test_get_rereads_after_size_change_and_serves_new_content(self) -> None:
        with self._seeded_cache() as (plan_dir, cache):
            session_path = plan_dir / "session-2.md"
            session_path.write_text(
                session_path.read_text(encoding="utf-8").replace(
                    "- **Status**: pending", "- **Status**: complete"
                ),
                encoding="utf-8",
            )

            with self._counting_inspect() as calls:
                refreshed = cache.get(plan_dir)

            self.assertEqual(1, len(calls))
            self.assertEqual(
                ["complete", "complete"],
                [session.status for session in refreshed.sessions],
            )

    def test_get_rereads_when_a_session_file_is_added_or_removed(self) -> None:
        with self._seeded_cache() as (plan_dir, cache):
            write_plan(plan_dir, ("complete", "pending", "pending"))

            with self._counting_inspect() as calls:
                grown = cache.get(plan_dir)
            self.assertEqual(1, len(calls))
            self.assertEqual(3, len(grown.sessions))

            (plan_dir / "session-3.md").unlink()
            with self._counting_inspect() as calls:
                shrunk = cache.get(plan_dir)
            self.assertEqual(1, len(calls))
            self.assertEqual(2, len(shrunk.sessions))

    def test_get_rereads_when_an_expansion_file_is_added_or_removed(self) -> None:
        with self._seeded_cache() as (plan_dir, cache):
            expansion_path = plan_dir / "session-2.expand.md"
            expansion_path.write_text("# Notes\n", encoding="utf-8")

            with self._counting_inspect() as calls:
                with_expansion = cache.get(plan_dir)
            self.assertEqual(1, len(calls))

            expansion_path.unlink()
            with self._counting_inspect() as calls:
                without_expansion = cache.get(plan_dir)
            self.assertEqual(1, len(calls))
            self.assertNotEqual(
                with_expansion.content_sha256, without_expansion.content_sha256
            )

    def test_get_rereads_for_a_different_plan_directory(self) -> None:
        with self._seeded_cache() as (plan_dir, cache):
            other_dir = plan_dir.parent / "other-plan"
            write_plan(other_dir, ("pending",))

            with self._counting_inspect() as calls:
                other = cache.get(other_dir)

            self.assertEqual(1, len(calls))
            self.assertEqual(plans.canonicalize_path(other_dir), other.plan_dir)

    def test_get_fresh_always_rereads(self) -> None:
        with self._seeded_cache() as (plan_dir, cache):
            with self._counting_inspect() as calls:
                cache.get_fresh(plan_dir)
                cache.get_fresh(plan_dir)

            self.assertEqual(2, len(calls))

    def test_invalidate_forces_the_next_get_to_reread(self) -> None:
        with self._seeded_cache() as (plan_dir, cache):
            cache.invalidate()

            with self._counting_inspect() as calls:
                cache.get(plan_dir)
                cache.get(plan_dir)

            self.assertEqual(1, len(calls))

    def test_unreadable_plan_raises_instead_of_serving_the_cached_snapshot(
        self,
    ) -> None:
        with self._seeded_cache() as (plan_dir, cache):
            (plan_dir / "manifest.md").unlink()

            with self.assertRaises(plans.PlanError):
                cache.get(plan_dir)

            (plan_dir / "manifest.md").write_text("# Manifest\n", encoding="utf-8")
            with self._counting_inspect() as calls:
                recovered = cache.get(plan_dir)
            self.assertEqual(1, len(calls))
            self.assertEqual(2, len(recovered.sessions))

    def test_concurrent_readers_never_observe_a_stale_snapshot(self) -> None:
        with self._seeded_cache() as (plan_dir, cache):
            session_path = plan_dir / "session-2.md"
            template = session_path.read_text(encoding="utf-8")
            stop = threading.Event()
            errors: list[BaseException] = []

            def hammer(fetch) -> None:
                try:
                    while not stop.is_set():
                        snapshot = fetch(plan_dir)
                        self.assertEqual(2, len(snapshot.sessions))
                except BaseException as error:  # pragma: no cover - failure path
                    errors.append(error)

            readers = [
                threading.Thread(target=hammer, args=(cache.get,)),
                threading.Thread(target=hammer, args=(cache.get_fresh,)),
            ]
            for reader in readers:
                reader.start()
            try:
                for index in range(40):
                    marker = "complete" if index % 2 else "pending"
                    session_path.write_text(
                        template.replace(
                            "- **Status**: pending", f"- **Status**: {marker}"
                        ),
                        encoding="utf-8",
                    )
            finally:
                stop.set()
                for reader in readers:
                    reader.join(timeout=10)

            self.assertEqual([], errors)
            self.assertFalse(any(reader.is_alive() for reader in readers))
            self.assertEqual(
                plans.inspect_plan(plan_dir).content_sha256,
                cache.get(plan_dir).content_sha256,
            )


class ReviewVerdictRepairHelperTests(unittest.TestCase):
    """Unit coverage for the bounded in-run review-verdict repair loop."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.event_path = Path(self._tmp.name) / events.EVENTS_FILE_NAME

    def _append(self, name: str, payload: dict[str, object]) -> None:
        events.append_event(self.event_path, events.Event(name, payload))

    def _append_retry(self, session: str, error: str) -> None:
        self._append(
            events.REVIEW_VERDICT_RETRY_EVENT,
            {
                "session": session,
                "fingerprint": review_transitions.review_verdict_fingerprint(
                    {"reason": "malformed-review-artifact", "error": error}
                ),
                "failure_reason": "malformed-review-artifact",
                "error": error,
            },
        )

    def test_fingerprint_folds_reason_and_error(self) -> None:
        first = review_transitions.review_verdict_fingerprint(
            {"reason": "malformed-review", "error": "bad status line"}
        )
        self.assertEqual(
            first,
            review_transitions.review_verdict_fingerprint(
                {"reason": "malformed-review", "error": "bad status line"}
            ),
        )
        self.assertTrue(first.startswith("review-verdict:"))
        self.assertNotEqual(
            first,
            review_transitions.review_verdict_fingerprint(
                {"reason": "malformed-review", "error": "missing status line"}
            ),
        )
        self.assertNotEqual(
            first,
            review_transitions.review_verdict_fingerprint(
                {"reason": "review-missing", "error": "bad status line"}
            ),
        )

    def test_fingerprint_is_none_without_reason_or_error(self) -> None:
        self.assertIsNone(review_transitions.review_verdict_fingerprint({}))
        self.assertIsNone(
            review_transitions.review_verdict_fingerprint({"reason": "", "error": ""})
        )

    def test_prior_retries_reset_at_each_review_cycle(self) -> None:
        self._append_retry("3", "stale error")
        self._append(events.REVIEW_CYCLE_EVENT, {"cycle": 1})
        self._append_retry("3", "fresh error")

        priors = review_transitions.read_prior_review_verdict_retries(
            self.event_path, "3"
        )

        self.assertEqual(["fresh error"], [p["error"] for p in priors])

    def test_prior_retries_are_scoped_to_one_session(self) -> None:
        self._append_retry("3", "other session error")
        self._append_retry("4", "target error")

        priors = review_transitions.read_prior_review_verdict_retries(
            self.event_path, "4"
        )

        self.assertEqual(["target error"], [p["error"] for p in priors])

    def test_prior_retries_tolerate_a_missing_or_corrupt_log(self) -> None:
        self.assertEqual(
            [],
            review_transitions.read_prior_review_verdict_retries(self.event_path, "3"),
        )
        self.event_path.write_text("not json\n\n", encoding="utf-8")
        self.assertEqual(
            [],
            review_transitions.read_prior_review_verdict_retries(self.event_path, "3"),
        )

    def test_exhaustion_on_repeat_fingerprint_or_over_budget(self) -> None:
        self.assertFalse(
            review_transitions.is_review_verdict_repair_exhausted("fp-1", (), 1)
        )
        self.assertFalse(
            review_transitions.is_review_verdict_repair_exhausted(
                "fp-2",
                ("fp-1",),
                review_transitions.MAX_REVIEW_VERDICT_REPAIR_ATTEMPTS,
            )
        )
        self.assertTrue(
            review_transitions.is_review_verdict_repair_exhausted("fp-1", ("fp-1",), 1)
        )
        self.assertTrue(
            review_transitions.is_review_verdict_repair_exhausted(
                "fp-3",
                ("fp-1", "fp-2"),
                review_transitions.MAX_REVIEW_VERDICT_REPAIR_ATTEMPTS + 1,
            )
        )
        # An unfingerprintable verdict still respects the attempt budget.
        self.assertTrue(
            review_transitions.is_review_verdict_repair_exhausted(
                None, (), review_transitions.MAX_REVIEW_VERDICT_REPAIR_ATTEMPTS + 1
            )
        )

    def test_repair_references_quote_the_latest_error(self) -> None:
        self.assertEqual(
            (),
            review_transitions.review_verdict_repair_references(self.event_path, "3"),
        )
        self._append_retry("3", "first error")
        self._append_retry("3", "second error")

        references = review_transitions.review_verdict_repair_references(
            self.event_path, "3"
        )

        joined = "\n".join(references)
        self.assertIn("attempt 2", joined)
        self.assertIn("second error", joined)
        self.assertNotIn("first error", joined)
        self.assertIn(review.FINAL_REVIEW_FILENAME, joined)
        self.assertIn(review.FINAL_REVIEW_ARTIFACT_FILENAME, joined)


if __name__ == "__main__":
    unittest.main()
