"""AbortTurnOrchestrationTests.

Split from tests/test_asl_control.py."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    config,
    control_queue,
    events,
    harnesses,
    mailbox,
    mailbox_appenders,
    plans,
    review_session_body,
    run_control,
    run_loop,
    state,
    state_locks,
    turn_execution,
    turn_postflight,
)
from tests.support import (
    REPO_ROOT,
    FakeRunControlListener,
    init_git_repo,
    install_fake_harness,
    patched_env,
    read_jsonl,
    write_plan,
)


class AbortTurnOrchestrationTests(unittest.TestCase):
    """An aborted turn stops the run cleanly instead of pausing it."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.plan_dir = self.tmp / "plan"
        self.state_dir = self.tmp / "state"
        self.repo_root = self.tmp / "repo"
        self.config_home = self.tmp / "config"
        init_git_repo(self.repo_root)
        harness_config = {
            name: {"executable": str(install_fake_harness(self.tmp, name))}
            for name in ("codex", "claude")
        }
        config_path = self.config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(harness_config), encoding="utf-8")

    def _queue_abort(
        self,
        command_id: str,
        *,
        grace_seconds: int | None = None,
        reason: str | None = None,
    ) -> None:
        control_queue.append_command(
            self.plan_dir,
            control_queue.COMMAND_REQUEST_ABORT_TURN,
            state_dir=self.state_dir,
            grace_seconds=grace_seconds,
            reason=reason,
            command_id=command_id,
        )

    def _activate_mailbox(self) -> None:
        # Creating the file is what enables the mailbox, so seed it with an
        # already-answered record rather than pending guidance.
        seeded = mailbox_appenders.append_guidance(
            self.plan_dir, "seeded", state_dir=self.state_dir
        )
        mailbox_appenders.append_acknowledgement(
            self.plan_dir, seeded.seq, state_dir=self.state_dir
        )

    def _complete_next_pending(self) -> None:
        for session_file in sorted(self.plan_dir.glob("session-*.md")):
            text = session_file.read_text(encoding="utf-8")
            if "- **Status**: pending" not in text:
                continue
            session_file.write_text(
                text.replace("- **Status**: pending", "- **Status**: complete", 1),
                encoding="utf-8",
            )
            return

    def _session_text(self, session_id: str) -> str:
        return (self.plan_dir / plans.session_id_to_filename(session_id)).read_text(
            encoding="utf-8"
        )

    def _session_status(self, session_id: str) -> str:
        return plans.read_session_status(
            plans.SessionFile(
                plans.parse_session_id(session_id),
                self.plan_dir / plans.session_id_to_filename(session_id),
            )
        ).status

    def run_control_actions(self) -> list[str]:
        event_path = (
            state.plan_state_dir(self.plan_dir, self.state_dir)
            / events.EVENTS_FILE_NAME
        )
        if not event_path.exists():
            return []
        return [
            record["payload"]["action"]
            for record in read_jsonl(event_path)
            if record["name"] == events.RUN_CONTROL_EVENT
        ]

    def _run_loop(
        self,
        *,
        statuses: tuple[str, ...],
        review_harness: str | None = None,
        pending_review: bool = False,
        abort_during_turn_index: int | None = None,
        abort_before_turn_index: int | None = None,
        abort_before_run: bool = False,
        worker_complies: bool = False,
        worker_self_pauses: bool = False,
        grace_seconds: int = 30,
        reason: str = "operator needs the machine",
        enable_mailbox: bool = False,
        expected_exit_code: int = 0,
    ) -> tuple[dict[str, object], list[str]]:
        """Run the loop in-process with a harness that observes its own abort.

        ``abort_during_turn_index`` queues the abort from inside that turn's
        harness call and then blocks until the poll thread has armed the turn's
        signal, so the observation is deterministic without a real process. The
        harness then plays the non-compliant outcome (the wait loop's
        ``aborted`` result, exactly what session 3 produces on expiry) or one of
        the two compliant ones: a completion inside the window
        (``worker_complies``) or a self-pause inside it
        (``worker_self_pauses``).
        """
        write_plan(self.plan_dir, statuses)
        if enable_mailbox:
            self._activate_mailbox()
        if pending_review:
            review_session_body.ensure_generated_review_session(
                self.plan_dir, review_harness=str(review_harness)
            )
        if abort_before_run:
            self._queue_abort("before-run", grace_seconds=grace_seconds, reason=reason)
        harness_calls: list[str] = []

        def fake_run_harness(name: str, **kwargs: object) -> harnesses.HarnessResult:
            if kwargs.get("plan_dir") is None:
                return harnesses.HarnessResult(
                    name=name,
                    argv=[name],
                    returncode=0,
                    stdout="",
                    stderr="",
                )
            harness_calls.append(name)
            if len(harness_calls) == abort_during_turn_index:
                self._queue_abort(
                    f"during-turn-{len(harness_calls)}",
                    grace_seconds=grace_seconds,
                    reason=reason,
                )
                abort = kwargs["abort"]
                assert isinstance(abort, harnesses.AbortSignal)
                deadline = time.monotonic() + 10.0
                while not abort.armed and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(
                    abort.armed,
                    "the poll thread never observed the queued abort",
                )
                if worker_self_pauses:
                    # The wind-down the abort guidance actually asks for: the
                    # worker leaves its session pending and pauses the run.
                    return harnesses.HarnessResult(
                        name=name,
                        argv=[name, str(kwargs["prompt"])],
                        returncode=0,
                        stdout=(
                            "RUNNER_PAUSE: blocked - supervisor abort acknowledged\n"
                        ),
                        stderr="",
                    )
                if not worker_complies:
                    # What the session-3 wait loop returns once the grace
                    # window expires and the process group is killed.
                    return harnesses.HarnessResult(
                        name=name,
                        argv=[name, str(kwargs["prompt"])],
                        returncode=137,
                        stdout="",
                        stderr="",
                        aborted=True,
                    )
            self._complete_next_pending()
            return harnesses.HarnessResult(
                name=name,
                argv=[name, str(kwargs["prompt"])],
                returncode=0,
                stdout=f"{name} completed\n",
                stderr="",
            )

        real_turn_paths = turn_execution.turn_paths
        turn_paths_calls = 0

        def fake_turn_paths(*args: object, **kwargs: object):
            nonlocal turn_paths_calls
            turn_paths_calls += 1
            if turn_paths_calls == abort_before_turn_index:
                self._queue_abort(
                    f"before-turn-{turn_paths_calls}",
                    grace_seconds=grace_seconds,
                    reason=reason,
                )
            return real_turn_paths(*args, **kwargs)

        listener = FakeRunControlListener(current_harness="codex")

        def fake_for_run(*_args: object, **kwargs: object):
            listener.on_action = kwargs.get("on_action")
            listener.on_status = kwargs.get("on_status")
            return listener

        args = argparse.Namespace(
            plan_dir=str(self.plan_dir),
            cwd=str(self.repo_root),
            harness="codex",
            review_harness=review_harness,
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
            ),
            mock.patch.object(
                turn_execution, "LIVE_CONTROL_POLL_INTERVAL_SECONDS", 0.02
            ),
            mock.patch.object(harnesses, "run_harness", side_effect=fake_run_harness),
            mock.patch.object(
                turn_execution, "turn_paths", side_effect=fake_turn_paths
            ),
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
        self.assertEqual(expected_exit_code, exit_code, payload)
        return payload, harness_calls

    def test_killed_turn_stops_the_run_and_returns_the_session_to_pending(
        self,
    ) -> None:
        payload, harness_calls = self._run_loop(
            statuses=("pending", "pending"),
            abort_during_turn_index=1,
        )

        # A supervisor asked for this, so it is a clean stop and not a fault.
        self.assertEqual("stopped", payload["reason"])
        self.assertFalse(payload["paused"])
        self.assertIsNone(payload.get("pause_reason"))
        self.assertEqual(["codex"], harness_calls)
        self.assertEqual([], payload["completed_sessions"])
        self.assertEqual("pending", self._session_status("1"))
        self.assertEqual(1, payload["final_snapshot"]["next_pending"])
        self.assertEqual(
            [
                events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
                events.RUN_CONTROL_ACTION_ABORT_TURN_REQUESTED,
                events.RUN_CONTROL_ACTION_ABORT_TURN_HONORED,
            ],
            self.run_control_actions(),
        )
        self.assertFalse(payload["control"]["abort_turn_requested"])

    def test_killed_turn_records_why_the_session_holds_no_completion(
        self,
    ) -> None:
        self._run_loop(
            statuses=("pending",),
            abort_during_turn_index=1,
            reason="host reboot",
        )

        note = self._session_text("1")
        self.assertIn("turn aborted by supervisor request", note)
        self.assertIn("host reboot", note)
        self.assertIn("holds no completion", note)

    def test_a_guard_pause_still_acknowledges_the_killed_process(self) -> None:
        def refuse(ctx, **kwargs):
            ctx.pause(
                reason=events.PAUSE_REASON_WORKSPACE_WRITE_GUARD_UNAVAILABLE,
                message="guard evidence unavailable",
                details=kwargs["session_details"],
            )
            return turn_postflight.PostTurnGuards(
                stopped=True, terminating_disposition=None
            )

        with mock.patch.object(
            turn_postflight, "enforce_post_turn_guards", side_effect=refuse
        ):
            payload, harness_calls = self._run_loop(
                statuses=("pending",),
                abort_during_turn_index=1,
                reason="stop the stuck harness",
                expected_exit_code=1,
            )
        self.assertTrue(payload["paused"])
        self.assertEqual(
            events.PAUSE_REASON_WORKSPACE_WRITE_GUARD_UNAVAILABLE,
            payload["pause_reason"],
        )
        self.assertFalse(payload["ok"])
        self.assertEqual([], payload["completed_sessions"])
        self.assertEqual("pending", self._session_status("1"))
        self.assertEqual(
            1,
            self.run_control_actions().count(
                events.RUN_CONTROL_ACTION_ABORT_TURN_HONORED
            ),
        )
        self.assertFalse(payload["control"]["abort_turn_requested"])

    def test_killed_turn_leaves_the_abort_guidance_readable(self) -> None:
        self._run_loop(
            statuses=("pending",),
            abort_during_turn_index=1,
            enable_mailbox=True,
            reason="host reboot",
        )

        pending = mailbox.read_mailbox(
            self.plan_dir, state_dir=self.state_dir
        ).pending_guidance
        self.assertEqual(1, len(pending))
        self.assertIn("host reboot", pending[0].text)
        self.assertIn("Supervisor abort requested", pending[0].text)

    def test_compliant_turn_is_recorded_as_satisfied_without_a_kill(
        self,
    ) -> None:
        payload, harness_calls = self._run_loop(
            statuses=("pending",),
            abort_during_turn_index=1,
            worker_complies=True,
        )

        # The turn ended on its own inside the window, so nothing was killed
        # and the completion path stays in charge of the outcome.
        self.assertEqual([1], payload["completed_sessions"])
        self.assertEqual("complete", self._session_status("1"))
        self.assertEqual(["codex"], harness_calls)
        self.assertFalse(payload["paused"])
        self.assertEqual("all-complete", payload["reason"])
        self.assertEqual(
            [
                events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
                events.RUN_CONTROL_ACTION_ABORT_TURN_REQUESTED,
                events.RUN_CONTROL_ACTION_ABORT_TURN_SATISFIED_BY_WORKER,
            ],
            self.run_control_actions(),
        )
        self.assertNotIn("turn aborted by supervisor request", self._session_text("1"))
        self.assertFalse(payload["control"]["abort_turn_requested"])

    def test_compliant_completion_lets_the_run_continue_to_the_next_session(
        self,
    ) -> None:
        payload, harness_calls = self._run_loop(
            statuses=("pending", "pending"),
            abort_during_turn_index=1,
            worker_complies=True,
        )

        # Nothing was killed and nothing was reverted, so the abort buys the
        # supervisor no stop at all: the loop moves on to session 2.
        self.assertEqual([1, 2], payload["completed_sessions"])
        self.assertEqual(["codex", "codex"], harness_calls)
        self.assertEqual("all-complete", payload["reason"])
        self.assertFalse(payload["paused"])
        self.assertEqual(
            [
                events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
                events.RUN_CONTROL_ACTION_ABORT_TURN_REQUESTED,
                events.RUN_CONTROL_ACTION_ABORT_TURN_SATISFIED_BY_WORKER,
            ],
            self.run_control_actions(),
        )

    def test_worker_that_self_pauses_inside_the_window_pauses_the_run(
        self,
    ) -> None:
        payload, harness_calls = self._run_loop(
            statuses=("pending", "pending"),
            abort_during_turn_index=1,
            worker_self_pauses=True,
            expected_exit_code=1,
        )

        # The worker wound down as asked, so its own pause owns the outcome:
        # the run is paused, not stopped, and carries the worker's reason.
        self.assertTrue(payload["paused"])
        self.assertFalse(payload["ok"])
        self.assertEqual("blocked", payload["reason"])
        self.assertEqual("blocked", payload["pause_reason"])
        self.assertEqual([], payload["completed_sessions"])
        self.assertEqual(["codex"], harness_calls)
        self.assertEqual("pending", self._session_status("1"))
        self.assertNotIn("turn aborted by supervisor request", self._session_text("1"))
        self.assertEqual(
            [
                events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
                events.RUN_CONTROL_ACTION_ABORT_TURN_REQUESTED,
                events.RUN_CONTROL_ACTION_ABORT_TURN_SATISFIED_BY_WORKER,
            ],
            self.run_control_actions(),
        )
        self.assertFalse(payload["control"]["abort_turn_requested"])

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
                events.RUN_CONTROL_ACTION_ABORT_TURN_HONORED,
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
                turn_execution, "LIVE_CONTROL_POLL_INTERVAL_SECONDS", 0.02
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


if __name__ == "__main__":
    unittest.main()
