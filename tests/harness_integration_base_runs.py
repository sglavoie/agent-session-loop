"""Locked-run drivers for the fake-harness integration tests."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    config,
    events,
    harnesses,
    run_control,
    run_loop,
    run_selection,
    state,
    state_locks,
)
from tests.harness_integration_base_setup import HarnessSetupMixin
from tests.harness_integration_support import LOOP_RUN_TIMEOUT_SECONDS
from tests.support import (
    REPO_ROOT,
    FakeRunControlListener,
    patched_env,
)


class HarnessRunMixin(HarnessSetupMixin):
    def run_locked_with_listener(
        self,
        *,
        mode: str,
        statuses: tuple[str, ...],
        listener: FakeRunControlListener,
        cwd: Path | None = None,
        commit_mode: str | None = None,
        harness_model: str | None = None,
        effort: str | None = None,
        note: str | None = None,
        extra_env: dict[str, str] | None = None,
        harness_config_overrides: dict[str, str] | None = None,
        before_run: Callable[[Path, Path], None] | None = None,
    ) -> tuple[int, dict[str, Any], Path, Path, Path]:
        plan_dir, state_dir, config_home, fake_codex = self.setup_loop(
            statuses=statuses,
            harness_config_overrides=harness_config_overrides,
        )
        real_state_dir = state.plan_state_dir(plan_dir, state_dir)
        if listener.state_path is None:
            listener.state_path = real_state_dir / events.STATE_FILE_NAME
        if listener.events_path is None:
            listener.events_path = real_state_dir / events.EVENTS_FILE_NAME
        if before_run is not None:
            before_run(plan_dir, state_dir)
        run_cwd = REPO_ROOT if cwd is None else cwd
        args = argparse.Namespace(
            plan_dir=str(plan_dir),
            cwd=str(run_cwd),
            harness="codex",
            state_dir=str(state_dir),
            json=False,
            dry_run=False,
            unsafe_bypass=True,
            timeout=None,
            max_sessions=None,
            commit_mode=commit_mode,
            harness_model=harness_model,
            effort=effort,
            note=note,
            allow_not_loop_ready=True,
        )
        env_updates = {
            "XDG_CONFIG_HOME": str(config_home),
            "FAKE_CODEX_MODE": mode,
            "FAKE_CODEX_SLEEP": "5",
            "PYTHONPATH": str(REPO_ROOT)
            + os.pathsep
            + os.environ.get("PYTHONPATH", ""),
        }
        if extra_env is not None:
            env_updates.update(extra_env)

        def fake_for_run(*_args, **kwargs):
            listener.on_action = kwargs.get("on_action")
            listener.on_status = kwargs.get("on_status")
            return listener

        with (
            patched_env(**env_updates),
            mock.patch.object(
                run_control.RunControlListener,
                "for_run",
                side_effect=fake_for_run,
            ),
        ):
            lock_info = state_locks.acquire_lock(plan_dir, state_dir)
            try:
                exit_code, payload = run_loop.run_locked(
                    args=args,
                    cwd=run_cwd,
                    timeout=None,
                    max_sessions=None,
                    commit_mode=commit_mode,
                    lock_info=lock_info,
                    run_id="test-run",
                )
            finally:
                state_locks.release_lock(plan_dir, state_dir)
        return exit_code, payload, plan_dir, state_dir, fake_codex

    def run_with_mocked_harness_switch(
        self,
        *,
        harness_model: str | None = None,
        effort: str | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, Any], list[dict[str, Any]], Path, Path]:
        listener = FakeRunControlListener()
        calls: list[dict[str, Any]] = []

        def complete_next_pending(plan_dir: Path) -> None:
            for session_file in sorted(plan_dir.glob("session-*.md")):
                text = session_file.read_text(encoding="utf-8")
                if "- **Status**: pending" not in text:
                    continue
                session_file.write_text(
                    text.replace("- **Status**: pending", "- **Status**: complete", 1),
                    encoding="utf-8",
                )
                return

        def fake_run_harness(name: str, **kwargs: object) -> harnesses.HarnessResult:
            calls.append({"harness": name, **kwargs})
            complete_next_pending(Path(kwargs["plan_dir"]))
            if len(calls) == 1 and listener.on_action is not None:
                listener.on_action(
                    run_control.RunControlAction(
                        action="harness-selected",
                        stop_after_current=False,
                        next_harness="claude",
                    )
                )
            return harnesses.HarnessResult(
                name=name,
                argv=[name, str(kwargs["prompt"])],
                returncode=0,
                stdout=f"{name} completed\n",
                stderr="",
            )

        with mock.patch.object(
            harnesses,
            "run_harness",
            side_effect=fake_run_harness,
        ):
            exit_code, payload, plan_dir, state_dir, _fake_codex = (
                self.run_locked_with_listener(
                    mode="success",
                    statuses=("pending", "pending"),
                    listener=listener,
                    harness_model=harness_model,
                    effort=effort,
                    extra_env=extra_env,
                    harness_config_overrides={"claude": sys.executable},
                )
            )
        return exit_code, payload, calls, plan_dir, state_dir

    def run_loop_command(
        self,
        *,
        mode: str,
        plan_dir: Path,
        state_dir: Path,
        config_home: Path,
        cwd: Path | None = None,
        timeout: int | None = None,
        max_sessions: int | None = None,
        commit_mode: str | None = None,
        harness_model: str | None = None,
        review_harness: str | None = None,
        review_harness_model: str | None = None,
        effort: str | None = None,
        note: str | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
        run_cwd = REPO_ROOT if cwd is None else cwd
        command = [
            sys.executable,
            "-m",
            "agent_session_loop",
            "run",
            str(plan_dir),
            "--cwd",
            str(run_cwd),
            "--harness",
            "codex",
            "--state-dir",
            str(state_dir),
            "--json",
            "--allow-not-loop-ready",
        ]
        if timeout is not None:
            command.extend(["--timeout", str(timeout)])
        if max_sessions is not None:
            command.extend(["--max-sessions", str(max_sessions)])
        if commit_mode == run_selection.COMMIT_MODE_PER_SESSION:
            command.append("--commit-per-session")
        elif commit_mode == run_selection.COMMIT_MODE_END:
            command.append("--commit-at-end")
        elif commit_mode is not None:
            raise ValueError(f"unsupported commit_mode: {commit_mode}")
        if harness_model is not None:
            command.extend(["--harness-model", harness_model])
        if review_harness is not None:
            command.extend(["--review-harness", review_harness])
        if review_harness_model is not None:
            command.extend(["--review-harness-model", review_harness_model])
        if effort is not None:
            command.extend(["--effort", effort])
        if note is not None:
            command.extend(["--note", note])

        env = os.environ.copy()
        env["XDG_CONFIG_HOME"] = str(config_home)
        env["FAKE_CODEX_MODE"] = mode
        env["FAKE_CODEX_SLEEP"] = "5"
        env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        if extra_env is not None:
            env.update(extra_env)

        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=LOOP_RUN_TIMEOUT_SECONDS,
            check=False,
        )
        return completed, json.loads(completed.stdout)

    def enable_fake_review_harness(
        self,
        config_home: Path,
        fake_codex: Path,
    ) -> None:
        config_path = config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        configured = json.loads(config_path.read_text(encoding="utf-8"))
        configured["claude"] = {"executable": str(fake_codex)}
        config_path.write_text(json.dumps(configured), encoding="utf-8")

    def run_locked_review_run(
        self,
        *,
        plan_dir: Path,
        state_dir: Path,
        config_home: Path,
        run_id: str,
        review_outcomes: str,
        max_review_fix_cycles: int,
        cwd: Path | None = None,
    ) -> tuple[int, dict[str, Any]]:
        """Run one review-enabled loop in process over an existing plan.

        ``cwd`` is the workspace the run guards and commits in. It defaults to
        this checkout, which is fine for a run that only ever reads it; any run
        that reaches a gate on repository cleanliness must pass its own
        temporary repository instead, or it inherits whatever the developer
        happens to have uncommitted here.
        """
        run_cwd = REPO_ROOT if cwd is None else cwd
        listener = FakeRunControlListener()
        real_state_dir = state.plan_state_dir(plan_dir, state_dir)
        listener.state_path = real_state_dir / events.STATE_FILE_NAME
        listener.events_path = real_state_dir / events.EVENTS_FILE_NAME
        args = argparse.Namespace(
            plan_dir=str(plan_dir),
            cwd=str(run_cwd),
            harness="codex",
            review_harness="claude",
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
            max_review_fix_cycles=max_review_fix_cycles,
        )

        def fake_for_run(*_args, **kwargs):
            listener.on_action = kwargs.get("on_action")
            listener.on_status = kwargs.get("on_status")
            return listener

        with (
            patched_env(
                XDG_CONFIG_HOME=str(config_home),
                FAKE_CODEX_MODE="success-with-review",
                FAKE_CODEX_REVIEW_OUTCOMES=review_outcomes,
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
            lock_info = state_locks.acquire_lock(plan_dir, state_dir)
            try:
                return run_loop.run_locked(
                    args=args,
                    cwd=run_cwd,
                    timeout=None,
                    max_sessions=None,
                    commit_mode=None,
                    lock_info=lock_info,
                    run_id=run_id,
                )
            finally:
                state_locks.release_lock(plan_dir, state_dir)

    def run_loop(
        self,
        *,
        mode: str,
        statuses: tuple[str, ...],
        cwd: Path | None = None,
        timeout: int | None = None,
        max_sessions: int | None = None,
        commit_mode: str | None = None,
        harness_model: str | None = None,
        effort: str | None = None,
        note: str | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, Any], Path, Path, Path]:
        plan_dir, state_dir, config_home, fake_codex = self.setup_loop(
            statuses=statuses
        )
        completed, payload = self.run_loop_command(
            mode=mode,
            plan_dir=plan_dir,
            state_dir=state_dir,
            config_home=config_home,
            cwd=cwd,
            timeout=timeout,
            max_sessions=max_sessions,
            commit_mode=commit_mode,
            harness_model=harness_model,
            effort=effort,
            note=note,
            extra_env=extra_env,
        )
        return completed, payload, plan_dir, state_dir, fake_codex
