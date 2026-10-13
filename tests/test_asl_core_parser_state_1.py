"""ParserAndStateTests (part 1).

Split from tests/test_asl_core.py."""

from __future__ import annotations

import io
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

from agent_session_loop import (
    events,
    plans_errors,
    plans_files,
    plans_payloads,
    plans_session_ids,
    plans_snapshot,
    state_status,
    state_status_text,
    tail,
)
from agent_session_loop.formatting import (
    format_duration_seconds,
    format_turn_finished_message,
    format_turn_started_message,
)
from tests.asl_core_support import ParserAndStateTestsBase
from tests.support import (
    write_plan,
)


class ParserAndStateTests(ParserAndStateTestsBase):
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
                    "session_kind": plans_payloads.SESSION_KIND_REVIEW,
                }
            ),
        )
        self.assertEqual(
            "session 7/7 started",
            format_turn_started_message(
                {
                    "session_id": "7",
                    "total_sessions": 7,
                    "session_kind": plans_payloads.SESSION_KIND_IMPLEMENTATION,
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
                    "session_kind": plans_payloads.SESSION_KIND_REVIEW,
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
            plans_session_ids.parse_session_id(value)
            for value in ("2.10", "2", "10", "2.1", "2.2")
        ]

        self.assertEqual(
            ["2", "2.1", "2.2", "2.10", "10"],
            [
                session_id.value
                for session_id in sorted(parsed, key=lambda item: item.sort_key)
            ],
        )
        self.assertEqual((2, 1), plans_session_ids.parse_session_id("2.1").components)
        self.assertEqual(
            "session-2.1.md", plans_session_ids.session_id_to_filename("2.1")
        )
        self.assertEqual(
            "session-2.1.expand.md",
            plans_session_ids.session_id_to_expansion_filename("2.1"),
        )
        self.assertEqual(
            "Session 2.1", plans_session_ids.session_id_display_label("2.1")
        )

        for value in ("0", "01", "1.0", "1.", "1..2", "1.a", "a"):
            with self.subTest(value=value):
                self.assertFalse(plans_session_ids.is_valid_session_id(value))
                with self.assertRaises(ValueError):
                    plans_session_ids.parse_session_id(value)

    def test_session_filename_helpers_match_dotted_files_exactly(self) -> None:
        session_id = plans_session_ids.session_id_from_filename("session-2.1.md")
        expansion_id = plans_session_ids.session_id_from_expansion_filename(
            "session-2.1.expand.md"
        )

        self.assertIsNotNone(session_id)
        self.assertIsNotNone(expansion_id)
        self.assertEqual("2.1", session_id.value)
        self.assertEqual("2.1", expansion_id.value)

        self.assertIsNone(
            plans_session_ids.session_id_from_filename("session-2.1.expand.md")
        )
        self.assertIsNone(plans_session_ids.session_id_from_filename("session-02.md"))
        self.assertIsNone(
            plans_session_ids.session_id_from_expansion_filename("session-2.1.md")
        )
        self.assertIsNone(
            plans_session_ids.session_id_from_expansion_filename(
                "session-2.01.expand.md"
            )
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

            with self.assertRaises(plans_errors.PlanError) as raised:
                plans_snapshot.inspect_plan(plan_dir)

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

            snapshot = plans_snapshot.inspect_plan(plan_dir)

            self.assertEqual(
                [1, 2, 3, 10], [session.number for session in snapshot.sessions]
            )
            self.assertEqual(
                ["complete", "pending", "complete", "pending"],
                [session.status for session in snapshot.sessions],
            )
            self.assertEqual(2, snapshot.next_pending.number)
            self.assertEqual(plans_files.path_sha256(plan_dir), snapshot.path_hash)

    def test_inspect_plan_uses_manifest_title_for_status_projection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            (plan_dir / "context.md").write_text("# Context decoy\n", encoding="utf-8")
            (plan_dir / "manifest.md").write_text(
                "# Durable plan title\n", encoding="utf-8"
            )

            snapshot = plans_snapshot.inspect_plan(plan_dir)
            payload = state_status.status_payload(snapshot, state_dir)
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                state_status_text._print_text_status(payload)

            self.assertEqual("Durable plan title", snapshot.plan_title)
            self.assertEqual("Durable plan title", payload["plan_title"])
            self.assertEqual(
                [
                    "Plan title: Durable plan title",
                    f"Plan: {plans_files.canonicalize_path(plan_dir)}",
                ],
                buffer.getvalue().splitlines()[:2],
            )

    def test_inspect_plan_title_falls_back_to_directory_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "legacy-plan"
            write_plan(plan_dir, ("pending",))
            (plan_dir / "manifest.md").write_text("**Sessions**: 1\n", encoding="utf-8")

            snapshot = plans_snapshot.inspect_plan(plan_dir)

            self.assertEqual("legacy-plan", snapshot.plan_title)
