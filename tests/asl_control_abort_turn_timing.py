"""Timing cases for AbortTurnOrchestrationTests."""

from __future__ import annotations

import argparse
import os
from unittest import mock

from agent_session_loop import (
    events,
    harnesses,
    run_control,
    run_loop,
    state_locks,
    turn_execution_constants,
)
from tests.support import (
    REPO_ROOT,
    FakeRunControlListener,
    patched_env,
    write_plan,
)

from .asl_control_abort_turn_support import AbortTurnOrchestrationTestsBase


class AbortTurnOrchestrationTests(AbortTurnOrchestrationTestsBase):
    def test_abort_between_turns_degrades_to_stop_after_current(self) -> None:
        payload, harness_calls = self._run_loop(
            statuses=("pending", "pending"),
            abort_before_turn_index=2,
        )

        # Nothing was in flight, so the abort could only mean "start no more".
        self.assertEqual("stopped", payload["reason"])
        self.assertFalse(payload["paused"])
        self.assertEqual([1], payload["completed_sessions"])
        self.assertEqual(["codex"], harness_calls)
        self.assertEqual("pending", self._session_status("2"))
        self.assertEqual(
            [
                events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
                events.RUN_CONTROL_ACTION_ABORT_TURN_REQUESTED,
                events.RUN_CONTROL_ACTION_ABORT_TURN_DEGRADED,
                events.RUN_CONTROL_ACTION_STOP_AFTER_CURRENT_REQUESTED,
                events.RUN_CONTROL_ACTION_STOP_AFTER_CURRENT_CONSUMED,
            ],
            self.run_control_actions(),
        )

    def test_abort_queued_before_the_run_still_runs_the_first_session(
        self,
    ) -> None:
        payload, harness_calls = self._run_loop(
            statuses=("pending", "pending"),
            abort_before_run=True,
        )

        # The pre-run drain has no turn to kill, so the command keeps the
        # stop-after-current meaning rather than aborting the first turn.
        self.assertEqual("stopped", payload["reason"])
        self.assertEqual([1], payload["completed_sessions"])
        self.assertEqual(["codex"], harness_calls)
        self.assertEqual("pending", self._session_status("2"))

    def test_expired_grace_kills_the_real_harness_process_group(self) -> None:
        write_plan(self.plan_dir, ("pending",))
        harness_pids: list[int] = []
        real_run_harness = harnesses.run_harness

        def spy_run_harness(name: str, **kwargs: object) -> harnesses.HarnessResult:
            if kwargs.get("plan_dir") is None:
                return real_run_harness(name, **kwargs)  # type: ignore[arg-type]
            inner_on_spawn = kwargs.get("on_spawn")

            def on_spawn(pid: int) -> None:
                harness_pids.append(pid)
                # Queued from the spawn callback so the grace window starts
                # against a process that is provably alive.
                self._queue_abort("during-real-turn", grace_seconds=1)
                if callable(inner_on_spawn):
                    inner_on_spawn(pid)

            kwargs["on_spawn"] = on_spawn
            return real_run_harness(name, **kwargs)  # type: ignore[arg-type]

        listener = FakeRunControlListener(current_harness="codex")

        def fake_for_run(*_args: object, **kwargs: object):
            listener.on_action = kwargs.get("on_action")
            listener.on_status = kwargs.get("on_status")
            return listener

        args = argparse.Namespace(
            plan_dir=str(self.plan_dir),
            cwd=str(self.repo_root),
            harness="codex",
            review_harness=None,
            state_dir=str(self.state_dir),
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
        with (
            patched_env(
                XDG_CONFIG_HOME=str(self.config_home),
                PYTHONPATH=str(REPO_ROOT)
                + os.pathsep
                + os.environ.get("PYTHONPATH", ""),
                FAKE_CODEX_MODE="timeout",
                FAKE_CODEX_SLEEP="60",
            ),
            mock.patch.object(
                turn_execution_constants, "LIVE_CONTROL_POLL_INTERVAL_SECONDS", 0.02
            ),
            mock.patch.object(harnesses, "run_harness", side_effect=spy_run_harness),
            mock.patch.object(
                run_control.RunControlListener,
                "for_run",
                side_effect=fake_for_run,
            ),
        ):
            lock_info = state_locks.acquire_lock(self.plan_dir, self.state_dir)
            try:
                exit_code, payload = run_loop.run_locked(
                    args=args,
                    cwd=self.repo_root,
                    timeout=None,
                    max_sessions=None,
                    commit_mode=None,
                    lock_info=lock_info,
                    run_id="test-run",
                )
            finally:
                state_locks.release_lock(self.plan_dir, self.state_dir)

        self.assertEqual(0, exit_code, payload)
        # The fake codex declines `app-server` before reading a byte, so the
        # turn spawns twice: the declined attempt, then the legacy fallback
        # that actually runs the prompt and is the one abort has to kill.
        self.assertEqual(2, len(harness_pids))
        self.assertEqual("stopped", payload["reason"])
        self.assertFalse(payload["paused"])
        self.assertEqual(137, payload["turns"][0]["returncode"])
        self.assertEqual("pending", self._session_status("1"))
        for pid in harness_pids:
            with self.assertRaises(ProcessLookupError):
                # The whole group is gone once the child is reaped, so nothing
                # the turn spawned outlives the abort.
                os.killpg(pid, 0)
        self.assertEqual(
            [
                events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
                events.RUN_CONTROL_ACTION_ABORT_TURN_REQUESTED,
                events.RUN_CONTROL_ACTION_ABORT_TURN_HONORED,
            ],
            self.run_control_actions(),
        )

    def test_aborted_review_turn_stops_the_run_with_the_review_pending(
        self,
    ) -> None:
        payload, harness_calls = self._run_loop(
            statuses=("pending",),
            review_harness="claude",
            pending_review=True,
            abort_during_turn_index=2,
        )

        self.assertEqual("stopped", payload["reason"])
        self.assertFalse(payload["paused"])
        self.assertEqual(["codex", "claude"], harness_calls)
        self.assertFalse(payload["review_completed"])
        self.assertEqual("pending", self._session_status("2"))
        self.assertIn("turn aborted by supervisor request", self._session_text("2"))
        self.assertEqual(
            [
                events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
                events.RUN_CONTROL_ACTION_ABORT_TURN_REQUESTED,
                events.RUN_CONTROL_ACTION_ABORT_TURN_HONORED,
            ],
            self.run_control_actions(),
        )
