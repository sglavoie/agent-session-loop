"""Tail follower tests.

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


if __name__ == "__main__":
    unittest.main()
