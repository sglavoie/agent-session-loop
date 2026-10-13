"""Human progress output, tailing and live-turn heartbeats."""

from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
import tempfile
from contextlib import redirect_stderr
from pathlib import Path

from agent_session_loop import (
    clock,
    events,
    plans_files,
    plans_snapshot,
    run_commit,
    run_payloads,
    run_persistence,
    run_reporting,
    run_selection,
    state,
)
from tests.harness_integration_base import FakeHarnessIntegrationBase
from tests.harness_integration_support import (
    LOOP_RUN_TIMEOUT_SECONDS,
    RUN_EXIT_TIMEOUT_SECONDS,
    STREAM_GATE_TIMEOUT_SECONDS,
)
from tests.support import (
    REPO_ROOT,
    wait_until,
    write_plan,
)


class ProgressAndHeartbeatTests(FakeHarnessIntegrationBase):
    def test_text_run_emits_human_progress_to_stderr(self) -> None:
        plan_dir, state_dir, config_home, _fake_codex = self.setup_loop(
            statuses=("pending",)
        )

        completed = subprocess.run(
            self.run_command(plan_dir=plan_dir, state_dir=state_dir),
            cwd=REPO_ROOT,
            env=self.loop_env(
                config_home=config_home,
                mode="streaming-success",
                extra={"FAKE_CODEX_STEP_SLEEP": "0.05"},
            ),
            text=True,
            capture_output=True,
            timeout=LOOP_RUN_TIMEOUT_SECONDS,
            check=False,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("Plan:", completed.stdout)
        self.assertIn("Completed session 1", completed.stdout)
        self.assertIn("Run complete: 1 session(s) completed", completed.stdout)
        timestamp = r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] "
        self.assertRegex(
            completed.stderr,
            timestamp + r"run started: harness codex; run ",
        )
        self.assertIn("run dir ", completed.stderr)
        self.assertIn("state ", completed.stderr)
        self.assertIn("events ", completed.stderr)
        self.assertRegex(
            completed.stderr,
            timestamp + r"session 1/1 started",
        )
        self.assertRegex(
            completed.stderr,
            timestamp
            + r"session 1/1 finished "
            + r"\(status complete, return code 0, duration [^,]+, total [^)]+\)",
        )
        self.assertIn(
            "session 1/1 finished (status complete, return code 0, duration ",
            completed.stderr,
        )
        self.assertIn("run finished: run ", completed.stderr)
        self.assertIn("reason all-complete", completed.stderr)
        self.assertNotIn("stdout=", completed.stderr)
        self.assertNotIn("stderr=", completed.stderr)
        self.assertNotIn("result=", completed.stderr)

    def test_run_note_is_persisted_and_printed_on_completion(self) -> None:
        plan_dir, state_dir, config_home, _fake_codex = self.setup_loop(
            statuses=("pending",)
        )
        note = "operator reference ABC-123"

        completed = subprocess.run(
            self.run_command(
                plan_dir=plan_dir,
                state_dir=state_dir,
                note=note,
            ),
            cwd=REPO_ROOT,
            env=self.loop_env(config_home=config_home, mode="success"),
            text=True,
            capture_output=True,
            timeout=LOOP_RUN_TIMEOUT_SECONDS,
            check=False,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn(f"Note: {note}", completed.stdout)
        self.assertIn(f"note: {note}", completed.stderr)
        payload = self.read_run_payload(plan_dir=plan_dir, state_dir=state_dir)
        self.assertTrue(payload["ok"])
        self.assertEqual("all-complete", payload["reason"])
        self.assertEqual(note, payload["note"])

    def test_text_run_emits_final_paused_progress_line(self) -> None:
        plan_dir, state_dir, config_home, _fake_codex = self.setup_loop(
            statuses=("pending",)
        )

        completed = subprocess.run(
            self.run_command(plan_dir=plan_dir, state_dir=state_dir),
            cwd=REPO_ROOT,
            env=self.loop_env(config_home=config_home, mode="failure"),
            text=True,
            capture_output=True,
            timeout=LOOP_RUN_TIMEOUT_SECONDS,
            check=False,
        )

        self.assertEqual(1, completed.returncode)
        self.assertIn("Run paused: nonzero-exit", completed.stderr)
        self.assertRegex(
            completed.stderr,
            r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] "
            r"run paused: run [^,]+, "
            r"reason nonzero-exit, duration \d+\.\ds",
        )

    def test_json_run_stays_quiet_and_status_reports_active_turn(self) -> None:
        plan_dir, state_dir, config_home, _fake_codex = self.setup_loop(
            statuses=("pending",)
        )
        gate_dir = state_dir / "active-turn-stream-gates"
        run_process = subprocess.Popen(
            self.run_command(plan_dir=plan_dir, state_dir=state_dir, json_output=True),
            cwd=REPO_ROOT,
            env=self.loop_env(
                config_home=config_home,
                mode="streaming-success",
                # Two cold `python -m agent_session_loop status` subprocesses
                # have to observe the turn while it is still in flight. Holding
                # the harness at the stream gate makes that window explicit
                # rather than racing a step sleep, which a machine saturated by
                # the parallel shards can outlast at any value.
                extra={
                    "FAKE_CODEX_STEP_SLEEP": "0",
                    "FAKE_CODEX_STREAM_GATE_DIR": str(gate_dir),
                    "FAKE_CODEX_STREAM_GATE_TIMEOUT": str(STREAM_GATE_TIMEOUT_SECONDS),
                },
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        gate_continue_path = gate_dir / "session-1.continue"

        def cleanup_run_process() -> None:
            # Release the gate first: a failed assertion above leaves the
            # harness parked there, and killing the run while it waits would
            # report a wedged child instead of the assertion that failed.
            gate_dir.mkdir(parents=True, exist_ok=True)
            gate_continue_path.write_text("continue\n", encoding="utf-8")
            if run_process.poll() is None:
                run_process.kill()
            try:
                run_process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                run_process.kill()
                run_process.communicate(timeout=5)

        self.addCleanup(cleanup_run_process)

        state_path = state.plan_state_dir(plan_dir, state_dir) / events.STATE_FILE_NAME

        def active_turn_payload():
            if not (gate_dir / "session-1.ready").exists():
                return None
            if not state_path.exists():
                return None
            raw = json.loads(state_path.read_text(encoding="utf-8"))
            active_turn = raw.get("active_turn")
            return active_turn if isinstance(active_turn, dict) else None

        active_turn = wait_until(
            active_turn_payload, timeout=STREAM_GATE_TIMEOUT_SECONDS
        )
        self.assertEqual(1, active_turn["session"])
        self.assertEqual(1, active_turn["turn_index"])
        self.assertTrue(active_turn["stdout_path"].endswith("/stdout.log"))
        self.assertTrue(active_turn["stderr_path"].endswith("/stderr.log"))

        status_completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "agent_session_loop",
                "status",
                str(plan_dir),
                "--state-dir",
                str(state_dir),
                "--json",
            ],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        status_payload = json.loads(status_completed.stdout)
        self.assertTrue(status_payload["ok"])
        self.assertEqual(1, status_payload["active_turn"]["session"])
        self.assertIn("elapsed_seconds", status_payload["active_turn"])
        self.assertIn("stdout_path", status_payload["active_turn"])

        status_text = subprocess.run(
            [
                sys.executable,
                "-m",
                "agent_session_loop",
                "status",
                str(plan_dir),
                "--state-dir",
                str(state_dir),
            ],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertIn("Active turn: session 1 turn 1", status_text.stdout)
        self.assertIn("Stdout:", status_text.stdout)
        self.assertIn("Stderr:", status_text.stdout)

        gate_continue_path.write_text("continue\n", encoding="utf-8")
        run_stdout, run_stderr = run_process.communicate(
            timeout=RUN_EXIT_TIMEOUT_SECONDS
        )
        self.assertEqual("", run_stderr)
        run_payload = json.loads(run_stdout)
        self.assertTrue(run_payload["ok"])
        self.assertEqual([1], run_payload["completed_sessions"])

    def test_tail_follows_streaming_logs_across_sessions(self) -> None:
        plan_dir, state_dir, config_home, _fake_codex = self.setup_loop(
            statuses=("pending", "pending")
        )
        run_process = subprocess.Popen(
            self.run_command(plan_dir=plan_dir, state_dir=state_dir, json_output=True),
            cwd=REPO_ROOT,
            env=self.loop_env(
                config_home=config_home,
                mode="streaming-success",
                extra={"FAKE_CODEX_STEP_SLEEP": "0.15"},
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(
            lambda: run_process.kill() if run_process.poll() is None else None
        )

        events_path = (
            state.plan_state_dir(plan_dir, state_dir) / events.EVENTS_FILE_NAME
        )

        def has_first_turn_started():
            if not events_path.exists():
                return False
            return "turn_started" in events_path.read_text(encoding="utf-8")

        wait_until(has_first_turn_started)
        tail_process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "agent_session_loop",
                "tail",
                str(plan_dir),
                "--state-dir",
                str(state_dir),
                "--lines",
                "200",
            ],
            cwd=REPO_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(
            lambda: tail_process.kill() if tail_process.poll() is None else None
        )

        run_stdout, run_stderr = run_process.communicate(
            timeout=RUN_EXIT_TIMEOUT_SECONDS
        )
        tail_stdout, tail_stderr = tail_process.communicate(
            timeout=RUN_EXIT_TIMEOUT_SECONDS
        )
        self.assertEqual("", run_stderr)
        run_payload = json.loads(run_stdout)
        self.assertTrue(run_payload["ok"])
        self.assertEqual([1, 2], run_payload["completed_sessions"])

        self.assertEqual(0, tail_process.returncode, tail_stderr)
        self.assertEqual("", tail_stderr)
        timestamp = r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] "
        self.assertRegex(
            tail_stdout,
            timestamp + r"session 1/2 started",
        )
        self.assertIn("session 1/2 finished", tail_stdout)
        self.assertIn("session 2/2 started", tail_stdout)
        self.assertIn("run finished: run ", tail_stdout)
        self.assertNotIn("event turn_started", tail_stdout)
        self.assertIn(
            "stdout session=1 turn=1 | session 1 stdout before completion", tail_stdout
        )
        self.assertIn(
            "stderr session=1 turn=1 | session 1 stderr before completion", tail_stdout
        )
        self.assertIn(
            "stdout session=2 turn=2 | session 2 stdout before completion", tail_stdout
        )
        self.assertIn(
            "stderr session=2 turn=2 | session 2 stderr after completion", tail_stdout
        )

    def test_live_turn_heartbeat_updates_state_events_and_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            event_path = tmp_dir / events.EVENTS_FILE_NAME
            state_path = tmp_dir / events.STATE_FILE_NAME
            payload = {"state_path": str(state_path), "active_turn": None}
            active_turn = {
                "run_id": "run-1",
                "turn_index": 1,
                "session": 1,
                "total_sessions": 3,
                "harness": "codex",
                "artifact_dir": str(tmp_dir / "turn"),
                "stdout_path": str(tmp_dir / "stdout.log"),
                "stderr_path": str(tmp_dir / "stderr.log"),
                "last_message_path": str(tmp_dir / "last-message.txt"),
                "result_path": str(tmp_dir / "result.json"),
                "started_at": clock.isoformat(clock.utc_now()),
                "finished_at": None,
                "duration_seconds": None,
                "idle_seconds_since_previous_turn": 0.0,
                "last_activity_at": None,
                "last_activity_stream": None,
                "last_activity_preview": "",
                "last_heartbeat_at": None,
                "elapsed_seconds": 0.0,
                "idle_seconds": 0.0,
                "heartbeat_count": 0,
                "stdout_chars": 0,
                "stderr_chars": 0,
                "action_required": None,
            }
            progress = run_reporting._ProgressReporter(enabled=True)
            stderr = io.StringIO()

            live_turn = run_commit._LiveTurn(
                payload=payload,
                event_path=event_path,
                progress=progress,
                active_turn=active_turn,
                heartbeat_interval=0.01,
            )
            with redirect_stderr(stderr):
                live_turn.start()
                wait_until(
                    lambda: (
                        event_path.exists()
                        and events.TURN_HEARTBEAT_EVENT
                        in event_path.read_text(encoding="utf-8")
                    )
                )
                live_turn.stop()
                live_turn.finish(clock.utc_now())

            state_payload = json.loads(state_path.read_text(encoding="utf-8"))
            event_records = [
                json.loads(line)
                for line in event_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertIsNone(state_payload["active_turn"])
            self.assertTrue(
                any(
                    record["name"] == events.TURN_HEARTBEAT_EVENT
                    for record in event_records
                )
            )
            heartbeat_payloads = [
                record["payload"]
                for record in event_records
                if record["name"] == events.TURN_HEARTBEAT_EVENT
            ]
            self.assertEqual(
                {"codex"}, {payload["harness"] for payload in heartbeat_payloads}
            )
            stderr_text = stderr.getvalue()
            timestamp = r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] "
            self.assertRegex(
                stderr_text,
                timestamp + r"session 1/3 started",
            )
            self.assertRegex(
                stderr_text,
                timestamp + r"session 1/3 heartbeat \(elapsed",
            )

    def test_initial_and_live_turn_state_include_active_verification_key(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            write_plan(plan_dir, ("pending",))
            snapshot = plans_snapshot.inspect_plan(plan_dir)
            plan_state_dir = state.plan_state_dir(plan_dir, state_dir)
            run_id = "run-1"
            run_dir = plan_state_dir / "runs" / run_id
            event_path = plan_state_dir / events.EVENTS_FILE_NAME
            state_path = plan_state_dir / events.STATE_FILE_NAME
            lock_info = state.LockInfo(
                path=plan_state_dir / state.LOCK_FILE_NAME,
                pid=123,
                plan_dir=str(plans_files.canonicalize_path(plan_dir)),
                created_at="2026-06-20T10:00:00+00:00",
                stale=False,
                reason="active",
                raw={},
            )
            args = argparse.Namespace(
                dry_run=False,
                review_harness=None,
                harness="codex",
                harness_model=None,
                effort=None,
                unsafe_bypass=True,
                no_mailbox=False,
                note=None,
                allow_not_loop_ready=False,
            )
            mailbox_activation = run_selection._mailbox_activation_payload(
                args, plan_dir
            )
            payload = run_payloads._initial_payload(
                args=args,
                snapshot=snapshot,
                state_dir=plan_state_dir,
                run_dir=run_dir,
                event_path=event_path,
                state_path=state_path,
                lock_info=lock_info,
                cwd=tmp_dir,
                run_id=run_id,
                commit_mode=None,
                mailbox_activation=mailbox_activation,
            )
            persistence = run_persistence._RunPersistence()

            persistence.write_state(payload)
            initial_state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertIn("active_verification", initial_state)
            self.assertIsNone(initial_state["active_verification"])
            self.assertFalse(initial_state["mailbox_enabled"])
            self.assertEqual(
                mailbox_activation,
                initial_state["mailbox_activation"],
            )

            active_turn = {
                "run_id": run_id,
                "turn_index": 1,
                "session": 1,
                "session_id": "1",
                "total_sessions": 1,
                "harness": "codex",
                "artifact_dir": str(run_dir / "turn-0001-session-1"),
                "stdout_path": str(run_dir / "turn-0001-session-1" / "stdout.log"),
                "stderr_path": str(run_dir / "turn-0001-session-1" / "stderr.log"),
                "last_message_path": str(
                    run_dir / "turn-0001-session-1" / "last-message.txt"
                ),
                "result_path": str(run_dir / "turn-0001-session-1" / "result.json"),
                "started_at": clock.isoformat(clock.utc_now()),
                "finished_at": None,
                "duration_seconds": None,
                "idle_seconds_since_previous_turn": 0.0,
                "last_activity_at": None,
                "last_activity_stream": None,
                "last_activity_preview": "",
                "last_heartbeat_at": None,
                "elapsed_seconds": 0.0,
                "idle_seconds": 0.0,
                "heartbeat_count": 0,
                "stdout_chars": 0,
                "stderr_chars": 0,
                "action_required": None,
            }
            live_turn = run_commit._LiveTurn(
                payload=payload,
                event_path=event_path,
                progress=run_reporting._ProgressReporter(enabled=False),
                active_turn=active_turn,
                persistence=persistence,
                heartbeat_interval=60,
            )
            try:
                live_turn.start()
                active_state = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertIsInstance(active_state["active_turn"], dict)
                self.assertIn("active_verification", active_state)
                self.assertIsNone(active_state["active_verification"])
            finally:
                live_turn.stop()
                live_turn.finish(clock.utc_now())
