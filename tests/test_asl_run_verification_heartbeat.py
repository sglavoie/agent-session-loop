"""Long verification heartbeat integration tests."""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    agent_mux_delivery,
    config,
    events,
    plans_progress,
    plans_snapshot,
    run_loop,
    session_verification,
    state,
    state_locks,
    state_status,
)
from tests.support import (
    REPO_ROOT,
    _contract_block,
    agent_mux_notify_enabled,
    init_git_repo,
    install_fake_amx,
    install_fake_codex,
    patched_env,
    read_jsonl,
    write_plan,
)

ASL_TEST_LANE = "integration"


class LongVerificationHeartbeatIntegrationTests(unittest.TestCase):
    """End-to-end coverage for the full post-turn verification lifecycle.

    Drives ``run_locked`` directly with a synthetic long-running verification
    command and a shortened heartbeat interval so at least one
    ``verification_heartbeat`` event fires inside the check window. Also locks
    in the documented post-turn event ordering: ``turn_finished`` precedes the
    four verification phase events, the progress journal entry is appended
    after verification passes, and ``run_finished`` is the final lifecycle
    event.

    Also covers the historical ``state.json`` shape (no ``active_verification``
    key) as a fixture-style regression: ``status_payload`` and
    ``_print_text_status`` must remain consumable by pre-feature state
    snapshots without raising or inventing detail.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.plan_dir = self.tmp_path / "plan"
        self.state_dir = self.tmp_path / "state"
        self.repo_root = self.tmp_path / "repo"
        self.config_home = self.tmp_path / "config"
        init_git_repo(self.repo_root)
        self.fake_codex = install_fake_codex(self.tmp_path)
        config_path = self.config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps({"codex": {"executable": str(self.fake_codex)}}),
            encoding="utf-8",
        )

    def _make_args(self) -> argparse.Namespace:
        return argparse.Namespace(
            plan_dir=str(self.plan_dir),
            cwd=str(self.repo_root),
            harness="codex",
            state_dir=str(self.state_dir),
            json=False,
            dry_run=False,
            unsafe_bypass=True,
            timeout=None,
            max_sessions=None,
            commit_mode=None,
            model=None,
            effort=None,
            note=None,
            allow_not_loop_ready=True,
        )

    def _invoke_run_locked(self) -> tuple[int, dict[str, Any]]:
        env_updates = {
            "XDG_CONFIG_HOME": str(self.config_home),
            "FAKE_CODEX_MODE": "success",
            "PYTHONPATH": str(REPO_ROOT)
            + os.pathsep
            + os.environ.get("PYTHONPATH", ""),
        }
        args = self._make_args()
        with patched_env(**env_updates):
            lock_info = state_locks.acquire_lock(self.plan_dir, self.state_dir)
            try:
                exit_code, payload = run_loop.run_locked(
                    args=args,
                    cwd=self.repo_root,
                    timeout=None,
                    max_sessions=None,
                    commit_mode=None,
                    lock_info=lock_info,
                    run_id="test-run-heartbeat",
                )
            finally:
                state_locks.release_lock(self.plan_dir, self.state_dir)
        return exit_code, payload

    def test_long_verification_command_emits_heartbeats_and_orders_post_turn_events(
        self,
    ) -> None:
        sleep_argv = [
            sys.executable,
            "-c",
            "import time; time.sleep(0.3)",
        ]
        check_payload = (
            '{"type": "command", "name": "long-sleep", '
            f'"argv": {json.dumps(sleep_argv)}, "timeout_seconds": 30}}'
        )
        contract = _contract_block(check_payload, session="1")
        write_plan(self.plan_dir, ("pending",), contracts_by_id={"1": contract})

        real_enforce = session_verification._enforce_session_verification

        def short_heartbeat_enforce(**kwargs: object) -> bool:
            kwargs["heartbeat_interval"] = 0.05
            return real_enforce(**kwargs)

        with mock.patch.object(
            session_verification,
            "_enforce_session_verification",
            new=short_heartbeat_enforce,
        ):
            exit_code, payload = self._invoke_run_locked()

        self.assertEqual(0, exit_code)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])
        self.assertEqual("all-complete", payload["reason"])
        # Both active phases cleared cleanly at run finish.
        self.assertIsNone(payload["active_turn"])
        self.assertIsNone(payload["active_verification"])
        # Final snapshot reflects the post-verify all-complete plan state.
        self.assertEqual(1, payload["final_snapshot"]["counts"]["complete"])
        self.assertIsNone(payload["final_snapshot"]["next_pending"])

        event_path = (
            state.plan_state_dir(self.plan_dir, self.state_dir)
            / events.EVENTS_FILE_NAME
        )
        event_records = read_jsonl(event_path)
        event_names = [record["name"] for record in event_records]

        # At least one verification_heartbeat fired during the synthetic long
        # verify. With a 0.3s sleep and a 0.05s interval, we expect several
        # heartbeats; the assertion uses >=1 to avoid scheduler flakiness.
        heartbeat_count = event_names.count(events.VERIFICATION_HEARTBEAT_EVENT)
        self.assertGreaterEqual(
            heartbeat_count,
            1,
            f"expected verification_heartbeat events; got: {event_names}",
        )

        # Heartbeats happened strictly between check_started and
        # check_finished for the same check.
        check_started_index = event_names.index(events.VERIFICATION_CHECK_STARTED_EVENT)
        check_finished_index = event_names.index(
            events.VERIFICATION_CHECK_FINISHED_EVENT
        )
        heartbeat_indexes = [
            i
            for i, name in enumerate(event_names)
            if name == events.VERIFICATION_HEARTBEAT_EVENT
        ]
        for hb_index in heartbeat_indexes:
            self.assertGreater(hb_index, check_started_index)
            self.assertLess(hb_index, check_finished_index)

        # Documented post-turn event ordering: the harness exit is announced
        # before the four verification phase events, turn_finished follows
        # them because verification is a guard that can still refuse the turn,
        # and run_finished comes last.
        execution_finished_index = event_names.index(
            events.TURN_EXECUTION_FINISHED_EVENT
        )
        turn_finished_index = event_names.index(events.TURN_FINISHED_EVENT)
        verification_started_index = event_names.index(
            events.VERIFICATION_STARTED_EVENT
        )
        verification_finished_index = event_names.index(
            events.VERIFICATION_FINISHED_EVENT
        )
        run_finished_index = event_names.index("run_finished")
        self.assertLess(execution_finished_index, verification_started_index)
        self.assertLess(verification_started_index, check_started_index)
        self.assertLess(check_started_index, check_finished_index)
        self.assertLess(check_finished_index, verification_finished_index)
        self.assertLess(verification_finished_index, turn_finished_index)
        self.assertLess(turn_finished_index, run_finished_index)

        # Progress journal append happened after verification passed and
        # before run_finished. The append itself is silent on success, so the
        # durable evidence is the journal entry that exists by the time the
        # run reports completion.
        entries = plans_progress.read_progress_entries(self.plan_dir)
        self.assertEqual(1, len(entries))
        self.assertEqual("1", entries[0].session_id.value)
        self.assertEqual("complete", entries[0].status)
        self.assertTrue(payload["turns"][0]["progress_appended"])

    def test_agent_mux_notifications_describe_the_same_phase_sequence(
        self,
    ) -> None:
        """The AgentMux notification stream must mirror the persisted
        lifecycle: same phase transitions as state/events, one monotonic
        per-run sequence, and verification heartbeats carrying check
        identity — the daemon-side contract from the reliability plan."""
        _fake_amx, amx_log = install_fake_amx(self.tmp_path)
        sleep_argv = [
            sys.executable,
            "-c",
            "import time; time.sleep(0.3)",
        ]
        check_payload = (
            '{"type": "command", "name": "long-sleep", '
            f'"argv": {json.dumps(sleep_argv)}, "timeout_seconds": 30}}'
        )
        contract = _contract_block(check_payload, session="1")
        write_plan(self.plan_dir, ("pending",), contracts_by_id={"1": contract})

        real_enforce = session_verification._enforce_session_verification

        def short_heartbeat_enforce(**kwargs: object) -> bool:
            kwargs["heartbeat_interval"] = 0.05
            return real_enforce(**kwargs)

        with (
            agent_mux_notify_enabled(),
            patched_env(
                PATH=str(self.tmp_path) + os.pathsep + os.environ.get("PATH", ""),
                FAKE_AMX_LOG=str(amx_log),
                # Slow interpreter start under parallel-shard load must not
                # silently drop a notification record.
                ASL_AGENT_MUX_NOTIFY_TIMEOUT_SECONDS="30",
            ),
            mock.patch.object(
                session_verification,
                "_enforce_session_verification",
                new=short_heartbeat_enforce,
            ),
        ):
            exit_code, payload = self._invoke_run_locked()

        self.assertEqual(0, exit_code)
        self.assertTrue(payload["ok"])

        amx_records = read_jsonl(amx_log)
        notified_events = [record["argv"][-1] for record in amx_records]
        hook_payloads = [json.loads(str(record["stdin"])) for record in amx_records]

        # At least one verification heartbeat notification fired inside the
        # synthetic 0.3s check window (0.05s interval).
        verification_heartbeats = [
            hook_payload
            for event, hook_payload in zip(notified_events, hook_payloads, strict=False)
            if event == agent_mux_delivery.VERIFICATION_HEARTBEAT_EVENT
        ]
        self.assertGreaterEqual(
            len(verification_heartbeats),
            1,
            f"expected verification heartbeat notifications; got: {notified_events}",
        )

        # The notification stream, with consecutive same-event runs
        # collapsed, is exactly the documented lifecycle order.
        collapsed_events = [
            event
            for index, event in enumerate(notified_events)
            if index == 0 or event != notified_events[index - 1]
        ]
        self.assertEqual(
            [
                agent_mux_delivery.START_EVENT,
                agent_mux_delivery.TURN_START_EVENT,
                agent_mux_delivery.VERIFICATION_START_EVENT,
                agent_mux_delivery.VERIFICATION_HEARTBEAT_EVENT,
                agent_mux_delivery.VERIFICATION_FINISH_EVENT,
                agent_mux_delivery.TURN_FINISH_EVENT,
                agent_mux_delivery.FINISH_EVENT,
            ],
            collapsed_events,
        )

        # Every notification carries the run identity, the daemon phase for
        # its event, and one strictly monotonic string-encoded sequence.
        run_id = payload["run_id"]
        for event, hook_payload in zip(notified_events, hook_payloads, strict=False):
            self.assertEqual(run_id, hook_payload["run_id"])
            self.assertEqual(
                agent_mux_delivery.PHASE_BY_EVENT[event],
                hook_payload["phase"],
            )
            self.assertEqual(
                f"{run_id}:{hook_payload['source_sequence']}",
                hook_payload["source_event_id"],
            )
            self.assertTrue(hook_payload["emitted_time"].isdigit())
        sequences = [
            int(hook_payload["source_sequence"]) for hook_payload in hook_payloads
        ]
        self.assertEqual(list(range(1, len(sequences) + 1)), sequences)

        # The persisted counter in state.json matches the last emission.
        state_payload = json.loads(
            Path(str(payload["state_path"])).read_text(encoding="utf-8")
        )
        self.assertEqual(len(sequences), state_payload["agent_mux_sequence"])

        # Verification heartbeats carry the check identity, heartbeat count,
        # and recency fields the daemon consumes.
        for heartbeat_payload in verification_heartbeats:
            self.assertEqual("long-sleep", heartbeat_payload["check_name"])
            self.assertEqual("command", heartbeat_payload["check_kind"])
            self.assertGreaterEqual(heartbeat_payload["heartbeat_count"], 1)
            self.assertIn("last_heartbeat_at", heartbeat_payload)
            self.assertEqual("1", heartbeat_payload["session_id"])

        # The notification phase transitions equal the phase transitions
        # derivable from the persisted events.jsonl — the "same phase
        # sequence" completion contract.
        event_path = (
            state.plan_state_dir(self.plan_dir, self.state_dir)
            / events.EVENTS_FILE_NAME
        )
        internal_phase_by_event = {
            "turn_started": "turn",
            events.TURN_HEARTBEAT_EVENT: "turn",
            "turn_finished": "between",
            events.VERIFICATION_STARTED_EVENT: "verification",
            events.VERIFICATION_HEARTBEAT_EVENT: "verification",
            events.VERIFICATION_FINISHED_EVENT: "between",
            "run_finished": "finished",
        }
        internal_phases = ["between"] + [
            internal_phase_by_event[record["name"]]
            for record in read_jsonl(event_path)
            if record["name"] in internal_phase_by_event
        ]
        collapse = lambda values: [  # noqa: E731 - one-off local, reads better inline
            value
            for index, value in enumerate(values)
            if index == 0 or value != values[index - 1]
        ]
        self.assertEqual(
            collapse(internal_phases),
            collapse([hook_payload["phase"] for hook_payload in hook_payloads]),
        )

        # `active_verification.last_heartbeat_at` was persisted while the
        # check ran; the final state clears the block entirely.
        self.assertIsNone(state_payload["active_verification"])

    def test_historical_state_without_active_verification_renders_cleanly(
        self,
    ) -> None:
        write_plan(self.plan_dir, ("complete",))
        plan_state = state.plan_state_dir(self.plan_dir, self.state_dir)
        plan_state.mkdir(parents=True, exist_ok=True)
        state_path = plan_state / events.STATE_FILE_NAME
        # Synthetic snapshot of a pre-feature `state.json`: no
        # `active_verification` key, no stale-orphan tracking, no
        # newer turn-level fields. The new code paths must default the
        # missing fields without raising or inventing detail.
        historical_payload = {
            "ok": True,
            "paused": False,
            "reason": "all-complete",
            "harness": "codex",
            "run_id": "historical-run-1",
            "run_started_at": "2025-01-01T00:00:00+00:00",
            "run_finished_at": "2025-01-01T00:05:00+00:00",
            "duration_seconds": 300.0,
            "active_turn": None,
        }
        state_path.write_text(
            json.dumps(historical_payload, sort_keys=True),
            encoding="utf-8",
        )

        snapshot = plans_snapshot.inspect_plan(self.plan_dir)
        payload = state_status.status_payload(snapshot, self.state_dir)

        # Top-level shape mirrors `runner_state` and defaults missing keys.
        self.assertIsNone(payload["active_turn"])
        self.assertIsNone(payload["active_verification"])
        runner_state = payload["runner_state"]
        self.assertIsNone(runner_state["active_turn"])
        self.assertIsNone(runner_state["active_verification"])
        # A historical run that recorded `run_finished_at` is not stale, even
        # without a live lock — the stale-orphan check only fires when state
        # never finished.
        self.assertFalse(runner_state["stale_unfinished"])
        self.assertIsNone(runner_state["stale_reason"])

        # The text renderer must not raise when fed a historical payload and
        # must not invent `Active verification:` or `stale/orphaned` lines
        # that weren't present in the snapshot.
        buffer = io.StringIO()
        args = argparse.Namespace(
            plan_dir=str(self.plan_dir),
            state_dir=str(self.state_dir),
            json=False,
        )
        with redirect_stdout(buffer):
            exit_code = state_status.status_from_args(args)
        self.assertEqual(0, exit_code)
        rendered = buffer.getvalue()
        self.assertNotIn("Active verification:", rendered)
        self.assertNotIn("stale/orphaned", rendered)
        self.assertIn("Run state: run historical-run-1", rendered)


if __name__ == "__main__":
    unittest.main()
