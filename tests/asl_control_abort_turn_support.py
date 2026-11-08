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
    mailbox_appenders,
    plans_handoff,
    plans_models,
    plans_session_ids,
    review_session_body,
    run_control,
    run_loop,
    state,
    state_locks,
    turn_execution_artifacts,
    turn_execution_constants,
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


class AbortTurnOrchestrationTestsBase(unittest.TestCase):
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
        return (
            self.plan_dir / plans_session_ids.session_id_to_filename(session_id)
        ).read_text(encoding="utf-8")

    def _session_status(self, session_id: str) -> str:
        return plans_handoff.read_session_status(
            plans_models.SessionFile(
                plans_session_ids.parse_session_id(session_id),
                self.plan_dir / plans_session_ids.session_id_to_filename(session_id),
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
        runner_ends_turn: str | None = None,
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
        (``worker_self_pauses``). ``runner_ends_turn`` (``"timed_out"`` or
        ``"interrupted"``) instead plays a turn the runner itself ended
        inside the window, which is no compliance at all.
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
                if runner_ends_turn is not None:
                    return harnesses.HarnessResult(
                        name=name,
                        argv=[name, str(kwargs["prompt"])],
                        returncode=124 if runner_ends_turn == "timed_out" else 130,
                        stdout="",
                        stderr="",
                        timed_out=runner_ends_turn == "timed_out",
                        interrupted=runner_ends_turn == "interrupted",
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

        real_turn_paths = turn_execution_artifacts.turn_paths
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
                turn_execution_constants, "LIVE_CONTROL_POLL_INTERVAL_SECONDS", 0.02
            ),
            mock.patch.object(harnesses, "run_harness", side_effect=fake_run_harness),
            mock.patch.object(
                turn_execution_artifacts, "turn_paths", side_effect=fake_turn_paths
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
