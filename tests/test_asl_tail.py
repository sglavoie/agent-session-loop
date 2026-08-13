"""Tail follower tests.

Split from tests/test_agent_session_loop.py."""

from __future__ import annotations

import argparse
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import dataclasses
from datetime import datetime, timedelta, timezone
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
    write_plan,
    wait_until,
)


class TailFollowerTests(unittest.TestCase):
    def setup_tail_files(self, tmp_dir: Path):
        plan_dir = tmp_dir / "plan"
        state_dir = tmp_dir / "state"
        write_plan(plan_dir, ("pending",))
        plan_state = state.plan_state_dir(plan_dir, state_dir)
        plan_state.mkdir(parents=True)
        return (
            plan_dir,
            state_dir,
            plan_state / events.STATE_FILE_NAME,
            plan_state / events.EVENTS_FILE_NAME,
        )

    def write_state_payload(
        self,
        state_path: Path,
        payload: dict[str, object],
    ) -> None:
        state_path.write_text(json.dumps(payload), encoding="utf-8")

    def append_log_line(self, path: Path, text: str) -> None:
        with path.open("a", encoding="utf-8") as log_file:
            log_file.write(text)

    def test_agent_mux_delivery_failures_have_a_correlation_summary(self) -> None:
        message = tail._event_message(
            events.AGENT_MUX_DELIVERY_EVENT,
            {
                "agent_mux_event": "AgentSessionLoopTurnStarted",
                "source_event_id": "run-1:2",
                "attempt_count": 3,
                "final_status": "retryable_exhausted",
                "trace_id": "trace-3",
                "error_classification": "retryable_outcome",
            },
        )
        self.assertIn("AgentMux delivery retryable_exhausted", message)
        self.assertIn("source run-1:2", message)
        self.assertIn("attempts 3", message)
        self.assertIn("trace trace-3", message)

    def test_commit_group_preview_bounds_and_coerces_durable_values(self) -> None:
        remaining = list(range(25))
        successful = [
            {"alias": index, "commit_sha": index + 100}
            for index in range(25)
        ]
        payload = {
            "phase": "updated",
            "group": {
                "scope": "end",
                "remaining_aliases": remaining,
                "successful_repositories": successful,
            },
        }

        message = tail._event_message(
            events.COMMIT_GROUP_UPDATED_EVENT,
            payload,
        )

        self.assertIn(
            "remaining " + ",".join(str(index) for index in range(20))
            + " (+5 more)",
            message,
        )
        self.assertIn(
            "partial SHAs "
            + ",".join(f"{index}={index + 100}" for index in range(20))
            + " (+5 more)",
            message,
        )
        self.assertNotIn("20=120", message)
        self.assertEqual(25, len(payload["group"]["remaining_aliases"]))
        self.assertEqual(25, len(payload["group"]["successful_repositories"]))

    def test_model_control_events_include_target_changes_and_overrides(
        self,
    ) -> None:
        message = tail._event_message(
            events.RUN_CONTROL_EVENT,
            {
                "action": (
                    events.RUN_CONTROL_ACTION_IMPLEMENTATION_MODEL_UPDATED
                ),
                "message": "implementation model controls updated",
                "stop_after_current": False,
                "target_harness": "claude",
                "changed_fields": ["model", "effort"],
                "implementation_overrides": {
                    "claude": {
                        "model": "claude-sonnet-4-5",
                        "effort": "high",
                    }
                },
                "session": "4",
                "total_sessions": 13,
                "turn_index": 1,
            },
        )

        self.assertIn("target harness claude", message)
        self.assertIn("changed model, effort", message)
        self.assertIn(
            'effective overrides {"claude": {"effort": "high", '
            '"model": "claude-sonnet-4-5"}}',
            message,
        )
        self.assertIn("session 4/13", message)
        self.assertIn("turn 1", message)

    def test_run_control_status_line_omits_abort_when_none_pending(
        self,
    ) -> None:
        message = tail._event_message(
            events.RUN_CONTROL_EVENT,
            {
                "action": events.RUN_CONTROL_ACTION_STOP_AFTER_CURRENT_REQUESTED,
                "message": "stop after current session requested",
                "stop_after_current": True,
                "abort_requested": False,
                "abort_grace_seconds": None,
                "abort_reason": None,
                "abort_deadline": None,
            },
        )

        self.assertEqual(
            "run control: stop after current session requested "
            "(stop-after-current on)",
            message,
        )

    def test_run_control_status_line_counts_down_pending_abort_grace(
        self,
    ) -> None:
        deadline = datetime.now(timezone.utc) + timedelta(seconds=90)
        message = tail._event_message(
            events.RUN_CONTROL_EVENT,
            {
                "action": events.RUN_CONTROL_ACTION_ABORT_TURN_REQUESTED,
                "message": "abort of the in-flight turn requested",
                "stop_after_current": False,
                "abort_requested": True,
                "abort_grace_seconds": 120,
                "abort_reason": "supervisor stop",
                "abort_deadline": deadline.isoformat(),
            },
        )

        self.assertIn("abort pending", message)
        self.assertIn("abort grace 120s", message)
        self.assertRegex(message, r"\b(89|90)s remaining\b")
        self.assertIn("abort reason supervisor stop", message)

    def test_run_control_status_line_marks_expired_abort_grace(self) -> None:
        # The deadline is the runner's clock, not the reader's: a tail
        # attached after the window closed must not print a negative count.
        message = tail._event_message(
            events.RUN_CONTROL_EVENT,
            {
                "action": events.RUN_CONTROL_ACTION_ABORT_TURN_REQUESTED,
                "message": "abort of the in-flight turn requested",
                "stop_after_current": False,
                "abort_requested": True,
                "abort_grace_seconds": 5,
                "abort_deadline": "2020-01-01T00:00:00+00:00",
            },
        )

        self.assertIn("abort pending", message)
        self.assertIn("grace expired", message)
        self.assertNotIn("remaining", message)

    def test_run_control_status_line_reports_unarmed_abort(self) -> None:
        # A main-thread checkpoint observes the abort with no turn to kill, so
        # nothing stamps a deadline before the action is recorded.
        message = tail._event_message(
            events.RUN_CONTROL_EVENT,
            {
                "action": events.RUN_CONTROL_ACTION_ABORT_TURN_REQUESTED,
                "message": "abort of the in-flight turn requested",
                "stop_after_current": False,
                "abort_requested": True,
                "abort_grace_seconds": 120,
                "abort_deadline": None,
            },
        )

        self.assertIn("abort pending", message)
        self.assertIn("grace not started", message)

    def test_run_control_status_line_clears_abort_on_terminal_actions(
        self,
    ) -> None:
        # Both terminal actions still carry `abort_requested` from the action
        # they consumed, so the renderer keys off the action name instead.
        for action in (
            events.RUN_CONTROL_ACTION_ABORT_TURN_HONORED,
            events.RUN_CONTROL_ACTION_ABORT_TURN_SATISFIED_BY_WORKER,
        ):
            with self.subTest(action=action):
                message = tail._event_message(
                    events.RUN_CONTROL_EVENT,
                    {
                        "action": action,
                        "message": action,
                        "stop_after_current": True,
                        "abort_requested": True,
                        "abort_grace_seconds": 120,
                        "abort_reason": "supervisor stop",
                        "abort_deadline": None,
                    },
                )

                self.assertIn("abort resolved", message)
                self.assertIn("abort grace 120s", message)
                self.assertNotIn("abort pending", message)
                self.assertNotIn("remaining", message)
                self.assertNotIn("grace not started", message)

    def test_mailbox_guidance_event_renders_metadata_without_body(self) -> None:
        message = tail._event_message(
            events.MAILBOX_EVENT,
            {
                "action": events.MAILBOX_ACTION_GUIDANCE_OBSERVED,
                "message_id": "guidance-1",
                "seq": 4,
                "length": 37,
                "digest": "abc123",
                "session_id": "7",
                "turn_index": 2,
            },
        )

        self.assertEqual(
            "mailbox: guidance observed "
            "(id guidance-1, sequence 4, length 37, digest abc123, "
            "session 7, turn 2)",
            message,
        )

    def test_mailbox_notice_event_renders_metadata_without_body(self) -> None:
        message = tail._event_message(
            events.MAILBOX_EVENT,
            {
                "action": events.MAILBOX_ACTION_NOTICE_PUBLISHED,
                "message_id": "notice-1",
                "seq": 8,
                "length": 23,
                "digest": "def456",
                "run_id": "run-1",
                "session_id": "7",
                "turn_index": 2,
            },
        )

        self.assertEqual(
            "mailbox: notice published "
            "(id notice-1, sequence 8, length 23, digest def456, "
            "run run-1, session 7, turn 2)",
            message,
        )
        self.assertNotIn("notice body", message)

    def test_mailbox_wait_started_event_renders_rolling_ceiling(self) -> None:
        message = tail._event_message(
            events.MAILBOX_EVENT,
            {
                "action": events.MAILBOX_ACTION_WAIT_STARTED,
                "question_id": "question-1",
                "session_id": "9",
                "turn_index": 3,
                "pending_outbox": 2,
                "base_deadline": "2026-07-24T12:05:00+00:00",
                "unsupervised_deadline_at": "2026-07-24T12:30:00+00:00",
                "unsupervised_ceiling_seconds": 1800.0,
                "last_lease_activity_at": "2026-07-24T12:01:00+00:00",
            },
        )

        self.assertEqual(
            "mailbox: reply wait started "
            "(question question-1, session 9, turn 3, "
            "pending outbox 2, "
            "base deadline 2026-07-24T12:05:00+00:00, "
            "unsupervised deadline 2026-07-24T12:30:00+00:00, "
            "unsupervised ceiling 1800.0s, "
            "last lease activity 2026-07-24T12:01:00+00:00)",
            message,
        )
        self.assertNotIn("hard deadline", message)

    def test_mailbox_wait_started_event_renders_as_nonterminal_wait(self) -> None:
        # Historical records carry no rolling ceiling; they keep their original
        # `hard deadline` rendering.
        message = tail._event_message(
            events.MAILBOX_EVENT,
            {
                "action": events.MAILBOX_ACTION_WAIT_STARTED,
                "question_id": "question-1",
                "session_id": "9",
                "turn_index": 3,
                "pending_outbox": 2,
                "base_deadline": "2026-07-24T12:05:00+00:00",
                "hard_deadline": "2026-07-24T12:15:00+00:00",
                "last_lease_activity_at": "2026-07-24T12:01:00+00:00",
            },
        )

        self.assertEqual(
            "mailbox: reply wait started "
            "(question question-1, session 9, turn 3, "
            "pending outbox 2, "
            "base deadline 2026-07-24T12:05:00+00:00, "
            "hard deadline 2026-07-24T12:15:00+00:00, "
            "last lease activity 2026-07-24T12:01:00+00:00)",
            message,
        )
        self.assertNotIn("paused", message)

    def test_mailbox_wait_cleared_event_renders_reason_and_count(self) -> None:
        message = tail._event_message(
            events.MAILBOX_EVENT,
            {
                "action": events.MAILBOX_ACTION_WAIT_CLEARED,
                "question_id": "question-1",
                "session_id": "9",
                "turn_index": 3,
                "reason": "supervisor-lost",
                "pending_outbox": 2,
            },
        )

        self.assertEqual(
            "mailbox: reply wait cleared "
            "(question question-1, session 9, turn 3, "
            "reason supervisor-lost, pending outbox 2)",
            message,
        )
        self.assertNotIn("paused", message)

    def test_mailbox_wait_cleared_event_renders_resolution_metadata(self) -> None:
        message = tail._event_message(
            events.MAILBOX_EVENT,
            {
                "action": events.MAILBOX_ACTION_WAIT_CLEARED,
                "question_id": "question-1",
                "session_id": "9",
                "turn_index": 3,
                "reason": "resolution",
                "pending_outbox": 0,
                "resolution_message_id": "resolution-1",
                "resolution_seq": 12,
                "resolution_action": "continue",
            },
        )

        self.assertEqual(
            "mailbox: reply wait cleared "
            "(question question-1, session 9, turn 3, reason resolution, "
            "pending outbox 0, resolution resolution-1, "
            "resolution sequence 12, resolution action continue)",
            message,
        )
        self.assertNotIn("question body", message)
        self.assertNotIn("resolution note", message)

    def test_mailbox_wait_cleared_event_renders_lease_evidence(self) -> None:
        message = tail._event_message(
            events.MAILBOX_EVENT,
            {
                "action": events.MAILBOX_ACTION_WAIT_CLEARED,
                "question_id": "question-1",
                "session_id": "9",
                "turn_index": 3,
                "reason": "supervisor-lost",
                "pending_outbox": 2,
                "lease_supervisor_id": "supervisor-1",
                "lease_updated_at": "2026-07-24T12:01:00+00:00",
                "lease_reason": "expired",
            },
        )

        self.assertEqual(
            "mailbox: reply wait cleared "
            "(question question-1, session 9, turn 3, "
            "reason supervisor-lost, pending outbox 2, "
            "lease supervisor supervisor-1, "
            "lease updated 2026-07-24T12:01:00+00:00, "
            "lease reason expired)",
            message,
        )

    def test_mailbox_wait_cleared_event_renders_suspend_evidence(self) -> None:
        message = tail._event_message(
            events.MAILBOX_EVENT,
            {
                "action": events.MAILBOX_ACTION_WAIT_CLEARED,
                "question_id": "question-1",
                "session_id": "9",
                "turn_index": 3,
                "reason": "supervisor-lost",
                "pending_outbox": 2,
                "lease_supervisor_id": "supervisor-1",
                "lease_updated_at": "2026-07-24T12:01:00+00:00",
                "lease_reason": "expired",
                "suspend_skew_seconds": 3600.0,
                "suspend_lease_grace_seconds": 90.0,
            },
        )

        self.assertEqual(
            "mailbox: reply wait cleared "
            "(question question-1, session 9, turn 3, "
            "reason supervisor-lost, pending outbox 2, "
            "lease supervisor supervisor-1, "
            "lease updated 2026-07-24T12:01:00+00:00, "
            "lease reason expired, "
            "suspend skew 3600.0s, "
            "suspend lease grace 90.0s)",
            message,
        )

    def test_mailbox_wait_cleared_event_renders_partial_lease_evidence(
        self,
    ) -> None:
        message = tail._event_message(
            events.MAILBOX_EVENT,
            {
                "action": events.MAILBOX_ACTION_WAIT_CLEARED,
                "question_id": "question-1",
                "session_id": "9",
                "turn_index": 3,
                "reason": "timeout",
                "pending_outbox": 1,
                "lease_supervisor_id": "supervisor-1",
                "lease_updated_at": "2026-07-24T12:14:00+00:00",
            },
        )

        self.assertEqual(
            "mailbox: reply wait cleared "
            "(question question-1, session 9, turn 3, "
            "reason timeout, pending outbox 1, "
            "lease supervisor supervisor-1, "
            "lease updated 2026-07-24T12:14:00+00:00)",
            message,
        )
        self.assertNotIn("lease reason", message)

    def test_mailbox_wait_preparation_failed_renders_reason_and_identity(
        self,
    ) -> None:
        message = tail._event_message(
            events.MAILBOX_EVENT,
            {
                "action": events.MAILBOX_ACTION_WAIT_PREPARATION_FAILED,
                "reason": "expired",
                "session_id": "9",
                "turn_index": 3,
                "run_id": "run-abc",
            },
        )

        self.assertEqual(
            "mailbox: reply wait preparation failed "
            "(reason expired, session 9, turn 3, run run-abc)",
            message,
        )

    def test_every_mailbox_action_has_a_specific_tail_renderer(self) -> None:
        payload = {
            "message_id": "message-1",
            "seq": 1,
            "length": 12,
            "digest": "abc123",
            "session_id": "1",
            "turn_index": 1,
            "question_id": "question-1",
            "pending_outbox": 0,
            "base_deadline": "base",
            "hard_deadline": "hard",
            "reason": "expired",
            "run_id": "run-abc",
            "resolution_message_id": "resolution-1",
            "resolution_seq": 2,
            "resolution_action": "continue",
        }

        for action in events.MAILBOX_ACTIONS:
            with self.subTest(action=action):
                rendered = tail._mailbox_message(
                    {"action": action, **payload}
                )
                self.assertNotEqual(f"mailbox: {action}", rendered)

    def cleanup_follower(self, follower: tail.TailFollower) -> None:
        follower.stop()
        follower.join(1.0)

    def test_tail_follower_starts_at_eof_for_existing_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path, events_path = self.setup_tail_files(
                tmp_dir
            )
            stdout_log = tmp_dir / "stdout.log"
            stderr_log = tmp_dir / "stderr.log"
            stdout_log.write_text("old stdout\n", encoding="utf-8")
            stderr_log.write_text("old stderr\n", encoding="utf-8")
            turn_payload = {
                "run_id": "run-1",
                "session": 1,
                "session_id": "1",
                "total_sessions": 1,
                "turn_index": 1,
                "stdout_path": str(stdout_log),
                "stderr_path": str(stderr_log),
            }
            self.write_state_payload(
                state_path,
                {
                    "run_id": "run-1",
                    "active_turn": turn_payload,
                    "turns": [turn_payload],
                    "run_finished_at": None,
                },
            )
            events.append_event(
                events_path,
                events.Event(
                    name="turn_started",
                    payload=turn_payload,
                    created_at="2026-06-11T12:00:00+00:00",
                ),
            )
            out = io.StringIO()
            follower = tail.TailFollower(
                plan_dir,
                state_dir=state_dir,
                out=out,
                poll_interval=0.01,
            )
            self.addCleanup(self.cleanup_follower, follower)

            follower.start()
            self.append_log_line(stdout_log, "new stdout\n")
            events.append_event(
                events_path,
                events.Event(
                    name="turn_finished",
                    payload={
                        **turn_payload,
                        "status_result": "complete",
                        "returncode": 0,
                        "duration_seconds": 1.2,
                    },
                    created_at="2026-06-11T12:00:01+00:00",
                ),
            )

            wait_until(
                lambda: (
                    "session 1/1 finished" in out.getvalue()
                    and "new stdout" in out.getvalue()
                )
            )
            follower.stop()
            self.assertTrue(follower.join(1.0))
            text = out.getvalue()
            self.assertNotIn("old stdout", text)
            self.assertNotIn("old stderr", text)
            self.assertNotIn("session 1/1 started", text)
            self.assertIn("stdout session=1 turn=1 | new stdout", text)
            self.assertIn("session 1/1 finished", text)

    def test_tail_follower_discovers_log_targets_from_new_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path, events_path = self.setup_tail_files(
                tmp_dir
            )
            stdout_log = tmp_dir / "stdout.log"
            self.write_state_payload(
                state_path,
                {"run_id": "run-1", "active_turn": None, "turns": []},
            )
            out = io.StringIO()
            follower = tail.TailFollower(
                plan_dir,
                state_dir=state_dir,
                out=out,
                poll_interval=0.01,
            )
            self.addCleanup(self.cleanup_follower, follower)

            follower.start()
            stdout_log.write_text("discovered stdout\n", encoding="utf-8")
            events.append_event(
                events_path,
                events.Event(
                    name="turn_started",
                    payload={
                        "run_id": "run-1",
                        "session": 1,
                        "session_id": "1",
                        "total_sessions": 1,
                        "turn_index": 1,
                        "stdout_path": str(stdout_log),
                    },
                    created_at="2026-06-11T12:00:00+00:00",
                ),
            )

            wait_until(
                lambda: (
                    "session 1/1 started" in out.getvalue()
                    and "stdout session=1 turn=1 | discovered stdout"
                    in out.getvalue()
                )
            )
            follower.stop()
            self.assertTrue(follower.join(1.0))

    def test_tail_follower_stop_cancels_following_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path, events_path = self.setup_tail_files(
                tmp_dir
            )
            self.write_state_payload(
                state_path,
                {"run_id": "run-1", "active_turn": None, "turns": []},
            )
            out = io.StringIO()
            follower = tail.TailFollower(
                plan_dir,
                state_dir=state_dir,
                out=out,
                poll_interval=0.01,
            )
            self.addCleanup(self.cleanup_follower, follower)

            follower.start()
            self.assertFalse(follower.join(0.02))
            follower.stop()
            self.assertTrue(follower.join(1.0))
            self.assertTrue(follower.done)
            before = out.getvalue()
            events.append_event(
                events_path,
                events.Event(
                    name="turn_started",
                    payload={"run_id": "run-1", "session": 1, "turn_index": 1},
                ),
            )
            time.sleep(0.05)
            self.assertEqual(before, out.getvalue())

    def test_tail_follower_stops_after_run_finished_and_drains_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path, events_path = self.setup_tail_files(
                tmp_dir
            )
            stdout_log = tmp_dir / "stdout.log"
            stdout_log.write_text("", encoding="utf-8")
            turn_payload = {
                "run_id": "run-1",
                "session": 1,
                "session_id": "1",
                "total_sessions": 1,
                "turn_index": 1,
                "stdout_path": str(stdout_log),
            }
            self.write_state_payload(
                state_path,
                {"run_id": "run-1", "active_turn": turn_payload, "turns": []},
            )
            out = io.StringIO()
            follower = tail.TailFollower(
                plan_dir,
                state_dir=state_dir,
                out=out,
                poll_interval=0.01,
            )
            self.addCleanup(self.cleanup_follower, follower)

            follower.start()
            self.append_log_line(stdout_log, "final stdout\n")
            events.append_event(
                events_path,
                events.Event(
                    name="run_finished",
                    payload={
                        "run_id": "run-1",
                        "ok": True,
                        "reason": "all-complete",
                        "duration_seconds": 1.0,
                    },
                    created_at="2026-06-11T12:00:02+00:00",
                ),
            )

            wait_until(lambda: follower.done)
            self.assertTrue(follower.join(1.0))
            text = out.getvalue()
            self.assertIn("run finished: run run-1", text)
            self.assertIn("stdout session=1 turn=1 | final stdout", text)

    def test_tail_follower_stops_on_synthetic_abandoned_run_finished(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path, events_path = self.setup_tail_files(
                tmp_dir
            )
            turn_payload = {
                "run_id": "run-1",
                "session": 1,
                "session_id": "1",
                "total_sessions": 1,
                "turn_index": 1,
            }
            # The state file still looks in-flight: only the synthetic event
            # can terminate the follower here, which is the whole reason the
            # abandoned record reuses the `run_finished` name.
            self.write_state_payload(
                state_path,
                {
                    "run_id": "run-1",
                    "active_turn": turn_payload,
                    "turns": [],
                    "run_finished_at": None,
                },
            )
            out = io.StringIO()
            follower = tail.TailFollower(
                plan_dir,
                state_dir=state_dir,
                out=out,
                poll_interval=0.01,
            )
            self.addCleanup(self.cleanup_follower, follower)

            follower.start()
            events.append_event(
                events_path,
                events.Event(
                    name=events.RUN_FINISHED_EVENT,
                    payload={
                        "run_id": "run-1",
                        "ok": False,
                        "paused": False,
                        "reason": "abandoned",
                        "duration_seconds": None,
                        "synthetic": True,
                        "outcome": events.RUN_OUTCOME_ABANDONED,
                    },
                    created_at="2026-06-11T12:00:02+00:00",
                ),
            )

            wait_until(lambda: follower.done)
            self.assertTrue(follower.join(1.0))
            self.assertIn("run paused: run run-1, reason abandoned", out.getvalue())

    def test_tail_follower_suppresses_event_lines_when_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path, events_path = self.setup_tail_files(
                tmp_dir
            )
            stdout_log = tmp_dir / "stdout.log"
            stdout_log.write_text("", encoding="utf-8")
            turn_payload = {
                "run_id": "run-1",
                "session": 1,
                "session_id": "1",
                "total_sessions": 1,
                "turn_index": 1,
                "stdout_path": str(stdout_log),
            }
            self.write_state_payload(
                state_path,
                {
                    "run_id": "run-1",
                    "active_turn": turn_payload,
                    "turns": [turn_payload],
                    "run_finished_at": None,
                },
            )
            out = io.StringIO()
            follower = tail.TailFollower(
                plan_dir,
                state_dir=state_dir,
                out=out,
                poll_interval=0.01,
                include_events=False,
            )
            self.addCleanup(self.cleanup_follower, follower)

            follower.start()
            events.append_event(
                events_path,
                events.Event(
                    name=events.TURN_HEARTBEAT_EVENT,
                    payload={
                        **turn_payload,
                        "elapsed_seconds": 5.0,
                        "idle_seconds": 1.0,
                        "last_activity_at": "2026-06-11T12:00:00+00:00",
                        "last_activity_stream": "stdout",
                    },
                    created_at="2026-06-11T12:00:01+00:00",
                ),
            )
            self.append_log_line(stdout_log, "streamed stdout\n")

            wait_until(
                lambda: "stdout session=1 turn=1 | streamed stdout" in out.getvalue()
            )
            follower.stop()
            self.assertTrue(follower.join(1.0))
            text = out.getvalue()
            self.assertNotIn("heartbeat", text)
            self.assertNotIn("started", text)

    def test_tail_follower_drains_run_finished_when_events_suppressed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path, events_path = self.setup_tail_files(
                tmp_dir
            )
            stdout_log = tmp_dir / "stdout.log"
            stdout_log.write_text("", encoding="utf-8")
            turn_payload = {
                "run_id": "run-1",
                "session": 1,
                "session_id": "1",
                "total_sessions": 1,
                "turn_index": 1,
                "stdout_path": str(stdout_log),
            }
            self.write_state_payload(
                state_path,
                {"run_id": "run-1", "active_turn": turn_payload, "turns": []},
            )
            out = io.StringIO()
            follower = tail.TailFollower(
                plan_dir,
                state_dir=state_dir,
                out=out,
                poll_interval=0.01,
                include_events=False,
            )
            self.addCleanup(self.cleanup_follower, follower)

            follower.start()
            self.append_log_line(stdout_log, "tail line\n")
            events.append_event(
                events_path,
                events.Event(
                    name="run_finished",
                    payload={
                        "run_id": "run-1",
                        "ok": True,
                        "reason": "all-complete",
                        "duration_seconds": 1.0,
                    },
                    created_at="2026-06-11T12:00:02+00:00",
                ),
            )

            wait_until(lambda: follower.done)
            self.assertTrue(follower.join(1.0))
            text = out.getvalue()
            self.assertIn("stdout session=1 turn=1 | tail line", text)
            self.assertNotIn("run finished", text)

    def test_tail_follower_initial_active_replay_emits_recent_logs_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path, events_path = self.setup_tail_files(
                tmp_dir
            )
            stdout_log = tmp_dir / "stdout.log"
            stdout_log.write_text(
                "first line\nsecond line\nthird line\n", encoding="utf-8"
            )
            turn_payload = {
                "run_id": "run-1",
                "session": 1,
                "session_id": "1",
                "total_sessions": 1,
                "turn_index": 2,
                "stdout_path": str(stdout_log),
            }
            self.write_state_payload(
                state_path,
                {
                    "run_id": "run-1",
                    "active_turn": turn_payload,
                    "turns": [turn_payload],
                    "run_finished_at": None,
                },
            )
            out = io.StringIO()
            follower = tail.TailFollower(
                plan_dir,
                state_dir=state_dir,
                out=out,
                poll_interval=0.01,
                include_events=False,
                initial_log_lines=40,
                initial_log_scope="active",
            )
            self.addCleanup(self.cleanup_follower, follower)

            follower.start()
            wait_until(
                lambda: "stdout session=1 turn=2 | third line" in out.getvalue()
            )
            self.append_log_line(stdout_log, "fourth line\n")
            wait_until(
                lambda: "stdout session=1 turn=2 | fourth line" in out.getvalue()
            )
            follower.stop()
            self.assertTrue(follower.join(1.0))
            text = out.getvalue()
            self.assertEqual(
                text.count("stdout session=1 turn=2 | first line"), 1
            )
            self.assertEqual(
                text.count("stdout session=1 turn=2 | second line"), 1
            )
            self.assertEqual(
                text.count("stdout session=1 turn=2 | third line"), 1
            )
            self.assertEqual(
                text.count("stdout session=1 turn=2 | fourth line"), 1
            )

    def test_tail_follower_initial_active_replay_skips_completed_turns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path, events_path = self.setup_tail_files(
                tmp_dir
            )
            old_stdout = tmp_dir / "old-stdout.log"
            active_stdout = tmp_dir / "active-stdout.log"
            old_stdout.write_text("old line\n", encoding="utf-8")
            active_stdout.write_text("active line\n", encoding="utf-8")
            completed_turn = {
                "run_id": "run-1",
                "session": 1,
                "session_id": "1",
                "total_sessions": 2,
                "turn_index": 1,
                "stdout_path": str(old_stdout),
            }
            active_turn = {
                "run_id": "run-1",
                "session": 2,
                "session_id": "2",
                "total_sessions": 2,
                "turn_index": 2,
                "stdout_path": str(active_stdout),
            }
            self.write_state_payload(
                state_path,
                {
                    "run_id": "run-1",
                    "active_turn": active_turn,
                    "turns": [completed_turn, active_turn],
                    "run_finished_at": None,
                },
            )
            out = io.StringIO()
            follower = tail.TailFollower(
                plan_dir,
                state_dir=state_dir,
                out=out,
                poll_interval=0.01,
                include_events=False,
                initial_log_lines=40,
                initial_log_scope="active",
            )
            self.addCleanup(self.cleanup_follower, follower)

            follower.start()
            wait_until(
                lambda: "stdout session=2 turn=2 | active line" in out.getvalue()
            )
            follower.stop()
            self.assertTrue(follower.join(1.0))
            text = out.getvalue()
            self.assertNotIn("old line", text)
            self.assertIn("stdout session=2 turn=2 | active line", text)

    def test_tail_follower_renders_heartbeat_events_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path, events_path = self.setup_tail_files(
                tmp_dir
            )
            stdout_log = tmp_dir / "stdout.log"
            stdout_log.write_text("", encoding="utf-8")
            turn_payload = {
                "run_id": "run-1",
                "session": 1,
                "session_id": "1",
                "total_sessions": 1,
                "turn_index": 1,
                "stdout_path": str(stdout_log),
            }
            self.write_state_payload(
                state_path,
                {
                    "run_id": "run-1",
                    "active_turn": turn_payload,
                    "turns": [turn_payload],
                    "run_finished_at": None,
                },
            )
            out = io.StringIO()
            follower = tail.TailFollower(
                plan_dir,
                state_dir=state_dir,
                out=out,
                poll_interval=0.01,
            )
            self.addCleanup(self.cleanup_follower, follower)

            follower.start()
            events.append_event(
                events_path,
                events.Event(
                    name=events.TURN_HEARTBEAT_EVENT,
                    payload={
                        **turn_payload,
                        "elapsed_seconds": 5.0,
                        "idle_seconds": 1.0,
                        "last_activity_at": "2026-06-11T12:00:00+00:00",
                        "last_activity_stream": "stdout",
                    },
                    created_at="2026-06-11T12:00:01+00:00",
                ),
            )

            wait_until(lambda: "session 1/1 heartbeat" in out.getvalue())
            follower.stop()
            self.assertTrue(follower.join(1.0))
            text = out.getvalue()
            self.assertIn("session 1/1 heartbeat (elapsed", text)
            self.assertIn("stream stdout", text)

    def test_tail_follower_inline_mode_does_not_duplicate_heartbeats(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path, events_path = self.setup_tail_files(
                tmp_dir
            )
            stdout_log = tmp_dir / "stdout.log"
            stdout_log.write_text("", encoding="utf-8")
            turn_payload = {
                "run_id": "run-1",
                "session": 1,
                "session_id": "1",
                "total_sessions": 1,
                "turn_index": 1,
                "stdout_path": str(stdout_log),
            }
            self.write_state_payload(
                state_path,
                {
                    "run_id": "run-1",
                    "active_turn": turn_payload,
                    "turns": [turn_payload],
                    "run_finished_at": None,
                },
            )
            out = io.StringIO()
            follower = tail.TailFollower(
                plan_dir,
                state_dir=state_dir,
                out=out,
                poll_interval=0.01,
                include_events=False,
                initial_log_lines=runner.INLINE_TAIL_INITIAL_LOG_LINES,
                initial_log_scope="active",
            )
            self.addCleanup(self.cleanup_follower, follower)

            follower.start()
            for index in range(3):
                events.append_event(
                    events_path,
                    events.Event(
                        name=events.TURN_HEARTBEAT_EVENT,
                        payload={
                            **turn_payload,
                            "elapsed_seconds": float(index + 1) * 5.0,
                            "idle_seconds": 0.5,
                            "last_activity_at": "2026-06-11T12:00:00+00:00",
                            "last_activity_stream": "stdout",
                        },
                        created_at=f"2026-06-11T12:00:0{index + 1}+00:00",
                    ),
                )
            self.append_log_line(stdout_log, "live stdout line\n")

            wait_until(
                lambda: "stdout session=1 turn=1 | live stdout line" in out.getvalue()
            )
            follower.stop()
            self.assertTrue(follower.join(1.0))
            text = out.getvalue()
            self.assertEqual(0, text.count("heartbeat"))
            self.assertNotIn("event turn_heartbeat", text)
            self.assertIn("stdout session=1 turn=1 | live stdout line", text)

    def test_tail_follower_inline_mode_discovers_later_session_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path, events_path = self.setup_tail_files(
                tmp_dir
            )
            stdout_log_one = tmp_dir / "stdout-1.log"
            stderr_log_one = tmp_dir / "stderr-1.log"
            stdout_log_one.write_text("session 1 startup\n", encoding="utf-8")
            stderr_log_one.write_text("", encoding="utf-8")
            turn_one = {
                "run_id": "run-1",
                "session": 1,
                "session_id": "1",
                "total_sessions": 2,
                "turn_index": 1,
                "stdout_path": str(stdout_log_one),
                "stderr_path": str(stderr_log_one),
            }
            self.write_state_payload(
                state_path,
                {
                    "run_id": "run-1",
                    "active_turn": turn_one,
                    "turns": [turn_one],
                    "run_finished_at": None,
                },
            )
            events.append_event(
                events_path,
                events.Event(
                    name="turn_started",
                    payload=turn_one,
                    created_at="2026-06-11T12:00:00+00:00",
                ),
            )
            out = io.StringIO()
            follower = tail.TailFollower(
                plan_dir,
                state_dir=state_dir,
                out=out,
                poll_interval=0.01,
                include_events=False,
                initial_log_lines=runner.INLINE_TAIL_INITIAL_LOG_LINES,
                initial_log_scope="active",
            )
            self.addCleanup(self.cleanup_follower, follower)

            follower.start()
            wait_until(
                lambda: "stdout session=1 turn=1 | session 1 startup"
                in out.getvalue()
            )

            stdout_log_two = tmp_dir / "stdout-2.log"
            stderr_log_two = tmp_dir / "stderr-2.log"
            stdout_log_two.write_text("session 2 stdout body\n", encoding="utf-8")
            stderr_log_two.write_text("session 2 stderr body\n", encoding="utf-8")
            turn_two = {
                "run_id": "run-1",
                "session": 2,
                "session_id": "2",
                "total_sessions": 2,
                "turn_index": 2,
                "stdout_path": str(stdout_log_two),
                "stderr_path": str(stderr_log_two),
            }
            events.append_event(
                events_path,
                events.Event(
                    name="turn_finished",
                    payload={
                        **turn_one,
                        "status_result": "complete",
                        "returncode": 0,
                        "duration_seconds": 1.0,
                    },
                    created_at="2026-06-11T12:00:01+00:00",
                ),
            )
            events.append_event(
                events_path,
                events.Event(
                    name="turn_started",
                    payload=turn_two,
                    created_at="2026-06-11T12:00:02+00:00",
                ),
            )

            wait_until(
                lambda: (
                    "stdout session=2 turn=2 | session 2 stdout body" in out.getvalue()
                    and "stderr session=2 turn=2 | session 2 stderr body"
                    in out.getvalue()
                )
            )
            self.append_log_line(stdout_log_two, "session 2 stdout follow-up\n")
            wait_until(
                lambda: "stdout session=2 turn=2 | session 2 stdout follow-up"
                in out.getvalue()
            )
            follower.stop()
            self.assertTrue(follower.join(1.0))
            text = out.getvalue()
            self.assertNotIn("session 1/2 started", text)
            self.assertNotIn("session 1/2 finished", text)
            self.assertNotIn("session 2/2 started", text)
            self.assertIn(
                "stdout session=2 turn=2 | session 2 stdout body", text
            )
            self.assertIn(
                "stderr session=2 turn=2 | session 2 stderr body", text
            )
            self.assertIn(
                "stdout session=2 turn=2 | session 2 stdout follow-up", text
            )

    def test_tail_follower_inline_mode_discovers_commit_log_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path, events_path = self.setup_tail_files(
                tmp_dir
            )
            turn_stdout = tmp_dir / "turn-stdout.log"
            turn_stdout.write_text("", encoding="utf-8")
            turn_payload = {
                "run_id": "run-1",
                "session": 1,
                "session_id": "1",
                "total_sessions": 1,
                "turn_index": 1,
                "stdout_path": str(turn_stdout),
            }
            self.write_state_payload(
                state_path,
                {
                    "run_id": "run-1",
                    "active_turn": turn_payload,
                    "turns": [turn_payload],
                    "run_finished_at": None,
                },
            )
            out = io.StringIO()
            follower = tail.TailFollower(
                plan_dir,
                state_dir=state_dir,
                out=out,
                poll_interval=0.01,
                include_events=False,
                initial_log_lines=runner.INLINE_TAIL_INITIAL_LOG_LINES,
                initial_log_scope="active",
            )
            self.addCleanup(self.cleanup_follower, follower)

            follower.start()

            commit_stdout = tmp_dir / "commit-stdout.log"
            commit_stderr = tmp_dir / "commit-stderr.log"
            commit_stdout.write_text("commit stdout body\n", encoding="utf-8")
            commit_stderr.write_text("commit stderr body\n", encoding="utf-8")
            commit_payload = {
                "run_id": "run-1",
                "commit_index": 1,
                "scope": "session",
                "session": 1,
                "session_id": "1",
                "stdout_path": str(commit_stdout),
                "stderr_path": str(commit_stderr),
            }
            events.append_event(
                events_path,
                events.Event(
                    name=events.COMMIT_STARTED_EVENT,
                    payload=commit_payload,
                    created_at="2026-06-11T12:00:01+00:00",
                ),
            )

            wait_until(
                lambda: (
                    "stdout commit=1 scope=session session=1 | commit stdout body"
                    in out.getvalue()
                    and "stderr commit=1 scope=session session=1 | commit stderr body"
                    in out.getvalue()
                )
            )
            self.append_log_line(commit_stdout, "commit stdout follow-up\n")
            wait_until(
                lambda: (
                    "stdout commit=1 scope=session session=1 | commit stdout follow-up"
                    in out.getvalue()
                )
            )
            follower.stop()
            self.assertTrue(follower.join(1.0))
            text = out.getvalue()
            self.assertNotIn("commit 1 started", text)
            self.assertIn(
                "stdout commit=1 scope=session session=1 | commit stdout body", text
            )
            self.assertIn(
                "stderr commit=1 scope=session session=1 | commit stderr body", text
            )
            self.assertIn(
                "stdout commit=1 scope=session session=1 | commit stdout follow-up",
                text,
            )


class FollowEventsTests(unittest.TestCase):
    """The reusable `follow_events` JSONL follower feeds the ASL events tailer."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="asl-follow-events-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = self.tmp / "events.jsonl"

    def _append(self, *lines: str) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            for line in lines:
                handle.write(line + "\n")

    def _record(self, name: str, **payload) -> str:
        return json.dumps({"name": name, "payload": payload})

    def test_missing_file_yields_empty_batches_until_it_appears(self) -> None:
        follower = tail.follow_events(self.path)
        self.assertEqual(next(follower), [])
        self._append(self._record("turn_heartbeat", session_id="1"))
        batch = next(follower)
        self.assertEqual(len(batch), 1)
        self.assertEqual(batch[0]["name"], "turn_heartbeat")

    def test_incremental_append_only_yields_new_records(self) -> None:
        self._append(self._record("turn_started", session_id="1"))
        follower = tail.follow_events(self.path)
        first = next(follower)
        self.assertEqual([r["name"] for r in first], ["turn_started"])
        # Nothing new appended -> empty batch, no re-delivery of prior records.
        self.assertEqual(next(follower), [])
        self._append(self._record("turn_heartbeat", session_id="1", idle_seconds=5))
        second = next(follower)
        self.assertEqual([r["name"] for r in second], ["turn_heartbeat"])

    def test_garbage_and_non_object_lines_are_skipped(self) -> None:
        follower = tail.follow_events(self.path)
        next(follower)
        self._append(
            "",
            "not json at all",
            "[1, 2, 3]",
            "  ",
            self._record("turn_heartbeat", session_id="7"),
        )
        batch = next(follower)
        self.assertEqual([r["name"] for r in batch], ["turn_heartbeat"])
        self.assertEqual(batch[0]["payload"]["session_id"], "7")

    def test_versionless_v1_records_are_surfaced_verbatim(self) -> None:
        follower = tail.follow_events(self.path)
        next(follower)
        # A v1 record carries no `version` key; the follower surfaces it as-is.
        self._append(
            json.dumps({"name": "turn_heartbeat", "payload": {"idle_seconds": 3}})
        )
        batch = next(follower)
        self.assertEqual(len(batch), 1)
        self.assertNotIn("version", batch[0])
        self.assertEqual(batch[0]["payload"]["idle_seconds"], 3)

    def test_partial_trailing_line_is_buffered_until_newline(self) -> None:
        follower = tail.follow_events(self.path)
        next(follower)
        # Write a record with no trailing newline yet (mid-append).
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write('{"name": "turn_heartbeat", "payload": {"idle_seconds"')
        self.assertEqual(next(follower), [])
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(": 9}}\n")
        batch = next(follower)
        self.assertEqual(len(batch), 1)
        self.assertEqual(batch[0]["payload"]["idle_seconds"], 9)

    def test_truncation_reopens_from_start(self) -> None:
        # Several records first, so the read offset is well past the length of
        # the short line the file is later truncated to.
        self._append(
            *(self._record("turn_heartbeat", session_id="1", idle_seconds=n)
              for n in range(5))
        )
        follower = tail.follow_events(self.path)
        next(follower)
        # Rewrite the file smaller than the prior read offset (truncation).
        self.path.write_text(
            self._record("run_paused", reason="x") + "\n",
            encoding="utf-8",
        )
        batch = next(follower)
        self.assertEqual([r["name"] for r in batch], ["run_paused"])

    def test_rotation_to_a_new_inode_reopens_from_start(self) -> None:
        self._append(self._record("turn_heartbeat", session_id="1"))
        follower = tail.follow_events(self.path)
        next(follower)
        # Replace the file with a brand-new inode (rotation).
        rotated = self.tmp / "events.next"
        rotated.write_text(
            self._record("turn_heartbeat", session_id="2", idle_seconds=1)
            + "\n"
            + self._record("turn_heartbeat", session_id="2", idle_seconds=2)
            + "\n",
            encoding="utf-8",
        )
        os.replace(rotated, self.path)
        batch = next(follower)
        self.assertEqual([r["payload"]["session_id"] for r in batch], ["2", "2"])


if __name__ == "__main__":
    unittest.main()
