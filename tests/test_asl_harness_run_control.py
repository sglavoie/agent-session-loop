"""Run-control stop requests, events and callbacks."""

from __future__ import annotations

import argparse
import io
import os
import tempfile
from contextlib import redirect_stderr
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    control_queue,
    events,
    harnesses,
    run_control,
    run_loop,
    run_selection,
    state,
)
from tests.harness_integration_base import FakeHarnessIntegrationBase
from tests.support import (
    REPO_ROOT,
    FakeRunControlListener,
    init_git_repo,
    patched_env,
    read_jsonl,
)


class RunControlTests(FakeHarnessIntegrationBase):
    def test_run_control_stop_after_current_stops_between_sessions(self) -> None:
        listener = FakeRunControlListener(stop_requested=True)
        exit_code, payload, _plan_dir, _state_dir, _fake_codex = (
            self.run_locked_with_listener(
                mode="success",
                statuses=("pending", "pending"),
                listener=listener,
            )
        )

        self.assertEqual(0, exit_code)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])
        self.assertEqual("stopped", payload["reason"])
        self.assertEqual([1], payload["completed_sessions"])
        self.assertEqual(2, payload["final_snapshot"]["next_pending"])
        self.assertEqual(1, len(payload["turns"]))
        self.assertEqual(1, listener.start_calls)
        self.assertEqual(1, listener.stop_calls)
        self.assertEqual(1, listener.consume_calls)

    def test_run_control_events_record_actions_and_final_control_payload(self) -> None:
        listener = FakeRunControlListener(
            stop_requested=True,
            actions_on_start=(
                run_control.RunControlAction(
                    action="request",
                    stop_after_current=True,
                ),
                run_control.RunControlAction(
                    action="cancel",
                    stop_after_current=False,
                ),
                run_control.RunControlAction(
                    action="request",
                    stop_after_current=True,
                ),
            ),
        )

        exit_code, payload, _plan_dir, _state_dir, _fake_codex = (
            self.run_locked_with_listener(
                mode="success",
                statuses=("pending", "pending"),
                listener=listener,
            )
        )

        self.assertEqual(0, exit_code)
        self.assertEqual("stopped", payload["reason"])
        self.assertEqual(
            {
                "stop_after_current": False,
                "abort_turn_requested": False,
                "abort_grace_seconds": None,
                "abort_reason": None,
                "abort_deadline": None,
                "tail_enabled": False,
                "next_harness": None,
                "target_harness": None,
                "changed_fields": [],
                "implementation_overrides": {},
                "guards": [],
                "consumed": True,
                "last_action": "stop-after-current-consumed",
            },
            payload["control"],
        )

        event_records = read_jsonl(Path(payload["events_path"]))
        control_events = [
            record["payload"]
            for record in event_records
            if record["name"] == events.RUN_CONTROL_EVENT
        ]
        self.assertEqual(
            [
                "stop-after-current-requested",
                "stop-after-current-canceled",
                "stop-after-current-requested",
                "stop-after-current-consumed",
            ],
            [event["action"] for event in control_events],
        )
        self.assertEqual(
            [True, False, True, False],
            [event["stop_after_current"] for event in control_events],
        )
        self.assertEqual(
            [
                "stop-after-current requested",
                "stop-after-current canceled",
                "stop-after-current requested",
                "stop-after-current consumed",
            ],
            [event["message"] for event in control_events],
        )
        for event in control_events:
            self.assertEqual(payload["run_id"], event["run_id"])

        consumed_event = control_events[-1]
        self.assertEqual(1, consumed_event["session"])
        self.assertEqual(1, consumed_event["turn_index"])
        self.assertEqual(2, consumed_event["total_sessions"])
        run_finished = event_records[self.event_index(event_records, "run_finished")]
        self.assertEqual(payload["control"], run_finished["payload"]["control"])

    def test_external_queue_stop_after_current_is_honored_between_sessions(
        self,
    ) -> None:
        listener = FakeRunControlListener(current_harness="codex")
        calls: list[dict[str, Any]] = []
        plan_dir_box: list[Path] = []
        state_dir_box: list[Path] = []

        def complete_next_pending(plan_dir: Path) -> None:
            for session_file in sorted(plan_dir.glob("session-*.md")):
                text = session_file.read_text(encoding="utf-8")
                if "- **Status**: pending" not in text:
                    continue
                session_file.write_text(
                    text.replace(
                        "- **Status**: pending",
                        "- **Status**: complete",
                        1,
                    ),
                    encoding="utf-8",
                )
                return

        def fake_run_harness(
            name: str,
            **kwargs: object,
        ) -> harnesses.HarnessResult:
            calls.append({"harness": name, **kwargs})
            plan_dir = Path(kwargs["plan_dir"])
            complete_next_pending(plan_dir)
            if len(calls) == 1:
                # Workbench-style external producer queues a stop request
                # while session 1 is in flight.
                control_queue.append_command(
                    plan_dir_box[0],
                    control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT,
                    state_dir=state_dir_box[0],
                    command_id="ext-stop-1",
                )
            return harnesses.HarnessResult(
                name=name,
                argv=[name, str(kwargs["prompt"])],
                returncode=0,
                stdout=f"{name} completed\n",
                stderr="",
            )

        # Pre-resolve the plan_dir/state_dir the helper will create so the
        # fake harness can append to the queue on its first call.
        plan_dir, state_dir, config_home, _fake_codex = self.setup_loop(
            statuses=("pending", "pending"),
        )
        plan_dir_box.append(plan_dir)
        state_dir_box.append(state_dir)

        with mock.patch.object(
            harnesses,
            "run_harness",
            side_effect=fake_run_harness,
        ):
            args = argparse.Namespace(
                plan_dir=str(plan_dir),
                cwd=str(REPO_ROOT),
                harness="codex",
                state_dir=str(state_dir),
                json=False,
                dry_run=False,
                unsafe_bypass=True,
                timeout=None,
                max_sessions=None,
                commit_mode=None,
                harness_model=None,
                effort=None,
                note=None,
                allow_not_loop_ready=True,
            )

            real_state_dir = state.plan_state_dir(plan_dir, state_dir)
            listener.state_path = real_state_dir / events.STATE_FILE_NAME
            listener.events_path = real_state_dir / events.EVENTS_FILE_NAME

            def fake_for_run(*_args, **kwargs):
                listener.on_action = kwargs.get("on_action")
                listener.on_status = kwargs.get("on_status")
                return listener

            with (
                patched_env(
                    XDG_CONFIG_HOME=str(config_home),
                    FAKE_CODEX_MODE="success",
                    FAKE_CODEX_SLEEP="5",
                    PYTHONPATH=str(REPO_ROOT)
                    + os.pathsep
                    + os.environ.get("PYTHONPATH", ""),
                ),
                mock.patch.object(
                    run_control.RunControlListener,
                    "for_run",
                    side_effect=fake_for_run,
                ),
            ):
                lock_info = state.acquire_lock(plan_dir, state_dir)
                try:
                    exit_code, payload = run_loop.run_locked(
                        args=args,
                        cwd=REPO_ROOT,
                        timeout=None,
                        max_sessions=None,
                        commit_mode=None,
                        lock_info=lock_info,
                        run_id="test-run",
                    )
                finally:
                    state.release_lock(plan_dir, state_dir)

        self.assertEqual(0, exit_code, payload)
        self.assertEqual("stopped", payload["reason"])
        # Only session 1 ran; session 2 was not started.
        self.assertEqual([1], payload["completed_sessions"])
        self.assertEqual(1, len(calls))

        control_events = [
            record["payload"]
            for record in read_jsonl(Path(payload["events_path"]))
            if record["name"] == events.RUN_CONTROL_EVENT
        ]
        actions = [event["action"] for event in control_events]
        self.assertIn("stop-after-current-requested", actions)
        self.assertIn("stop-after-current-consumed", actions)
        # The requested event must precede the consumed event so the queue
        # transition is recorded before the lifecycle ack.
        self.assertLess(
            actions.index("stop-after-current-requested"),
            actions.index("stop-after-current-consumed"),
        )

        # Queue file is left truncated, not deleted, after drain.
        queue_path = control_queue.queue_path_for_plan(plan_dir, state_dir)
        self.assertTrue(queue_path.exists())
        self.assertEqual("", queue_path.read_text(encoding="utf-8"))

    def test_run_control_starts_after_state_and_event_files_exist(self) -> None:
        listener = FakeRunControlListener()

        exit_code, payload, _plan_dir, _state_dir, _fake_codex = (
            self.run_locked_with_listener(
                mode="success",
                statuses=("pending",),
                listener=listener,
            )
        )

        self.assertEqual(0, exit_code)
        self.assertEqual("all-complete", payload["reason"])
        self.assertEqual(1, listener.start_calls)
        self.assertTrue(listener.state_exists_at_start)
        self.assertTrue(listener.events_exists_at_start)

    def test_run_control_status_callback_prints_without_recording_event(
        self,
    ) -> None:
        listener = FakeRunControlListener(
            status_on_start=(
                run_control.RunControlAction(
                    action="status",
                    stop_after_current=False,
                    tail_enabled=True,
                    next_harness="claude",
                ),
            ),
        )
        stderr = io.StringIO()

        with redirect_stderr(stderr):
            exit_code, payload, _plan_dir, _state_dir, _fake_codex = (
                self.run_locked_with_listener(
                    mode="success",
                    statuses=("pending",),
                    listener=listener,
                )
            )

        self.assertEqual(0, exit_code)
        stderr_text = stderr.getvalue()
        self.assertIn(
            "status: no active turn; next session 1/1 turn 1; "
            "sessions 0/1 complete, 1 pending",
            stderr_text,
        )
        self.assertIn("harness active codex; future claude", stderr_text)
        self.assertIn("stop-after-current off; live tail on", stderr_text)
        self.assertEqual(
            {
                "stop_after_current": False,
                "abort_turn_requested": False,
                "abort_grace_seconds": None,
                "abort_reason": None,
                "abort_deadline": None,
                "tail_enabled": False,
                "next_harness": None,
                "target_harness": None,
                "changed_fields": [],
                "implementation_overrides": {},
                "guards": [],
                "consumed": False,
                "last_action": None,
            },
            payload["control"],
        )
        control_events = [
            record
            for record in read_jsonl(Path(payload["events_path"]))
            if record["name"] == events.RUN_CONTROL_EVENT
        ]
        self.assertEqual([], control_events)

    def test_run_control_final_session_keeps_all_complete_reason(self) -> None:
        listener = FakeRunControlListener(stop_requested=True)

        exit_code, payload, _plan_dir, _state_dir, _fake_codex = (
            self.run_locked_with_listener(
                mode="success",
                statuses=("pending",),
                listener=listener,
            )
        )

        self.assertEqual(0, exit_code)
        self.assertTrue(payload["ok"])
        self.assertEqual("all-complete", payload["reason"])
        self.assertEqual([1], payload["completed_sessions"])
        self.assertIsNone(payload["final_snapshot"]["next_pending"])
        # Without a review harness nothing follows the last session, so the
        # request is never honored — and therefore never consumed.
        self.assertEqual(0, listener.consume_calls)
        self.assertTrue(listener.state.stop_after_current)

    def test_run_control_stop_after_current_waits_for_per_session_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            repo_dir = tmp_dir / "repo"
            init_git_repo(repo_dir)
            commit_log, commit_env = self.setup_commit_harness(tmp_dir)
            listener = FakeRunControlListener(stop_requested=True)

            exit_code, payload, _plan_dir, _state_dir, _fake_codex = (
                self.run_locked_with_listener(
                    mode="success",
                    statuses=("pending", "pending"),
                    listener=listener,
                    cwd=repo_dir,
                    commit_mode=run_selection.COMMIT_MODE_PER_SESSION,
                    extra_env=commit_env,
                )
            )
            commit_records = read_jsonl(commit_log)

        self.assertEqual(0, exit_code)
        self.assertEqual("stopped", payload["reason"])
        self.assertEqual([1], payload["completed_sessions"])
        self.assertEqual(1, len(payload["commits"]))
        self.assertTrue(payload["commits"][0]["success"])
        self.assertEqual(1, len(commit_records))
        event_records = read_jsonl(Path(payload["events_path"]))
        commit_finished = self.event_index(
            event_records,
            events.COMMIT_FINISHED_EVENT,
            session=1,
            scope="session",
        )
        consumed = self.event_index(
            event_records,
            events.RUN_CONTROL_EVENT,
            action="stop-after-current-consumed",
        )
        self.assertLess(commit_finished, consumed)

    def test_run_control_stop_request_does_not_override_harness_failure(self) -> None:
        listener = FakeRunControlListener(stop_requested=True)

        exit_code, payload, _plan_dir, _state_dir, _fake_codex = (
            self.run_locked_with_listener(
                mode="failure",
                statuses=("pending", "pending"),
                listener=listener,
            )
        )

        self.assertEqual(1, exit_code)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["paused"])
        self.assertEqual("nonzero-exit", payload["reason"])
        self.assertEqual("nonzero-exit", payload["pause_reason"])
        self.assertEqual([], payload["completed_sessions"])
        self.assertEqual(0, listener.consume_calls)
        self.assertEqual(1, listener.stop_calls)
