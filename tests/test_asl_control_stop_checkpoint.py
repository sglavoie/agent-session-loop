"""PreTurnStopCheckpointTests.

Split from tests/test_asl_control.py."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    config,
    control_queue,
    events,
    harnesses,
    plans,
    review_session_body,
    run_control,
    run_loop,
    state,
    state_locks,
    turn_execution,
)
from tests.support import (
    REPO_ROOT,
    FakeRunControlListener,
    init_git_repo,
    install_fake_harness,
    patched_env,
    write_plan,
)


class PreTurnStopCheckpointTests(unittest.TestCase):
    """A queued stop must never let another turn start."""

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

    def _queue_stop(self, command_id: str) -> None:
        control_queue.append_command(
            self.plan_dir,
            control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT,
            state_dir=self.state_dir,
            command_id=command_id,
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

    def _session_status(self, session_id: str) -> str:
        return plans.read_session_status(
            plans.SessionFile(
                plans.parse_session_id(session_id),
                self.plan_dir / plans.session_id_to_filename(session_id),
            )
        ).status

    def _run_loop(
        self,
        *,
        statuses: tuple[str, ...],
        review_harness: str | None = None,
        pending_review: bool = False,
        stop_before_run: bool = False,
        stop_before_turn_index: int | None = None,
        stop_during_turn_index: int | None = None,
    ) -> tuple[dict[str, object], list[str]]:
        """Run the loop in-process with a mocked harness.

        ``stop_before_turn_index`` queues the stop while the turn with that
        index is being built, i.e. after the previous iteration's tail check
        already ran. ``stop_during_turn_index`` queues it from inside that
        turn's harness call, and ``stop_before_run`` queues it before the run
        starts at all.
        """
        write_plan(self.plan_dir, statuses)
        if pending_review:
            review_session_body.ensure_generated_review_session(
                self.plan_dir, review_harness=str(review_harness)
            )
        if stop_before_run:
            self._queue_stop("before-run")
        harness_calls: list[str] = []

        def fake_run_harness(name: str, **kwargs: object) -> harnesses.HarnessResult:
            # The terminal supervisor audit also goes through run_harness, but
            # with plan_dir=None; only loop turns drive the plan forward.
            if kwargs.get("plan_dir") is not None:
                harness_calls.append(name)
                self._complete_next_pending()
                if len(harness_calls) == stop_during_turn_index:
                    self._queue_stop(f"during-turn-{len(harness_calls)}")
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
            if turn_paths_calls == stop_before_turn_index:
                self._queue_stop(f"before-turn-{turn_paths_calls}")
            return real_turn_paths(*args, **kwargs)

        listener = FakeRunControlListener(current_harness="codex")
        real_state_dir = state.plan_state_dir(self.plan_dir, self.state_dir)
        listener.state_path = real_state_dir / events.STATE_FILE_NAME
        listener.events_path = real_state_dir / events.EVENTS_FILE_NAME

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
        self.assertEqual(0, exit_code, payload)
        return payload, harness_calls

    def test_stop_between_turns_is_honored_before_the_next_turn(self) -> None:
        payload, harness_calls = self._run_loop(
            statuses=("pending", "pending"),
            stop_before_turn_index=2,
        )

        self.assertEqual("stopped", payload["reason"])
        self.assertFalse(payload["paused"])
        self.assertEqual([1], payload["completed_sessions"])
        self.assertEqual(1, len(payload["turns"]))
        self.assertEqual(["codex"], harness_calls)
        self.assertEqual("pending", self._session_status("2"))
        self.assertEqual(2, payload["final_snapshot"]["next_pending"])

    def test_stop_between_turns_is_honored_before_a_review_turn(self) -> None:
        payload, harness_calls = self._run_loop(
            statuses=("pending",),
            review_harness="claude",
            pending_review=True,
            stop_before_turn_index=2,
        )

        self.assertEqual("stopped", payload["reason"])
        self.assertFalse(payload["paused"])
        self.assertEqual(1, len(payload["turns"]))
        self.assertEqual(["codex"], harness_calls)
        self.assertFalse(payload["review_completed"])
        self.assertEqual("pending", self._session_status("2"))

    def test_stop_queued_before_the_run_still_runs_the_first_session(
        self,
    ) -> None:
        payload, harness_calls = self._run_loop(
            statuses=("pending", "pending"),
            stop_before_run=True,
        )

        self.assertEqual("stopped", payload["reason"])
        self.assertEqual([1], payload["completed_sessions"])
        self.assertEqual(1, len(payload["turns"]))
        self.assertEqual(["codex"], harness_calls)
        self.assertEqual("pending", self._session_status("2"))

    def test_stop_on_the_last_session_still_skips_review_work(self) -> None:
        payload, harness_calls = self._run_loop(
            statuses=("pending",),
            review_harness="claude",
            stop_during_turn_index=1,
        )

        self.assertEqual("stopped", payload["reason"])
        self.assertEqual([1], payload["completed_sessions"])
        self.assertEqual(1, len(payload["turns"]))
        self.assertEqual(["codex"], harness_calls)
        self.assertEqual("stopped", payload["review_skipped_reason"])
        self.assertEqual([], list(self.plan_dir.glob("session-2*.md")))


if __name__ == "__main__":
    unittest.main()
