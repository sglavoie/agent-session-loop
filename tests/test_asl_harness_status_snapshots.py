"""Compact status snapshots and derived ledgers during fake-harness runs."""

from __future__ import annotations

from agent_session_loop import (
    clock,
    config,
    events,
    harness_failures,
    plans_files,
    plans_snapshot,
    run_reporting,
    state,
)
from tests.harness_integration_base import FakeHarnessIntegrationBase
from tests.harness_integration_support import OBSERVED_LAUNCH_CLOSURE
from tests.support import (
    read_jsonl,
)


class StatusSnapshotTests(FakeHarnessIntegrationBase):
    def test_compact_status_snapshot_reports_active_turn(self) -> None:
        plan_dir, _state_dir, _config_home, _fake_codex = self.setup_loop(
            statuses=("pending", "pending")
        )
        active_turn = {
            "session": 1,
            "session_id": "1",
            "total_sessions": 2,
            "turn_index": 1,
            "harness": "codex",
            "stdout_path": "/tmp/run/stdout.log",
            "stderr_path": "/tmp/run/stderr.log",
            "last_message_path": "/tmp/run/last-message.txt",
            "result_path": "/tmp/run/result.json",
            "elapsed_seconds": 12.3,
            "idle_seconds": 2.5,
        }
        payload = {
            "harness": "codex",
            "model": "gpt-5",
            "model_source": config.MODEL_SOURCE_FLAG,
            "effort": "high",
            "effort_source": config.EFFORT_SOURCE_FLAG,
            "note": "operator reference ABC-123",
            "run_started_at": "2026-06-11T12:00:00+00:00",
            "active_turn": active_turn,
            "turns": [],
        }
        control = {
            "stop_after_current": True,
            "tail_enabled": True,
            "next_harness": "claude",
        }

        text = "\n".join(
            run_reporting._compact_status_lines(
                payload=payload,
                control=control,
                snapshot=plans_snapshot.inspect_plan(plan_dir),
                now=clock.parse_datetime("2026-06-11T12:00:20+00:00"),
            )
        )

        self.assertIn("status: active session 1/2 turn 1", text)
        self.assertIn("elapsed 12.3s, idle 2.5s, total 20.0s", text)
        self.assertIn("status note: operator reference ABC-123", text)
        self.assertIn("sessions 0/2 complete, 2 pending", text)
        self.assertIn("harness active codex; future claude", text)
        self.assertIn("model gpt-5 (flag); effort high (flag)", text)
        self.assertIn("stop-after-current on; live tail on", text)
        self.assertIn("stdout /tmp/run/stdout.log", text)
        self.assertIn("last-message /tmp/run/last-message.txt", text)

    def test_compact_status_snapshot_reports_idle_between_turns(self) -> None:
        plan_dir, _state_dir, _config_home, _fake_codex = self.setup_loop(
            statuses=("complete", "pending")
        )
        payload = {
            "harness": "codex",
            "model": config.NATIVE_MODEL_DEFAULT,
            "model_source": config.MODEL_SOURCE_UNRESOLVED,
            "effort": None,
            "effort_source": None,
            "run_started_at": "2026-06-11T12:00:00+00:00",
            "active_turn": None,
            "turns": [
                {
                    "stdout_path": "/tmp/run/turn-1/stdout.log",
                    "stderr_path": "/tmp/run/turn-1/stderr.log",
                    "last_message_path": "/tmp/run/turn-1/last-message.txt",
                    "result_path": "/tmp/run/turn-1/result.json",
                }
            ],
        }
        control = {
            "stop_after_current": False,
            "tail_enabled": False,
            "next_harness": None,
        }

        text = "\n".join(
            run_reporting._compact_status_lines(
                payload=payload,
                control=control,
                snapshot=plans_snapshot.inspect_plan(plan_dir),
                now=clock.parse_datetime("2026-06-11T12:01:30+00:00"),
            )
        )

        self.assertIn(
            "status: idle; next session 2/2 turn 2; "
            "sessions 1/2 complete, 1 pending; total 1m30s",
            text,
        )
        self.assertIn("stdout /tmp/run/turn-1/stdout.log", text)
        self.assertIn("result /tmp/run/turn-1/result.json", text)

    def test_compact_status_snapshot_reports_no_active_turn(self) -> None:
        plan_dir, _state_dir, _config_home, _fake_codex = self.setup_loop(
            statuses=("pending",)
        )
        payload = {
            "harness": "codex",
            "model": config.NATIVE_MODEL_DEFAULT,
            "model_source": config.MODEL_SOURCE_UNRESOLVED,
            "effort": None,
            "effort_source": None,
            "active_turn": None,
            "turns": [],
        }
        control = {
            "stop_after_current": False,
            "tail_enabled": False,
            "next_harness": None,
        }

        text = "\n".join(
            run_reporting._compact_status_lines(
                payload=payload,
                control=control,
                snapshot=plans_snapshot.inspect_plan(plan_dir),
            )
        )

        self.assertIn(
            "status: no active turn; next session 1/1 turn 1; "
            "sessions 0/1 complete, 1 pending",
            text,
        )
        self.assertIn("status paths: none yet", text)

    def test_compact_status_snapshot_reports_plan_inspection_error(self) -> None:
        payload = {
            "harness": "codex",
            "model": config.NATIVE_MODEL_DEFAULT,
            "model_source": config.MODEL_SOURCE_UNRESOLVED,
            "effort": None,
            "effort_source": None,
            "active_turn": None,
            "turns": [],
            "initial_snapshot": {
                "counts": {
                    "pending": 1,
                    "complete": 0,
                    "empty": 0,
                    "unknown": 0,
                },
                "next_pending_id": "1",
            },
        }
        control = {
            "stop_after_current": False,
            "tail_enabled": False,
            "next_harness": None,
        }

        text = "\n".join(
            run_reporting._compact_status_lines(
                payload=payload,
                control=control,
                snapshot=None,
                plan_error="missing context.md",
            )
        )

        self.assertIn("status: plan inspection error: missing context.md", text)
        self.assertIn("sessions 0/1 complete, 1 pending", text)

    def test_compact_status_live_all_complete_overrides_stale_next_pending(
        self,
    ) -> None:
        plan_dir, _state_dir, _config_home, _fake_codex = self.setup_loop(
            statuses=("complete", "complete", "complete", "complete", "complete")
        )
        payload = {
            "harness": "codex",
            "model": config.NATIVE_MODEL_DEFAULT,
            "model_source": config.MODEL_SOURCE_UNRESOLVED,
            "effort": None,
            "effort_source": None,
            "active_turn": None,
            "turns": [],
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
        control = {
            "stop_after_current": False,
            "tail_enabled": False,
            "next_harness": None,
        }

        text = "\n".join(
            run_reporting._compact_status_lines(
                payload=payload,
                control=control,
                snapshot=plans_snapshot.inspect_plan(plan_dir),
            )
        )

        self.assertIn("sessions 5/5 complete, 0 pending", text)
        self.assertIn("next session none", text)
        self.assertNotIn("next session 5", text)

    def test_compact_status_snapshot_reports_active_verification(self) -> None:
        plan_dir, _state_dir, _config_home, _fake_codex = self.setup_loop(
            statuses=("pending", "pending")
        )
        active_verification = {
            "run_id": "run-1",
            "turn_index": 1,
            "session": 1,
            "session_id": "1",
            "total_sessions": 2,
            "check_count": 2,
            "check_names": ["pytest", "compile"],
            "check_kinds": ["command", "command"],
            "started_at": "2026-06-11T12:00:00+00:00",
            "current_check_index": 1,
            "current_check_name": "compile",
            "current_check_kind": "command",
            "current_check_started_at": "2026-06-11T12:00:10+00:00",
            "elapsed_seconds": 15.0,
            "check_elapsed_seconds": 5.0,
            "heartbeat_count": 1,
        }
        payload = {
            "harness": "codex",
            "model": "gpt-5",
            "model_source": config.MODEL_SOURCE_FLAG,
            "effort": "high",
            "effort_source": config.EFFORT_SOURCE_FLAG,
            "run_started_at": "2026-06-11T12:00:00+00:00",
            "active_turn": None,
            "active_verification": active_verification,
            "turns": [
                {
                    "stdout_path": "/tmp/run/turn-1/stdout.log",
                    "stderr_path": "/tmp/run/turn-1/stderr.log",
                    "last_message_path": "/tmp/run/turn-1/last-message.txt",
                    "result_path": "/tmp/run/turn-1/result.json",
                }
            ],
        }
        control = {
            "stop_after_current": False,
            "tail_enabled": False,
            "next_harness": None,
        }

        text = "\n".join(
            run_reporting._compact_status_lines(
                payload=payload,
                control=control,
                snapshot=plans_snapshot.inspect_plan(plan_dir),
                now=clock.parse_datetime("2026-06-11T12:00:20+00:00"),
            )
        )

        self.assertIn("status: verifying session 1/2 turn 1 check 2/2 compile", text)
        self.assertIn("elapsed 15.0s, check elapsed 5.0s, total 20.0s", text)
        self.assertIn("sessions 0/2 complete, 2 pending", text)
        self.assertNotIn("status: active session", text)
        self.assertNotIn("status: idle", text)

    def test_compact_status_snapshot_reports_verification_before_first_check(
        self,
    ) -> None:
        plan_dir, _state_dir, _config_home, _fake_codex = self.setup_loop(
            statuses=("pending",)
        )
        active_verification = {
            "run_id": "run-1",
            "turn_index": 1,
            "session": 1,
            "session_id": "1",
            "total_sessions": 1,
            "check_count": 3,
            "check_names": ["a", "b", "c"],
            "check_kinds": ["command", "file_exists", "regex_in_file"],
            "started_at": "2026-06-11T12:00:00+00:00",
            "current_check_index": None,
            "current_check_name": None,
            "current_check_kind": None,
            "current_check_started_at": None,
            "elapsed_seconds": 0.5,
            "check_elapsed_seconds": None,
            "heartbeat_count": 0,
        }
        payload = {
            "harness": "codex",
            "model": "gpt-5",
            "model_source": config.MODEL_SOURCE_FLAG,
            "effort": "high",
            "effort_source": config.EFFORT_SOURCE_FLAG,
            "run_started_at": "2026-06-11T12:00:00+00:00",
            "active_turn": None,
            "active_verification": active_verification,
            "turns": [],
        }
        control = {
            "stop_after_current": False,
            "tail_enabled": False,
            "next_harness": None,
        }

        text = "\n".join(
            run_reporting._compact_status_lines(
                payload=payload,
                control=control,
                snapshot=plans_snapshot.inspect_plan(plan_dir),
                now=clock.parse_datetime("2026-06-11T12:00:01+00:00"),
            )
        )

        self.assertIn("status: verifying session 1/1 turn 1 check -/3 pending", text)
        self.assertNotIn("check elapsed", text)
        self.assertIn("elapsed 0.5s", text)

    def test_successful_turn_syncs_derived_status_ledgers(self) -> None:
        plan_dir, state_dir, config_home, _fake_codex = self.setup_loop(
            statuses=("pending", "pending")
        )
        (plan_dir / "manifest.md").write_text(
            "\n".join(
                [
                    "# Manifest",
                    "",
                    "| # | Title | Status | File |",
                    "|---|---|---|---|",
                    "| 1 | One | pending | session-1.md |",
                    "| 2 | Two | pending | session-2.md |",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        (plan_dir / "context.md").write_text(
            "\n".join(
                [
                    "# Context",
                    "",
                    "| Session | Title | Status |",
                    "|---|---|---|",
                    "| S1 | One | pending |",
                    "| S2 | Two | pending |",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        completed, payload = self.run_loop_command(
            mode="success",
            plan_dir=plan_dir,
            state_dir=state_dir,
            config_home=config_home,
            max_sessions=1,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        turn = payload["turns"][0]
        self.assertEqual(
            {
                str(plans_files.canonicalize_path(plan_dir / "manifest.md")),
                str(plans_files.canonicalize_path(plan_dir / "context.md")),
            },
            set(turn["ledger_updates"]),
        )
        self.assertIn(
            "| 1 | One | complete | session-1.md |",
            (plan_dir / "manifest.md").read_text(encoding="utf-8"),
        )
        self.assertIn(
            "| 2 | Two | pending | session-2.md |",
            (plan_dir / "manifest.md").read_text(encoding="utf-8"),
        )
        self.assertIn(
            "| S1 | One | complete |",
            (plan_dir / "context.md").read_text(encoding="utf-8"),
        )
        self.assertIn(
            "| S2 | Two | pending |",
            (plan_dir / "context.md").read_text(encoding="utf-8"),
        )

    def test_launch_transport_closure_records_a_degradation_not_a_failure(
        self,
    ) -> None:
        completed, payload, plan_dir, state_dir, _fake_codex = self.run_loop(
            mode="success",
            statuses=("pending",),
            extra_env={"FAKE_CODEX_STDERR_TEXT": f"{OBSERVED_LAUNCH_CLOSURE}\n"},
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        turn = payload["turns"][0]
        self.assertEqual("complete", turn["status_result"])
        self.assertNotIn("failure_classification", turn)
        self.assertEqual(
            harness_failures.TRANSPORT_DEGRADATION_MARKER,
            turn["harness_degradation"]["marker"],
        )
        self.assertFalse(turn["harness_degradation"]["affected_turn"])
        records = read_jsonl(
            state.plan_state_dir(plan_dir, state_dir) / events.EVENTS_FILE_NAME
        )
        degraded = [
            record
            for record in records
            if record.get("name") == events.HARNESS_DEGRADED_EVENT
        ]
        self.assertEqual(1, len(degraded))
        self.assertFalse(degraded[0]["payload"]["affected_turn"])
        self.assertEqual(1, degraded[0]["payload"]["turn_index"])
