"""Event persistence, plan parsing, and state schema tests.

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
    mailbox,
    plans,
    preflight,
    prepare,
    progress_cli,
    progress_repair,
    qna,
    responses,
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
    write_plan,
    write_plan_with_ids,
    fail_open_for_path,
    _write_state_payload,
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
            self.assertEqual(
                plans.PlanMetadata(cwd=str(cwd.resolve())), metadata
            )
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
            self.assertEqual(
                readiness_before, plans.stable_readiness_sha256(plan_dir)
            )


class EventPersistenceTests(unittest.TestCase):
    def test_append_event_writes_complete_json_lines_under_concurrency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / events.EVENTS_FILE_NAME

            def append_many(worker: int) -> None:
                for index in range(50):
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
            for thread in threads:
                thread.join(timeout=2)
                self.assertFalse(thread.is_alive())

            lines = event_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(400, len(lines))
            records = [json.loads(line) for line in lines]
            observed = {
                (record["payload"]["worker"], record["payload"]["index"])
                for record in records
            }
            expected = {
                (worker, index)
                for worker in range(8)
                for index in range(50)
            }
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
                    "session_kind": runner.SESSION_KIND_REVIEW,
                }
            ),
        )
        self.assertEqual(
            "session 7/7 started",
            format_turn_started_message(
                {
                    "session_id": "7",
                    "total_sessions": 7,
                    "session_kind": runner.SESSION_KIND_IMPLEMENTATION,
                }
            ),
        )
        self.assertEqual(
            "session 7/7 started",
            format_turn_started_message(
                {"session_id": "7", "total_sessions": 7}
            ),
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
            "session 2/7 finished "
            "(status complete, return code 0, duration 6m13s)",
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
                    "session_kind": runner.SESSION_KIND_REVIEW,
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
            plans.parse_session_id(value)
            for value in ("2.10", "2", "10", "2.1", "2.2")
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
        expansion_id = plans.session_id_from_expansion_filename(
            "session-2.1.expand.md"
        )

        self.assertIsNotNone(session_id)
        self.assertIsNotNone(expansion_id)
        self.assertEqual("2.1", session_id.value)
        self.assertEqual("2.1", expansion_id.value)

        self.assertIsNone(plans.session_id_from_filename("session-2.1.expand.md"))
        self.assertIsNone(plans.session_id_from_filename("session-02.md"))
        self.assertIsNone(
            plans.session_id_from_expansion_filename("session-2.1.md")
        )
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
            self.assertEqual("unknown-status", runner._invalid_status_pause(unknown)[0])

            (plan_dir / "session-1.md").write_text(
                "# Session 1\n\n## Handoff\n\n- **Files modified**: none\n",
                encoding="utf-8",
            )
            empty = plans.inspect_plan(plan_dir)
            self.assertEqual("empty", empty.sessions[0].status)
            self.assertEqual("malformed-plan", runner._invalid_status_pause(empty)[0])

    def test_inspect_plan_wraps_plan_file_read_failures(self) -> None:
        cases = (
            "context.md",
            "manifest.md",
            "session-1.expand.md",
        )

        for relative_name in cases:
            with self.subTest(relative_name=relative_name):
                with tempfile.TemporaryDirectory() as tmp:
                    plan_dir = Path(tmp) / "plan"
                    write_plan(plan_dir, ("pending",))
                    target = plan_dir / relative_name
                    if not target.exists():
                        target.write_text("extra plan metadata\n", encoding="utf-8")

                    with fail_open_for_path(target):
                        with self.assertRaises(plans.PlanError) as raised:
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

            with mock.patch.object(Path, "iterdir", failing_iterdir):
                with self.assertRaises(plans.PlanError) as raised:
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
            with self.subTest(value=invalid):
                with self.assertRaises(plans.PlanError):
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
            with self.subTest(label=label):
                with self.assertRaises(plans.PlanError):
                    plans.parse_readiness_metadata(text)

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
        self.assertEqual(
            ("python3", "-m", "unittest", "tests.t"), command_check.argv
        )
        self.assertEqual(30.0, command_check.timeout_seconds)
        self.assertIsNone(command_check.cwd)
        self.assertEqual("check-3", regex_check.name)
        self.assertEqual("^def parse_completion_contract", regex_check.pattern)

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
        self.assertEqual(contracts.DEFAULT_COMMAND_TIMEOUT_SECONDS, check.timeout_seconds)
        self.assertEqual("subdir", check.cwd)

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
                '{"checks": [{"type": "regex_in_file", "path": "a", '
                '"pattern": "("}]}',
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
                    '    {"type": "file_exists", "path": "agent_session_loop/contracts.py"}',
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
            self.assertEqual(
                "initial-review", payload["review_resume"]["action"]
            )
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
            self.assertEqual(0, status["last_seq"])
            self.assertEqual(0, status["pending_guidance"])
            self.assertEqual(0, status["unanswered_questions"])
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
            self.assertEqual(2, status["last_seq"])
            self.assertEqual(1, status["pending_guidance"])
            self.assertEqual(1, status["unanswered_questions"])
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
            self.assertIn("1 unanswered questions", output)
            self.assertIn("Mailbox lease: fresh", output)

    def test_status_payload_surfaces_full_and_malformed_mailboxes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            mailbox.append_guidance(plan_dir, "x", state_dir=state_dir)

            with mock.patch.object(mailbox, "MAILBOX_MAX_BYTES", 1):
                full_payload = state.status_payload(
                    plans.inspect_plan(plan_dir), state_dir
                )

            full_status = full_payload["mailbox"]
            self.assertTrue(full_status["full"])
            self.assertEqual(1, full_status["max_bytes"])

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
            with mock.patch.object(
                mailbox, "read_mailbox", side_effect=read_error
            ):
                payload = state.status_payload(
                    plans.inspect_plan(plan_dir), state_dir
                )

            status = payload["mailbox"]
            self.assertTrue(status["present"])
            self.assertIsNone(status["size_bytes"])
            self.assertIsNone(status["full"])
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
                        "run_started_at": "2026-07-24T11:59:00+00:00",
                        "run_finished_at": None,
                        "waiting_for_reply": waiting,
                    }
                ),
                encoding="utf-8",
            )

            payload = state.status_payload(plans.inspect_plan(plan_dir), state_dir)

            self.assertEqual(waiting, payload["waiting_for_reply"])
            self.assertEqual(
                waiting, payload["runner_state"]["waiting_for_reply"]
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

    def test_status_payload_flags_stale_unfinished_runner_state(self) -> None:
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

            runner_state = payload["runner_state"]
            self.assertTrue(runner_state["present"])
            self.assertTrue(runner_state["stale_unfinished"])
            self.assertIsNotNone(runner_state["stale_reason"])
            self.assertIn("no lock file is present", runner_state["stale_reason"])

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
                payload = state.status_payload(
                    plans.inspect_plan(plan_dir), state_dir
                )
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

            payload = state.status_payload(plans.inspect_plan(plan_dir), state_dir)

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
                            "current_check_started_at": (
                                "2026-06-20T10:00:01+00:00"
                            ),
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
            with redirect_stdout(buffer):
                exit_code = state.status_from_args(args)

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
                        "run_started_at": "2026-06-19T12:00:00+00:00",
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
            "version": runner.RESULT_SCHEMA_VERSION,
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

    def test_write_state_payload_compacts_turns_and_stamps_v2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            payload = self._base_state_payload(state_path)
            runner._write_state_payload(payload)

            persisted = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(runner.STATE_SCHEMA_VERSION, persisted["version"])
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
            runner._write_state_payload(payload)

            size = state_path.stat().st_size

        self.assertLess(size, 1_000_000, f"state.json was {size} bytes")

    def test_run_finished_event_is_compact_without_raw_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / "events.jsonl"
            payload = self._base_state_payload(Path(tmp) / "state.json")
            payload["events_path"] = str(event_path)
            persistence = runner._RunPersistence(write_state=False)
            runner._finish_run(event_path, payload, persistence=persistence)

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

    def test_run_paused_event_and_state_bound_verification_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            event_path = Path(tmp) / "events.jsonl"
            payload = self._base_state_payload(state_path)
            persistence = runner._RunPersistence()
            details = {
                "session": "1",
                "recipe": {"check_count": 1, "kinds": ["command"], "names": ["tests"]},
                "passed": False,
                "checks": [self._verification_check()],
                "failures": [self._verification_check()],
            }
            runner._pause_run(
                event_path,
                payload,
                reason="verify-failed",
                message="verification failed",
                details=details,
                persistence=persistence,
            )
            runner._write_state_payload(payload)

            records = [
                json.loads(line)
                for line in event_path.read_text(encoding="utf-8").splitlines()
            ]
            persisted = json.loads(state_path.read_text(encoding="utf-8"))

        paused = [r for r in records if r["name"] == events.RUN_PAUSED_EVENT][0]
        event_check = paused["payload"]["details"]["checks"][0]
        self.assertNotIn("stdout", event_check)
        self.assertEqual("/artifacts/tests-stdout.gz", event_check["stdout_artifact_path"])
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
            payload = state.status_payload(snapshot, state_dir)

        self.assertEqual("legacy-run", payload["runner_state"]["run_id"])
        self.assertEqual(1, payload["active_turn"]["session"])


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
            (entry / events.STATE_FILE_NAME).write_text(
                "{ not json", encoding="utf-8"
            )
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

            with mock.patch.object(
                Path, "iterdir", side_effect=OSError("boom")
            ):
                with self.assertRaises(state.StateError):
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
            harnesses.detect_invoking_harness(
                {"CLAUDECODE": "1", "CODEX_SANDBOX": "1"}
            )
        )
        self.assertIsNone(
            harnesses.detect_invoking_harness({"CLAUDECODE": ""})
        )

    def test_skill_invocations_applies_prefix_and_argument(self) -> None:
        invocations = harnesses.skill_invocations(
            "session-run", "/plans/p/", env={}
        )
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


if __name__ == "__main__":
    unittest.main()
