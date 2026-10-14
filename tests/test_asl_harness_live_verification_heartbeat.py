"""Live verification heartbeats and check round trips."""

from __future__ import annotations

import io
import json
import tempfile
from contextlib import redirect_stderr
from pathlib import Path

from agent_session_loop import (
    events,
    live_verification,
    run_persistence,
    run_reporting,
    verification_models,
)
from tests.harness_integration_base import FakeHarnessIntegrationBase
from tests.support import (
    read_jsonl,
    wait_until,
)


class LiveVerificationHeartbeatTests(FakeHarnessIntegrationBase):
    def test_live_verification_heartbeat_updates_state_events_and_progress(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            event_path = tmp_dir / events.EVENTS_FILE_NAME
            state_path = tmp_dir / events.STATE_FILE_NAME
            payload = {
                "state_path": str(state_path),
                "active_turn": None,
                "active_verification": None,
            }
            persistence = run_persistence._RunPersistence()
            progress = run_reporting._ProgressReporter(enabled=True)
            stderr = io.StringIO()

            live = live_verification._LiveVerification(
                event_path=event_path,
                payload=payload,
                progress=progress,
                persistence=persistence,
                run_id="run-1",
                turn_index=1,
                session_id="1",
                session_label="1/2",
                total_sessions=2,
                check_count=2,
                check_names=("pytest", "focused"),
                check_kinds=("command", "command"),
                heartbeat_interval=0.01,
            )

            with redirect_stderr(stderr):
                live.start()
                live.check_started(
                    check_index=0,
                    name="pytest",
                    kind="command",
                    argv=("pytest", "-q"),
                    cwd="/tmp/repo",
                )
                wait_until(
                    lambda: (
                        event_path.exists()
                        and events.VERIFICATION_HEARTBEAT_EVENT
                        in event_path.read_text(encoding="utf-8")
                    )
                )
                live.check_finished(
                    check_index=0,
                    name="pytest",
                    kind="command",
                    role=verification_models.CHECK_ROLE_GATE,
                    status="pass",
                    duration_seconds=1.25,
                    failure_reason=None,
                    returncode=0,
                )
                live.check_skipped(
                    check_index=1,
                    name="focused",
                    kind="command",
                    on_failure_of=("pytest",),
                    reason="condition-not-met",
                )
                live.finish(
                    passed=True,
                    passed_count=1,
                    reason="ok",
                    skipped_count=1,
                    gate_count=1,
                    diagnostic_count=1,
                )

            state_payload = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertIsNone(state_payload["active_verification"])

            event_records = read_jsonl(event_path)
            event_names = [record["name"] for record in event_records]
            self.assertIn(events.VERIFICATION_STARTED_EVENT, event_names)
            self.assertIn(events.VERIFICATION_CHECK_STARTED_EVENT, event_names)
            self.assertIn(events.VERIFICATION_HEARTBEAT_EVENT, event_names)
            self.assertIn(events.VERIFICATION_CHECK_FINISHED_EVENT, event_names)
            self.assertIn(events.VERIFICATION_CHECK_SKIPPED_EVENT, event_names)
            self.assertIn(events.VERIFICATION_FINISHED_EVENT, event_names)

            started_payload = next(
                record["payload"]
                for record in event_records
                if record["name"] == events.VERIFICATION_STARTED_EVENT
            )
            self.assertEqual(2, started_payload["check_count"])
            self.assertEqual(["pytest", "focused"], started_payload["check_names"])
            self.assertEqual("1/2", started_payload["session_label"])

            check_started_payload = next(
                record["payload"]
                for record in event_records
                if record["name"] == events.VERIFICATION_CHECK_STARTED_EVENT
            )
            self.assertEqual(0, check_started_payload["check_index"])
            self.assertEqual("pytest", check_started_payload["name"])
            self.assertEqual(["pytest", "-q"], check_started_payload["argv"])
            self.assertEqual("/tmp/repo", check_started_payload["cwd"])

            check_finished_payload = next(
                record["payload"]
                for record in event_records
                if record["name"] == events.VERIFICATION_CHECK_FINISHED_EVENT
            )
            self.assertEqual("pass", check_finished_payload["status"])
            self.assertEqual(1.25, check_finished_payload["duration_seconds"])
            self.assertEqual(0, check_finished_payload["returncode"])
            self.assertIsNone(check_finished_payload["failure_reason"])

            check_skipped_payload = next(
                record["payload"]
                for record in event_records
                if record["name"] == events.VERIFICATION_CHECK_SKIPPED_EVENT
            )
            self.assertEqual("focused", check_skipped_payload["name"])
            self.assertEqual(["pytest"], check_skipped_payload["on_failure_of"])
            self.assertEqual("condition-not-met", check_skipped_payload["reason"])

            finished_payload = next(
                record["payload"]
                for record in event_records
                if record["name"] == events.VERIFICATION_FINISHED_EVENT
            )
            self.assertTrue(finished_payload["passed"])
            self.assertEqual("ok", finished_payload["reason"])
            self.assertEqual(1, finished_payload["passed_count"])
            self.assertEqual(1, finished_payload["skipped_count"])
            self.assertEqual(1, finished_payload["executed_count"])
            self.assertEqual(1, finished_payload["gate_count"])
            self.assertEqual(1, finished_payload["diagnostic_count"])

            stderr_text = stderr.getvalue()
            timestamp = r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] "
            self.assertRegex(
                stderr_text,
                timestamp + r"verification started for session 1/2 turn 1 \(2 checks\)",
            )
            self.assertRegex(
                stderr_text,
                timestamp + r"verification check 1/2 started: pytest \(command\)",
            )
            self.assertRegex(
                stderr_text,
                timestamp
                + r"verification heartbeat for session 1/2 check 1/2 pytest \(elapsed",
            )
            self.assertRegex(
                stderr_text,
                timestamp
                + r"verification check 1/2 finished: pytest \(command\) status pass",
            )
            self.assertRegex(
                stderr_text,
                timestamp
                + r"verification check 2/2 skipped: focused \(command\), "
                + r"condition condition-not-met, on failure of pytest",
            )
            self.assertRegex(
                stderr_text,
                timestamp
                + r"verification passed for session 1/2 \(1/2 checks passed, "
                + r"1 gate, 1 skipped",
            )

    def test_live_verification_check_started_finished_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            event_path = tmp_dir / events.EVENTS_FILE_NAME
            state_path = tmp_dir / events.STATE_FILE_NAME
            payload = {
                "state_path": str(state_path),
                "active_turn": None,
                "active_verification": None,
            }
            persistence = run_persistence._RunPersistence()
            live = live_verification._LiveVerification(
                event_path=event_path,
                payload=payload,
                progress=run_reporting._ProgressReporter(enabled=False),
                persistence=persistence,
                run_id="run-2",
                turn_index=3,
                session_id="2.1",
                session_label="2.1/4",
                total_sessions=4,
                check_count=2,
                check_names=("readme", "regex"),
                check_kinds=("file_exists", "regex_in_file"),
                check_repos=("SVC", "OAK"),
                heartbeat_interval=60,
            )

            live.start()
            after_start_state = json.loads(state_path.read_text(encoding="utf-8"))[
                "active_verification"
            ]
            self.assertEqual(2, after_start_state["check_count"])
            self.assertEqual(
                ["file_exists", "regex_in_file"], after_start_state["check_kinds"]
            )
            self.assertEqual(["SVC", "OAK"], after_start_state["check_repos"])
            self.assertIsNone(after_start_state["current_check_index"])

            live.check_started(
                check_index=0,
                name="readme",
                kind="file_exists",
                argv=None,
                cwd=None,
                repo="SVC",
            )
            mid_state = json.loads(state_path.read_text(encoding="utf-8"))[
                "active_verification"
            ]
            self.assertEqual(0, mid_state["current_check_index"])
            self.assertEqual("readme", mid_state["current_check_name"])
            self.assertEqual("file_exists", mid_state["current_check_kind"])
            self.assertIsNotNone(mid_state["current_check_started_at"])

            live.check_finished(
                check_index=0,
                name="readme",
                kind="file_exists",
                role=verification_models.CHECK_ROLE_GATE,
                status="fail",
                duration_seconds=0.0,
                failure_reason="file-missing",
                returncode=None,
                repo="SVC",
            )
            after_check_state = json.loads(state_path.read_text(encoding="utf-8"))[
                "active_verification"
            ]
            self.assertIsNone(after_check_state["current_check_started_at"])
            self.assertEqual(0, after_check_state["current_check_index"])

            live.finish(
                passed=False,
                passed_count=0,
                reason="verify-failed",
                error="check failed",
            )
            final_state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertIsNone(final_state["active_verification"])

            event_records = read_jsonl(event_path)
            failed_check = next(
                record["payload"]
                for record in event_records
                if record["name"] == events.VERIFICATION_CHECK_FINISHED_EVENT
            )
            self.assertEqual("fail", failed_check["status"])
            self.assertEqual("file-missing", failed_check["failure_reason"])
            self.assertIsNone(failed_check["returncode"])
            self.assertEqual("SVC", failed_check["repo"])
            self.assertNotIn("argv", failed_check)

            finished = next(
                record["payload"]
                for record in event_records
                if record["name"] == events.VERIFICATION_FINISHED_EVENT
            )
            self.assertFalse(finished["passed"])
            self.assertEqual("verify-failed", finished["reason"])
            self.assertEqual("check failed", finished["error"])
            check_started = next(
                record["payload"]
                for record in event_records
                if record["name"] == events.VERIFICATION_CHECK_STARTED_EVENT
            )
            self.assertIsNone(check_started["argv"])
            self.assertIsNone(check_started["cwd"])
            self.assertEqual("SVC", check_started["repo"])

    def test_live_verification_background_finish_keeps_running_check_displayed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            event_path = tmp_dir / events.EVENTS_FILE_NAME
            state_path = tmp_dir / events.STATE_FILE_NAME
            payload = {
                "state_path": str(state_path),
                "active_turn": None,
                "active_verification": None,
            }
            live = live_verification._LiveVerification(
                event_path=event_path,
                payload=payload,
                progress=run_reporting._ProgressReporter(enabled=False),
                persistence=run_persistence._RunPersistence(),
                run_id="run-3",
                turn_index=1,
                session_id="1",
                session_label="1/1",
                total_sessions=1,
                check_count=2,
                check_names=("fast", "slow"),
                check_kinds=("command", "command"),
                parallel=True,
                heartbeat_interval=60,
            )

            live.start()
            started_event = next(
                record["payload"]
                for record in read_jsonl(event_path)
                if record["name"] == events.VERIFICATION_STARTED_EVENT
            )
            self.assertTrue(started_event["parallel"])

            for index, name in ((0, "fast"), (1, "slow")):
                live.check_started(
                    check_index=index,
                    name=name,
                    kind="command",
                    argv=("python3",),
                    cwd=None,
                )
            live.check_finished(
                check_index=0,
                name="fast",
                kind="command",
                role=verification_models.CHECK_ROLE_GATE,
                status="pass",
                duration_seconds=0.1,
                failure_reason=None,
                returncode=0,
            )

            active = json.loads(state_path.read_text(encoding="utf-8"))[
                "active_verification"
            ]
            # `slow` is still running, so the background completion of `fast`
            # must not steal the display slot or blank the check timer.
            self.assertEqual(1, active["current_check_index"])
            self.assertEqual("slow", active["current_check_name"])
            self.assertIsNotNone(active["current_check_started_at"])
            finished_event = next(
                record["payload"]
                for record in read_jsonl(event_path)
                if record["name"] == events.VERIFICATION_CHECK_FINISHED_EVENT
            )
            self.assertEqual(0, finished_event["check_index"])
            self.assertEqual("fast", finished_event["name"])

            live.check_finished(
                check_index=1,
                name="slow",
                kind="command",
                role=verification_models.CHECK_ROLE_GATE,
                status="pass",
                duration_seconds=0.5,
                failure_reason=None,
                returncode=0,
            )
            active = json.loads(state_path.read_text(encoding="utf-8"))[
                "active_verification"
            ]
            self.assertIsNone(active["current_check_started_at"])

            live.finish(passed=True, passed_count=2, reason="ok")

    def test_live_verification_displayed_finish_promotes_remaining_check(
        self,
    ) -> None:
        """Reverse completion order must not stop parallel heartbeats.

        The display slot goes to the most recently started check, so when that
        one finishes first the earlier-started command is still running and has
        to take over the slot; otherwise the check timer is blanked and
        `_record_heartbeat` goes silent while verification is still working.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            event_path = tmp_dir / events.EVENTS_FILE_NAME
            state_path = tmp_dir / events.STATE_FILE_NAME
            payload = {
                "state_path": str(state_path),
                "active_turn": None,
                "active_verification": None,
            }
            live = live_verification._LiveVerification(
                event_path=event_path,
                payload=payload,
                progress=run_reporting._ProgressReporter(enabled=False),
                persistence=run_persistence._RunPersistence(),
                run_id="run-5",
                turn_index=1,
                session_id="1",
                session_label="1/1",
                total_sessions=1,
                check_count=3,
                check_names=("slow", "medium", "fast"),
                check_kinds=("command", "command", "command"),
                parallel=True,
                heartbeat_interval=60,
            )

            live.start()
            for index, name in ((0, "slow"), (1, "medium"), (2, "fast")):
                live.check_started(
                    check_index=index,
                    name=name,
                    kind="command",
                    argv=("python3",),
                    cwd=None,
                )

            # `fast` holds the display slot and retires first, while `slow` and
            # `medium` are still running.
            live.check_finished(
                check_index=2,
                name="fast",
                kind="command",
                role=verification_models.CHECK_ROLE_GATE,
                status="pass",
                duration_seconds=0.1,
                failure_reason=None,
                returncode=0,
            )
            active = json.loads(state_path.read_text(encoding="utf-8"))[
                "active_verification"
            ]
            self.assertEqual(1, active["current_check_index"])
            self.assertEqual("medium", active["current_check_name"])
            self.assertIsNotNone(active["current_check_started_at"])
            heartbeat = live._record_heartbeat()
            self.assertIsNotNone(heartbeat)
            self.assertEqual(1, heartbeat["check_index"])
            self.assertEqual("medium", heartbeat["name"])

            live.check_finished(
                check_index=1,
                name="medium",
                kind="command",
                role=verification_models.CHECK_ROLE_GATE,
                status="pass",
                duration_seconds=0.3,
                failure_reason=None,
                returncode=0,
            )
            active = json.loads(state_path.read_text(encoding="utf-8"))[
                "active_verification"
            ]
            self.assertEqual(0, active["current_check_index"])
            self.assertEqual("slow", active["current_check_name"])
            self.assertIsNotNone(active["current_check_started_at"])
            heartbeat = live._record_heartbeat()
            self.assertIsNotNone(heartbeat)
            self.assertEqual(0, heartbeat["check_index"])
            self.assertEqual("slow", heartbeat["name"])

            # Only the last active check clears the timer and silences
            # heartbeats.
            live.check_finished(
                check_index=0,
                name="slow",
                kind="command",
                role=verification_models.CHECK_ROLE_GATE,
                status="pass",
                duration_seconds=0.9,
                failure_reason=None,
                returncode=0,
            )
            active = json.loads(state_path.read_text(encoding="utf-8"))[
                "active_verification"
            ]
            self.assertEqual(0, active["current_check_index"])
            self.assertIsNone(active["current_check_started_at"])
            self.assertIsNone(live._record_heartbeat())

            live.finish(passed=True, passed_count=3, reason="ok")
