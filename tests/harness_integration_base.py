"""Shared fake-harness setup helpers for the harness integration tests."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    clock,
    config,
    events,
    harnesses,
    run_control,
    run_loop,
    run_selection,
    state,
    state_locks,
)
from tests.harness_integration_support import LOOP_RUN_TIMEOUT_SECONDS
from tests.support import (
    REPO_ROOT,
    FakeRunControlListener,
    _contract_block,
    install_fake_codex,
    patched_env,
    read_jsonl,
    write_plan,
    write_plan_with_ids,
)


class FakeHarnessIntegrationBase(unittest.TestCase):
    def setup_loop(
        self,
        *,
        statuses: tuple[str, ...],
        harness_config_overrides: dict[str, str] | None = None,
    ) -> tuple[Path, Path, Path, Path]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmp_dir = Path(tmp.name)
        plan_dir = tmp_dir / "plan"
        state_dir = tmp_dir / "state"
        config_home = tmp_dir / "config"
        config_path = config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        config_path.parent.mkdir(parents=True, exist_ok=True)
        fake_codex = install_fake_codex(tmp_dir)
        harness_config = {"codex": {"executable": str(fake_codex)}}
        if harness_config_overrides is not None:
            for harness_name, executable in harness_config_overrides.items():
                harness_config[harness_name] = {"executable": executable}
        config_path.write_text(
            json.dumps(harness_config),
            encoding="utf-8",
        )
        write_plan(plan_dir, statuses)
        return plan_dir, state_dir, config_home, fake_codex

    def setup_loop_with_ids(
        self,
        *,
        statuses: tuple[tuple[str, str], ...],
    ) -> tuple[Path, Path, Path, Path]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmp_dir = Path(tmp.name)
        plan_dir = tmp_dir / "plan"
        state_dir = tmp_dir / "state"
        config_home = tmp_dir / "config"
        config_path = config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        config_path.parent.mkdir(parents=True, exist_ok=True)
        fake_codex = install_fake_codex(tmp_dir)
        config_path.write_text(
            json.dumps({"codex": {"executable": str(fake_codex)}}),
            encoding="utf-8",
        )
        write_plan_with_ids(plan_dir, statuses)
        return plan_dir, state_dir, config_home, fake_codex

    def setup_commit_harness(
        self,
        tmp_dir: Path,
        *,
        mode: str = "success",
        mutation_path: str | None = "worked.txt",
        fail_limit: int | None = None,
    ) -> tuple[Path, dict[str, str]]:
        """Give a run something to commit and log each commit-message turn.

        Auto-commit re-enters this checkout as a subprocess that resolves its
        generation harness from the run's own ``XDG_CONFIG_HOME``, so the fake
        codex fixture answers a commit turn with no PATH shim. Unlike the fake
        ``spec`` binary this replaces, ``asl commit`` refuses a clean worktree;
        appending to ``mutation_path`` leaves every turn a distinct change to
        commit. ``mutation_path=None`` is for a run whose cwd is dirtied by the
        turn itself. ``fail_limit`` bounds a failing mode to its first N turns,
        which is how a transient failure that heals on retry is staged.
        """
        log_path = tmp_dir / "fake-commit.jsonl"
        env = {"FAKE_CODEX_COMMIT_LOG": str(log_path)}
        if mode != "success":
            env["FAKE_CODEX_COMMIT_MODE"] = mode
        if fail_limit is not None:
            env["FAKE_CODEX_COMMIT_FAIL_LIMIT"] = str(fail_limit)
        if mutation_path is not None:
            env["FAKE_CODEX_MUTATION_PATH"] = mutation_path
            env["FAKE_CODEX_MUTATION_ACTION"] = "append"
        return log_path, env

    def event_index(
        self,
        records: list[dict[str, Any]],
        name: str,
        **payload_values: object,
    ) -> int:
        for index, record in enumerate(records):
            if record.get("name") != name:
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            if all(payload.get(key) == value for key, value in payload_values.items()):
                return index
        raise AssertionError(f"event not found: {name} {payload_values}")

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

    def idle_started_at(self, turn: dict[str, Any]) -> datetime:
        started_at = clock.parse_datetime(turn["started_at"])
        assert isinstance(started_at, datetime)
        return started_at - timedelta(seconds=turn["idle_seconds_since_previous_turn"])

    def last_event_created_at(self, events_path: Path, name: str) -> datetime:
        created_at = [
            clock.parse_datetime(record["created_at"])
            for record in read_jsonl(events_path)
            if record["name"] == name
        ][-1]
        assert created_at is not None
        return created_at

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

    def loop_env(
        self,
        *,
        config_home: Path,
        mode: str,
        extra: dict[str, str] | None = None,
    ) -> dict[str, str]:
        env = os.environ.copy()
        env["XDG_CONFIG_HOME"] = str(config_home)
        env["FAKE_CODEX_MODE"] = mode
        env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        if extra is not None:
            env.update(extra)
        return env

    def run_command(
        self,
        *,
        plan_dir: Path,
        state_dir: Path,
        cwd: Path | None = None,
        json_output: bool = False,
        max_sessions: int | None = None,
        commit_mode: str | None = None,
        harness_model: str | None = None,
        effort: str | None = None,
        note: str | None = None,
    ) -> list[str]:
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
            "--allow-not-loop-ready",
        ]
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
        if effort is not None:
            command.extend(["--effort", effort])
        if note is not None:
            command.extend(["--note", note])
        if json_output:
            command.append("--json")
        return command

    def read_run_payload(
        self,
        *,
        plan_dir: Path,
        state_dir: Path,
    ) -> dict[str, Any]:
        state_path = state.plan_state_dir(plan_dir, state_dir) / events.STATE_FILE_NAME
        return json.loads(state_path.read_text(encoding="utf-8"))

    def settlement_check_script(self, tmp_dir: Path, *, heals: bool = True) -> Path:
        """Write the check the retained-work settlement regressions drive with.

        ``heals=True`` fails the first run and passes every one after it, which
        is the arrangement the settlement exists for: the pause the first
        process records is real, so the second process is stopped by the
        worktree or by nothing. ``heals=False`` never passes and fails the same
        way every time, which is how the verification repair budget is spent to
        exhaustion. Either way the marker lives outside every guarded
        repository, so the only dirt a relaunch can trip over is the turn's own
        retained work.
        """
        marker = tmp_dir / "verification-attempted"
        script = tmp_dir / "verify-once.sh"
        lines = ["#!/bin/sh"]
        if heals:
            lines.append(f'if [ -f "{marker}" ]; then exit 0; fi')
            lines.append(f': > "{marker}"')
        lines.extend(["exit 1", ""])
        script.write_text("\n".join(lines), encoding="utf-8")
        return script

    def settlement_contract(self, script: Path, *, repo: str | None = None) -> str:
        """Render the one-check contract that runs ``script``."""
        check: dict[str, Any] = {
            "type": "command",
            "name": "verify-once",
            "argv": ["sh", str(script)],
            "timeout_seconds": 60,
        }
        if repo is not None:
            check["repo"] = repo
        return _contract_block(json.dumps(check), session="1")

    def refuse_first_commits_hook(
        self, repo_dir: Path, tmp_dir: Path, *, count: int
    ) -> Path:
        """Refuse the first ``count`` commits this repository is asked to make.

        ``git commit --no-verify`` still runs ``prepare-commit-msg``, so this is
        the layer a settlement is really exposed at -- the commit itself,
        failing the way a repository policy hook fails, rather than the fake
        harness's commit-message failure mode, which a ``--message-file`` commit
        never reaches. Every refusal is appended to the returned log so a test
        can assert how many attempts were actually spent, and both the log and
        the counter live outside the repository so refusing leaves no dirt of
        its own.
        """
        refusals = tmp_dir / "hook-refusals.log"
        hook = repo_dir / ".git" / "hooks" / "prepare-commit-msg"
        hook.write_text(
            "\n".join(
                [
                    "#!/bin/sh",
                    f'attempts="{tmp_dir / "hook-attempts.log"}"',
                    'printf "x\\n" >> "$attempts"',
                    'seen=$(wc -l < "$attempts" | tr -d " ")',
                    f'if [ "$seen" -le {count} ]; then',
                    f'  printf "refused\\n" >> "{refusals}"',
                    '  echo "prepare-commit-msg refused this commit" >&2',
                    "  exit 1",
                    "fi",
                    "exit 0",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        hook.chmod(0o755)
        return refusals

    def _leftover_script(self, tmp_dir: Path, marker: Path) -> Path:
        """A process that publishes its PID atomically and then outlives us.

        Its streams are closed by the caller rather than inherited, so a
        surviving leftover never holds a captured pipe open: without that the
        run pays a full bounded reader join before any sweep happens.
        """
        script = tmp_dir / "leftover.py"
        script.write_text(
            "import os, pathlib, sys, time\n"
            "target = pathlib.Path(sys.argv[1])\n"
            "pending = target.with_suffix('.pending')\n"
            "pending.write_text(str(os.getpid()))\n"
            "os.replace(pending, target)\n"
            "time.sleep(300)\n",
            encoding="utf-8",
        )
        return script

    def sweep_events(self, plan_dir: Path, state_dir: Path) -> list[dict]:
        records = read_jsonl(
            state.plan_state_dir(plan_dir, state_dir) / events.EVENTS_FILE_NAME
        )
        return [
            record
            for record in records
            if record.get("name") == events.PROCESS_SWEEP_EVENT
        ]
