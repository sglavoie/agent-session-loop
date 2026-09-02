"""Fake-harness end-to-end runner integration tests.

Split from tests/test_agent_session_loop.py."""

from __future__ import annotations

import argparse
import ast
import errno
import io
import json
import os
import pty
import select
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from collections.abc import Callable, Mapping
from contextlib import redirect_stderr, redirect_stdout, suppress
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    cli,
    commit_message,
    config,
    contracts,
    control_queue,
    events,
    harness_failures,
    harnesses,
    pause_policy,
    plans,
    process_tree,
    progress_repair,
    review_session_body,
    run_control,
    runner,
    state,
    task_packet,
    turn_execution,
    verification,
)
from tests.support import (
    FAKE_CODEX_FIXTURE,
    REPO_ROOT,
    FakeRunControlListener,
    assert_marked_pids_alive,
    assert_marked_pids_exited,
    git_stdout,
    init_git_repo,
    install_fake_amx,
    install_fake_codex,
    install_fake_harness,
    kill_marked_pids,
    patched_env,
    read_jsonl,
    wait_until,
    write_plan,
    write_plan_with_ids,
    write_process_tree_scripts,
)

# Bounds a wedged child so it cannot hang the suite; it is not a latency
# assertion, so it stays far above what a run needs on a machine already
# saturated by the parallel shards.
RUN_EXIT_TIMEOUT_SECONDS = 120

# The same backstop for a full ``agent_session_loop run`` driven as a
# subprocess. These invocations take roughly five seconds idle and half again
# that under the parallel shards, so the former inline bounds of 10-15s were
# close enough to real latency to fail as a timeout rather than as the
# assertion the test actually makes. Like the bound above it is not a latency
# assertion: it exists so a hung run surfaces as a failure instead of running
# out the suite's own clock.
LOOP_RUN_TIMEOUT_SECONDS = 120

# The exact message the harness returned in run 20260814T210616107741Z-55923,
# which fell through to a fail-closed ``nonzero-exit`` and paused the run with
# four sessions still pending (ASI-125).
OBSERVED_SOCKET_CLOSURE = (
    "API Error: The socket connection was closed unexpectedly. For more "
    "information, pass `verbose: true` in the second argument to fetch()"
)

# The exact message the harness returned in run 20260825T004049070293Z-54505
# (turn-0001-session-1/last-message.txt). The classifier's literal
# "authentication" alternative did not match the verb form, so it fell through
# to the fail-closed ``nonzero-exit`` and the pause projected ``reply`` (ASI-135).
OBSERVED_AUTH_EXPIRY = (
    "Failed to authenticate: OAuth session expired and could not be refreshed"
)

# The launch-time diagnostic every observed Codex turn opens with: its MCP
# client dies, the turn runs to completion anyway (ASI-1).
OBSERVED_LAUNCH_CLOSURE = (
    "2026-08-16T02:38:52.929927Z ERROR rmcp::transport::worker: worker quit "
    "with fatal: Transport channel closed, when "
    'Client(HttpRequest(HttpRequest("http/request failed: error sending '
    'request for url (http://127.0.0.1:8765/mcp)")))'
)


class _PythonScriptCopilotAdapter(harnesses.CopilotAdapter):
    """Run a synthetic Copilot harness through Python instead of direct exec.

    macOS attaches ``com.apple.provenance`` to locally generated executables
    and may open a Gatekeeper verification window when these temporary test
    scripts are executed directly.  Making the trusted Python interpreter the
    executable preserves the adapter arguments and process-group behavior
    without asking Gatekeeper to assess each disposable script.
    """

    def __init__(self, script: Path) -> None:
        super().__init__(executable=sys.executable)
        self.script = script

    def build_argv(
        self,
        *,
        cwd: Path,
        plan_dir: Path | None,
        prompt: str,
        unsafe_bypass: bool = False,
        last_message_path: Path | None = None,
        model: str | None = None,
        effort: str | None = None,
        access: harnesses.AccessProfile = harnesses.WORKER_ACCESS,
        env: Mapping[str, str] | None = None,
    ) -> list[str]:
        argv = super().build_argv(
            cwd=cwd,
            plan_dir=plan_dir,
            prompt=prompt,
            unsafe_bypass=unsafe_bypass,
            last_message_path=last_message_path,
            model=model,
            effort=effort,
            access=access,
            env=env,
        )
        return [argv[0], str(self.script), *argv[1:]]


class FakeHarnessIntegrationTests(unittest.TestCase):
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
                runner.run_control.RunControlListener,
                "for_run",
                side_effect=fake_for_run,
            ),
        ):
            lock_info = state.acquire_lock(plan_dir, state_dir)
            try:
                exit_code, payload = runner._run_locked(
                    args=args,
                    cwd=run_cwd,
                    timeout=None,
                    max_sessions=None,
                    commit_mode=commit_mode,
                    lock_info=lock_info,
                    run_id="test-run",
                )
            finally:
                state.release_lock(plan_dir, state_dir)
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
            runner.harnesses,
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
        if commit_mode == runner.COMMIT_MODE_PER_SESSION:
            command.append("--commit-per-session")
        elif commit_mode == runner.COMMIT_MODE_END:
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
                runner.run_control.RunControlListener,
                "for_run",
                side_effect=fake_for_run,
            ),
        ):
            lock_info = state.acquire_lock(plan_dir, state_dir)
            try:
                return runner._run_locked(
                    args=args,
                    cwd=run_cwd,
                    timeout=None,
                    max_sessions=None,
                    commit_mode=None,
                    lock_info=lock_info,
                    run_id=run_id,
                )
            finally:
                state.release_lock(plan_dir, state_dir)

    def idle_started_at(self, turn: dict[str, Any]) -> datetime:
        started_at = runner._parse_datetime(turn["started_at"])
        assert isinstance(started_at, datetime)
        return started_at - timedelta(seconds=turn["idle_seconds_since_previous_turn"])

    def last_event_created_at(self, events_path: Path, name: str) -> datetime:
        created_at = [
            runner._parse_datetime(record["created_at"])
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

    def test_loop_lifecycle_notifications_run_after_state_writes(self) -> None:
        listener = FakeRunControlListener()
        observed: list[tuple[str, object]] = []

        def read_state_payload(payload: dict[str, Any]) -> dict[str, Any]:
            return json.loads(
                Path(str(payload["state_path"])).read_text(encoding="utf-8")
            )

        def notify_started(
            payload: dict[str, Any],
            persistence: object = None,
        ) -> None:
            state_payload = read_state_payload(payload)
            observed.append(("started", state_payload["active_turn"]))
            self.assertEqual(payload["run_id"], state_payload["run_id"])
            self.assertEqual("codex", state_payload["harness"])
            self.assertIsNone(state_payload["active_turn"])

        def notify_turn_started(
            payload: dict[str, Any],
            active_turn: dict[str, Any],
            persistence: object = None,
        ) -> None:
            state_payload = read_state_payload(payload)
            state_turn = state_payload["active_turn"]
            self.assertIsInstance(state_turn, dict)
            assert isinstance(state_turn, dict)
            observed.append(("turn_started", state_turn["harness"]))
            self.assertEqual(active_turn["turn_index"], state_turn["turn_index"])
            self.assertEqual("codex", state_turn["harness"])

        def notify_finished(
            payload: dict[str, Any],
            persistence: object = None,
        ) -> None:
            state_payload = read_state_payload(payload)
            observed.append(("finished", state_payload["run_finished_at"]))
            self.assertEqual(
                payload["run_finished_at"], state_payload["run_finished_at"]
            )
            self.assertIsNone(state_payload["active_turn"])

        with (
            mock.patch.object(
                runner,
                "_notify_agent_mux_loop_started",
                side_effect=notify_started,
            ),
            mock.patch.object(
                turn_execution,
                "notify_turn_started",
                side_effect=notify_turn_started,
            ),
            mock.patch.object(
                runner,
                "_notify_agent_mux_loop_finished",
                side_effect=notify_finished,
            ),
        ):
            exit_code, payload, _plan_dir, _state_dir, _fake_codex = (
                self.run_locked_with_listener(
                    mode="success",
                    statuses=("pending",),
                    listener=listener,
                )
            )

        self.assertEqual(0, exit_code)
        self.assertTrue(payload["ok"])
        self.assertEqual(
            ["started", "turn_started", "finished"],
            [name for name, _value in observed],
        )

    def test_copilot_run_from_other_cwd_notifies_agent_mux_with_project_payloads(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            config_home = tmp_dir / "config"
            config_path = config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
            config_path.parent.mkdir(parents=True, exist_ok=True)
            fake_codex = install_fake_codex(tmp_dir)
            fake_copilot = install_fake_harness(tmp_dir, "copilot")
            _fake_amx, amx_log = install_fake_amx(tmp_dir)
            caller_dir = tmp_dir / "caller"
            project_dir = tmp_dir / "project"
            caller_dir.mkdir()
            init_git_repo(project_dir)
            write_plan(plan_dir, ("pending",))
            config_path.write_text(
                json.dumps(
                    {
                        "codex": {"executable": str(fake_codex)},
                        "copilot": {"executable": str(fake_copilot)},
                    }
                ),
                encoding="utf-8",
            )

            env = os.environ.copy()
            env.update(
                {
                    "XDG_CONFIG_HOME": str(config_home),
                    "FAKE_CODEX_MODE": "success",
                    "FAKE_CODEX_SLEEP": "5",
                    "FAKE_AMX_LOG": str(amx_log),
                    # The suite environment forces notification off; this
                    # test exercises real delivery against the fake amx.
                    runner.AGENT_MUX_NOTIFY_ENV_VAR: "auto",
                    # Under parallel-shard load the fake amx interpreter can
                    # take longer than the 1s production timeout to start,
                    # which silently drops the notification record.
                    runner.AGENT_MUX_NOTIFY_TIMEOUT_ENV_VAR: "30",
                    "PATH": str(tmp_dir) + os.pathsep + os.environ.get("PATH", ""),
                    "PYTHONPATH": str(REPO_ROOT)
                    + os.pathsep
                    + env.get("PYTHONPATH", ""),
                }
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "run",
                    str(plan_dir),
                    "--cwd",
                    str(project_dir),
                    "--harness",
                    "copilot",
                    "--state-dir",
                    str(state_dir),
                    "--json",
                    "--allow-not-loop-ready",
                ],
                cwd=caller_dir,
                env=env,
                text=True,
                capture_output=True,
                timeout=LOOP_RUN_TIMEOUT_SECONDS,
                check=False,
            )
            payload = json.loads(completed.stdout)

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertTrue(payload["ok"])
            self.assertEqual("copilot", payload["harness"])
            expected_cwd = str(plans.canonicalize_path(project_dir))
            self.assertEqual(expected_cwd, payload["cwd"])

            amx_records = read_jsonl(amx_log)
            self.assertEqual(
                [
                    runner.AGENT_MUX_START_EVENT,
                    runner.AGENT_MUX_TURN_START_EVENT,
                    runner.AGENT_MUX_TURN_FINISH_EVENT,
                    runner.AGENT_MUX_FINISH_EVENT,
                ],
                [record["argv"][-1] for record in amx_records],
            )
            self.assertEqual(
                {str(plans.canonicalize_path(caller_dir))},
                {record["cwd"] for record in amx_records},
            )

            hook_payloads = [json.loads(str(record["stdin"])) for record in amx_records]
            self.assertEqual(
                [
                    runner.AGENT_MUX_START_REASON,
                    runner.AGENT_MUX_TURN_START_REASON,
                    runner.AGENT_MUX_TURN_FINISH_REASON,
                    runner.AGENT_MUX_FINISH_REASON,
                ],
                [hook_payload["reason"] for hook_payload in hook_payloads],
            )
            self.assertEqual(
                ["between", "turn", "between", "finished"],
                [hook_payload["phase"] for hook_payload in hook_payloads],
            )
            # A nested ASL subprocess allocates and persists the per-run
            # sequence: strings, starting at 1, monotonic across the run.
            self.assertEqual(
                ["1", "2", "3", "4"],
                [hook_payload["source_sequence"] for hook_payload in hook_payloads],
            )
            for hook_payload in hook_payloads:
                self.assertEqual("agent_session_loop", hook_payload["source"])
                self.assertEqual("copilot", hook_payload["harness"])
                self.assertEqual(expected_cwd, hook_payload["cwd"])
                self.assertEqual(payload["plan_dir"], hook_payload["plan_dir"])
                self.assertEqual(payload["state_path"], hook_payload["state_path"])
                self.assertEqual(payload["run_id"], hook_payload["run_id"])
                self.assertEqual(
                    f"{payload['run_id']}:{hook_payload['source_sequence']}",
                    hook_payload["source_event_id"],
                )
                self.assertTrue(hook_payload["emitted_time"].isdigit())
                self.assertTrue(Path(str(hook_payload["state_path"])).exists())

            turn_payload = hook_payloads[1]
            self.assertEqual(1, turn_payload["turn_index"])
            self.assertEqual(1, turn_payload["session"])
            self.assertEqual("1", turn_payload["session_id"])
            self.assertEqual(
                str(payload["turns"][0]["artifact_dir"]),
                turn_payload["artifact_dir"],
            )
            final_state = json.loads(
                Path(payload["state_path"]).read_text(encoding="utf-8")
            )
            self.assertIsNone(final_state["active_turn"])
            self.assertEqual(4, final_state["agent_mux_sequence"])

    def test_harness_switch_updates_agent_mux_turn_notification_harness(self) -> None:
        hook_payloads: list[dict[str, Any]] = []
        finish_payloads: list[dict[str, Any]] = []

        def notify_turn_started(
            payload: dict[str, Any],
            active_turn: dict[str, Any],
            persistence: object = None,
        ) -> None:
            hook_payloads.append(
                runner._agent_mux_loop_payload(
                    payload,
                    reason=runner.AGENT_MUX_TURN_START_REASON,
                    active_turn=active_turn,
                )
            )

        def notify_finished(
            payload: dict[str, Any],
            persistence: object = None,
        ) -> None:
            finish_payloads.append(
                runner._agent_mux_loop_payload(
                    payload,
                    reason=runner.AGENT_MUX_FINISH_REASON,
                )
            )

        with (
            mock.patch.object(
                turn_execution,
                "notify_turn_started",
                side_effect=notify_turn_started,
            ),
            mock.patch.object(
                runner,
                "_notify_agent_mux_loop_finished",
                side_effect=notify_finished,
            ),
        ):
            exit_code, payload, _calls, _plan_dir, _state_dir = (
                self.run_with_mocked_harness_switch()
            )

        self.assertEqual(0, exit_code)
        self.assertEqual(
            ["codex", "claude"],
            [turn["harness"] for turn in payload["turns"]],
        )
        self.assertEqual(
            ["codex", "claude"],
            [hook_payload["harness"] for hook_payload in hook_payloads],
        )
        self.assertEqual(
            [1, 2],
            [hook_payload["turn_index"] for hook_payload in hook_payloads],
        )
        self.assertEqual(
            [1, 2],
            [hook_payload["session"] for hook_payload in hook_payloads],
        )
        self.assertEqual(1, len(finish_payloads))
        self.assertEqual("claude", finish_payloads[0]["harness"])

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
        if commit_mode == runner.COMMIT_MODE_PER_SESSION:
            command.append("--commit-per-session")
        elif commit_mode == runner.COMMIT_MODE_END:
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

    def start_run_pty(
        self,
        *,
        plan_dir: Path,
        state_dir: Path,
        config_home: Path,
        mode: str,
        cwd: Path | None = None,
        commit_mode: str | None = None,
        note: str | None = None,
        extra_env: dict[str, str] | None = None,
        fake_step_sleep: str = "0.8",
    ) -> tuple[subprocess.Popen[bytes], int]:
        master_fd, slave_fd = pty.openpty()
        env_extra = {"FAKE_CODEX_STEP_SLEEP": fake_step_sleep}
        if extra_env is not None:
            env_extra.update(extra_env)
        env = self.loop_env(config_home=config_home, mode=mode, extra=env_extra)
        try:
            process = subprocess.Popen(
                self.run_command(
                    plan_dir=plan_dir,
                    state_dir=state_dir,
                    cwd=cwd,
                    commit_mode=commit_mode,
                    note=note,
                ),
                cwd=REPO_ROOT,
                env=env,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
            )
        finally:
            os.close(slave_fd)
        os.set_blocking(master_fd, False)

        def cleanup_process() -> None:
            if process.poll() is None:
                process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            with suppress(OSError):
                os.close(master_fd)

        self.addCleanup(cleanup_process)
        return process, master_fd

    def read_run_pty_available(self, master_fd: int, output: str) -> str:
        while True:
            readable, _, _ = select.select([master_fd], [], [], 0)
            if not readable:
                return output
            try:
                chunk = os.read(master_fd, 4096)
            except BlockingIOError:
                return output
            except OSError as error:
                if error.errno == errno.EIO:
                    return output
                raise
            if not chunk:
                return output
            output += chunk.decode("utf-8", errors="replace")

    def read_run_pty_until(
        self,
        master_fd: int,
        output: str,
        predicate,
        *,
        process: subprocess.Popen[bytes],
        timeout: float = 30.0,
    ) -> str:
        # A readiness wait, not a latency assertion: the loop returns the moment
        # the predicate holds and breaks early once the process exits, so a
        # generous budget costs nothing on the happy path. The old 5s default
        # was tight enough that the full suite's own parallel load, not any
        # regression, could outlast a run's startup.
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            output = self.read_run_pty_available(master_fd, output)
            if predicate(output):
                return output
            if process.poll() is not None:
                break
            remaining = max(0.0, deadline - time.monotonic())
            select.select([master_fd], [], [], min(0.05, remaining))

        output = self.read_run_pty_available(master_fd, output)
        if predicate(output):
            return output
        self.fail(f"condition was not met before timeout. PTY output:\n{output}")

    def finish_run_pty(
        self,
        *,
        process: subprocess.Popen[bytes],
        master_fd: int,
        output: str,
        timeout: float = 30.0,
    ) -> str:
        output = self.read_run_pty_until(
            master_fd,
            output,
            lambda _text: process.poll() is not None,
            process=process,
            timeout=timeout,
        )
        process.wait(timeout=5)
        return self.read_run_pty_available(master_fd, output)

    def read_run_payload(
        self,
        *,
        plan_dir: Path,
        state_dir: Path,
    ) -> dict[str, Any]:
        state_path = state.plan_state_dir(plan_dir, state_dir) / events.STATE_FILE_NAME
        return json.loads(state_path.read_text(encoding="utf-8"))

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
            runner._compact_status_lines(
                payload=payload,
                control=control,
                snapshot=plans.inspect_plan(plan_dir),
                now=runner._parse_datetime("2026-06-11T12:00:20+00:00"),
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
            runner._compact_status_lines(
                payload=payload,
                control=control,
                snapshot=plans.inspect_plan(plan_dir),
                now=runner._parse_datetime("2026-06-11T12:01:30+00:00"),
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
            runner._compact_status_lines(
                payload=payload,
                control=control,
                snapshot=plans.inspect_plan(plan_dir),
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
            runner._compact_status_lines(
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
            runner._compact_status_lines(
                payload=payload,
                control=control,
                snapshot=plans.inspect_plan(plan_dir),
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
            runner._compact_status_lines(
                payload=payload,
                control=control,
                snapshot=plans.inspect_plan(plan_dir),
                now=runner._parse_datetime("2026-06-11T12:00:20+00:00"),
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
            runner._compact_status_lines(
                payload=payload,
                control=control,
                snapshot=plans.inspect_plan(plan_dir),
                now=runner._parse_datetime("2026-06-11T12:00:01+00:00"),
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
                str(plans.canonicalize_path(plan_dir / "manifest.md")),
                str(plans.canonicalize_path(plan_dir / "context.md")),
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

    def test_text_run_pty_p_stops_after_current_session(self) -> None:
        plan_dir, state_dir, config_home, _fake_codex = self.setup_loop(
            statuses=("pending", "pending")
        )
        process, master_fd = self.start_run_pty(
            plan_dir=plan_dir,
            state_dir=state_dir,
            config_home=config_home,
            mode="streaming-success",
        )

        output = self.read_run_pty_until(
            master_fd,
            "",
            lambda text: "session 1/2 started" in text,
            process=process,
        )
        os.write(master_fd, b"p")
        output = self.read_run_pty_until(
            master_fd,
            output,
            lambda text: "control: stop-after-current requested" in text,
            process=process,
        )
        output = self.finish_run_pty(
            process=process,
            master_fd=master_fd,
            output=output,
        )

        self.assertEqual(0, process.returncode, output)
        self.assertIn("Run complete: 1 session(s) completed", output)
        self.assertIn("Next pending: session 2", output)
        payload = self.read_run_payload(plan_dir=plan_dir, state_dir=state_dir)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])
        self.assertEqual("stopped", payload["reason"])
        self.assertEqual([1], payload["completed_sessions"])
        self.assertEqual(2, payload["final_snapshot"]["next_pending"])
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
        self.assertIn(
            "- **Status**: complete",
            (plan_dir / "session-1.md").read_text(encoding="utf-8"),
        )
        self.assertIn(
            "- **Status**: pending",
            (plan_dir / "session-2.md").read_text(encoding="utf-8"),
        )

        control_events = [
            record["payload"]
            for record in read_jsonl(Path(payload["events_path"]))
            if record["name"] == events.RUN_CONTROL_EVENT
        ]
        self.assertEqual(
            [
                "stop-after-current-requested",
                "stop-after-current-consumed",
            ],
            [event["action"] for event in control_events],
        )

    def test_text_run_pty_second_p_cancels_stop_after_current(self) -> None:
        plan_dir, state_dir, config_home, _fake_codex = self.setup_loop(
            statuses=("pending", "pending")
        )
        process, master_fd = self.start_run_pty(
            plan_dir=plan_dir,
            state_dir=state_dir,
            config_home=config_home,
            mode="streaming-success",
        )

        output = self.read_run_pty_until(
            master_fd,
            "",
            lambda text: "session 1/2 started" in text,
            process=process,
        )
        os.write(master_fd, b"p")
        output = self.read_run_pty_until(
            master_fd,
            output,
            lambda text: "control: stop-after-current requested" in text,
            process=process,
        )
        os.write(master_fd, b"p")
        output = self.read_run_pty_until(
            master_fd,
            output,
            lambda text: "control: stop-after-current canceled" in text,
            process=process,
        )
        output = self.finish_run_pty(
            process=process,
            master_fd=master_fd,
            output=output,
        )

        self.assertEqual(0, process.returncode, output)
        payload = self.read_run_payload(plan_dir=plan_dir, state_dir=state_dir)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])
        self.assertEqual("all-complete", payload["reason"])
        self.assertEqual([1, 2], payload["completed_sessions"])
        self.assertIsNone(payload["final_snapshot"]["next_pending"])
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
                "last_action": "stop-after-current-canceled",
            },
            payload["control"],
        )
        control_events = [
            record["payload"]
            for record in read_jsonl(Path(payload["events_path"]))
            if record["name"] == events.RUN_CONTROL_EVENT
        ]
        self.assertEqual(
            [
                "stop-after-current-requested",
                "stop-after-current-canceled",
            ],
            [event["action"] for event in control_events],
        )

    def test_text_run_pty_ctrl_c_pauses_with_note(self) -> None:
        plan_dir, state_dir, config_home, _fake_codex = self.setup_loop(
            statuses=("pending",)
        )
        note = "operator reference ABC-123"
        gate_dir = state_dir / "ctrl-c-stream-gates"
        # The turn must still be in flight when SIGINT lands: hold the harness at
        # the stream gate instead of racing a step sleep, which a loaded machine
        # can outlast.
        process, master_fd = self.start_run_pty(
            plan_dir=plan_dir,
            state_dir=state_dir,
            config_home=config_home,
            mode="streaming-success",
            note=note,
            fake_step_sleep="0",
            extra_env={
                "FAKE_CODEX_STREAM_GATE_DIR": str(gate_dir),
                "FAKE_CODEX_STREAM_GATE_TIMEOUT": "20",
            },
        )

        output = self.read_run_pty_until(
            master_fd,
            "",
            lambda text: "session 1/1 started" in text,
            process=process,
        )

        def harness_started():
            if not (gate_dir / "session-1.ready").exists():
                return None
            payload = self.read_run_payload(plan_dir=plan_dir, state_dir=state_dir)
            active_turn = payload.get("active_turn")
            if not isinstance(active_turn, dict):
                return None
            stdout_path = Path(active_turn["stdout_path"])
            if not stdout_path.exists():
                return None
            if "stdout before completion" in stdout_path.read_text(encoding="utf-8"):
                return active_turn
            return None

        wait_until(harness_started, timeout=30)
        process.send_signal(signal.SIGINT)
        output = self.finish_run_pty(
            process=process,
            master_fd=master_fd,
            output=output,
        )

        self.assertEqual(runner.INTERRUPTED_EXIT, process.returncode, output)
        self.assertIn(f"Note: {note}", output)
        self.assertIn("Run paused: interrupted", output)
        payload = self.read_run_payload(plan_dir=plan_dir, state_dir=state_dir)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["paused"])
        self.assertEqual("interrupted", payload["pause_reason"])
        self.assertEqual(note, payload["note"])
        self.assertIsNone(payload["active_turn"])
        self.assertEqual([], payload["completed_sessions"])
        self.assertEqual(1, len(payload["turns"]))
        self.assertTrue(payload["turns"][0]["interrupted"])
        self.assertEqual("interrupted", payload["turns"][0]["status_result"])
        self.assertFalse(state.lock_path_for_plan(plan_dir, state_dir).exists())

    def test_text_run_pty_h_prints_one_help_line(self) -> None:
        plan_dir, state_dir, config_home, _fake_codex = self.setup_loop(
            statuses=("pending",)
        )
        process, master_fd = self.start_run_pty(
            plan_dir=plan_dir,
            state_dir=state_dir,
            config_home=config_home,
            mode="streaming-success",
        )

        output = self.read_run_pty_until(
            master_fd,
            "",
            lambda text: "session 1/1 started" in text,
            process=process,
        )
        os.write(master_fd, b"h")
        output = self.read_run_pty_until(
            master_fd,
            output,
            lambda text: "controls: p toggle stop-after-current" in text,
            process=process,
        )
        output = self.finish_run_pty(
            process=process,
            master_fd=master_fd,
            output=output,
        )

        self.assertEqual(0, process.returncode, output)
        self.assertRegex(
            output,
            r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] "
            r"controls: p toggle stop-after-current "
            r"\(currently off\); s show status; "
            r"t toggle live tail \(currently off\); "
            r"1 codex, 2 claude, 3 copilot select next harness "
            r"\(currently unchanged\); h show help",
        )
        self.assertEqual(1, output.count("controls: p toggle stop-after-current"))
        payload = self.read_run_payload(plan_dir=plan_dir, state_dir=state_dir)
        self.assertEqual("all-complete", payload["reason"])
        control_events = [
            record
            for record in read_jsonl(Path(payload["events_path"]))
            if record["name"] == events.RUN_CONTROL_EVENT
        ]
        self.assertEqual([], control_events)

    def test_text_run_pty_s_and_number_keys_select_future_harness(self) -> None:
        plan_dir, state_dir, config_home, fake_codex = self.setup_loop(
            statuses=("pending", "pending")
        )
        config_path = config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        config_path.write_text(
            json.dumps(
                {
                    "codex": {"executable": str(fake_codex)},
                    "claude": {"executable": str(fake_codex)},
                    "copilot": {"executable": str(fake_codex)},
                }
            ),
            encoding="utf-8",
        )
        process, master_fd = self.start_run_pty(
            plan_dir=plan_dir,
            state_dir=state_dir,
            config_home=config_home,
            mode="streaming-success",
            note="operator reference ABC-123",
            fake_step_sleep="2.0",
        )

        output = self.read_run_pty_until(
            master_fd,
            "",
            lambda text: "session 1/2 started" in text,
            process=process,
        )
        os.write(master_fd, b"s")
        output = self.read_run_pty_until(
            master_fd,
            output,
            lambda text: (
                "status: active session 1/2 turn 1" in text
                and "total " in text
                and "status note: operator reference ABC-123" in text
                and "status: harness active codex; future unchanged" in text
                and "status: controls stop-after-current off; live tail off" in text
            ),
            process=process,
        )

        os.write(master_fd, b"1")
        output = self.read_run_pty_until(
            master_fd,
            output,
            lambda text: "control: next harness already codex" in text,
            process=process,
        )
        os.write(master_fd, b"2")
        output = self.read_run_pty_until(
            master_fd,
            output,
            lambda text: "control: next harness selected: claude" in text,
            process=process,
        )
        os.write(master_fd, b"3")
        output = self.read_run_pty_until(
            master_fd,
            output,
            lambda text: "control: next harness selected: copilot" in text,
            process=process,
        )
        os.write(master_fd, b"s")
        output = self.read_run_pty_until(
            master_fd,
            output,
            lambda text: "status: harness active codex; future copilot" in text,
            process=process,
        )
        output = self.read_run_pty_until(
            master_fd,
            output,
            lambda text: "session 2/2 started" in text,
            process=process,
        )
        output = self.finish_run_pty(
            process=process,
            master_fd=master_fd,
            output=output,
        )

        self.assertEqual(0, process.returncode, output)
        payload = self.read_run_payload(plan_dir=plan_dir, state_dir=state_dir)
        self.assertEqual("all-complete", payload["reason"])
        self.assertEqual(
            {
                "stop_after_current": False,
                "abort_turn_requested": False,
                "abort_grace_seconds": None,
                "abort_reason": None,
                "abort_deadline": None,
                "tail_enabled": False,
                "next_harness": "copilot",
                "target_harness": None,
                "changed_fields": [],
                "implementation_overrides": {},
                "guards": [],
                "consumed": False,
                "last_action": "harness-selected",
            },
            payload["control"],
        )
        turns = payload["turns"]
        self.assertEqual(["codex", "copilot"], [turn["harness"] for turn in turns])
        # Autonomous turns default to packet mode, so the prompt is the
        # harness-neutral task packet, not the `session-run` skill invocation.
        codex_prompt = turns[0]["argv"][-1]
        self.assertTrue(codex_prompt.startswith(task_packet.TASK_PACKET_HEADING_PREFIX))
        self.assertNotIn("session-run", codex_prompt)
        self.assertIn("-p", turns[1]["argv"])
        copilot_prompt = turns[1]["argv"][turns[1]["argv"].index("-p") + 1]
        self.assertTrue(
            copilot_prompt.startswith(task_packet.TASK_PACKET_HEADING_PREFIX)
        )
        self.assertNotIn("session-run", copilot_prompt)

        control_events = [
            record["payload"]
            for record in read_jsonl(Path(payload["events_path"]))
            if record["name"] == events.RUN_CONTROL_EVENT
        ]
        self.assertEqual(
            ["harness-selected", "harness-selected"],
            [event["action"] for event in control_events],
        )
        self.assertEqual(
            ["claude", "copilot"],
            [event["selected_harness"] for event in control_events],
        )
        self.assertEqual(
            ["claude", "copilot"],
            [event["next_harness"] for event in control_events],
        )

    def test_text_run_pty_t_toggles_inline_tail_for_new_log_lines(self) -> None:
        plan_dir, state_dir, config_home, _fake_codex = self.setup_loop(
            statuses=("pending", "pending")
        )
        state_path = state.plan_state_dir(plan_dir, state_dir) / events.STATE_FILE_NAME
        gate_dir = state_dir / "stream-gates"

        def ready_path(session_id: str) -> Path:
            return gate_dir / f"session-{session_id}.ready"

        def continue_path(session_id: str) -> Path:
            return gate_dir / f"session-{session_id}.continue"

        def release_session(session_id: str) -> None:
            gate_dir.mkdir(parents=True, exist_ok=True)
            continue_path(session_id).write_text("continue\n", encoding="utf-8")

        process, master_fd = self.start_run_pty(
            plan_dir=plan_dir,
            state_dir=state_dir,
            config_home=config_home,
            mode="streaming-success",
            fake_step_sleep="0",
            extra_env={
                "FAKE_CODEX_STREAM_GATE_DIR": str(gate_dir),
                "FAKE_CODEX_STREAM_GATE_TIMEOUT": "20",
            },
        )

        output = self.read_run_pty_until(
            master_fd,
            "",
            lambda text: "session 1/2 started" in text,
            process=process,
        )

        def first_turn_logs_ready():
            if not ready_path("1").exists():
                return None
            if not state_path.exists():
                return None
            payload = json.loads(state_path.read_text(encoding="utf-8"))
            active_turn = payload.get("active_turn")
            if not isinstance(active_turn, dict):
                return None
            stdout_path = Path(active_turn["stdout_path"])
            stderr_path = Path(active_turn["stderr_path"])
            if not stdout_path.exists() or not stderr_path.exists():
                return None
            if "session 1 stdout before completion" in stdout_path.read_text(
                encoding="utf-8"
            ) and "session 1 stderr before completion" in stderr_path.read_text(
                encoding="utf-8"
            ):
                return active_turn
            return None

        wait_until(first_turn_logs_ready, timeout=30)
        os.write(master_fd, b"t")
        output = self.read_run_pty_until(
            master_fd,
            output,
            lambda text: "control: live tail enabled" in text,
            process=process,
        )
        output = self.read_run_pty_until(
            master_fd,
            output,
            lambda text: (
                "stdout session=1 turn=1 | session 1 stdout before completion" in text
            ),
            process=process,
        )
        output = self.read_run_pty_until(
            master_fd,
            output,
            lambda text: (
                "stderr session=1 turn=1 | session 1 stderr before completion" in text
            ),
            process=process,
        )
        release_session("1")
        output = self.read_run_pty_until(
            master_fd,
            output,
            lambda text: (
                "stdout session=1 turn=1 | session 1 stdout after completion" in text
            ),
            process=process,
        )

        output = self.read_run_pty_until(
            master_fd,
            output,
            lambda text: (
                "stdout session=2 turn=2 | session 2 stdout before completion" in text
            ),
            process=process,
        )
        os.write(master_fd, b"t")
        output = self.read_run_pty_until(
            master_fd,
            output,
            lambda text: "control: live tail disabled" in text,
            process=process,
        )
        release_session("2")
        output = self.finish_run_pty(
            process=process,
            master_fd=master_fd,
            output=output,
        )

        self.assertEqual(0, process.returncode, output)
        self.assertIn(
            "stdout session=2 turn=2 | session 2 stdout before completion",
            output,
        )
        self.assertNotIn(
            "stdout session=2 turn=2 | session 2 stdout after completion",
            output,
        )
        self.assertNotIn(
            "stderr session=2 turn=2 | session 2 stderr after completion",
            output,
        )
        payload = self.read_run_payload(plan_dir=plan_dir, state_dir=state_dir)
        self.assertEqual("all-complete", payload["reason"])
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
                "last_action": "tail-disabled",
            },
            payload["control"],
        )
        control_events = [
            record["payload"]
            for record in read_jsonl(Path(payload["events_path"]))
            if record["name"] == events.RUN_CONTROL_EVENT
        ]
        self.assertEqual(
            ["tail-enabled", "tail-disabled"],
            [event["action"] for event in control_events],
        )
        self.assertEqual(
            [True, False],
            [event["tail_enabled"] for event in control_events],
        )

    def test_text_run_pty_commit_per_session_finishes_before_stop_consumed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            repo_dir = tmp_dir / "repo"
            init_git_repo(repo_dir)
            commit_log, commit_env = self.setup_commit_harness(tmp_dir)
            plan_dir, state_dir, config_home, _fake_codex = self.setup_loop(
                statuses=("pending", "pending")
            )
            process, master_fd = self.start_run_pty(
                plan_dir=plan_dir,
                state_dir=state_dir,
                config_home=config_home,
                mode="streaming-success",
                cwd=repo_dir,
                commit_mode=runner.COMMIT_MODE_PER_SESSION,
                extra_env=commit_env,
            )

            output = self.read_run_pty_until(
                master_fd,
                "",
                lambda text: "session 1/2 started" in text,
                process=process,
            )
            os.write(master_fd, b"p")
            output = self.read_run_pty_until(
                master_fd,
                output,
                lambda text: "control: stop-after-current requested" in text,
                process=process,
            )
            output = self.finish_run_pty(
                process=process,
                master_fd=master_fd,
                output=output,
            )
            payload = self.read_run_payload(plan_dir=plan_dir, state_dir=state_dir)
            commit_records = read_jsonl(commit_log)

        self.assertEqual(0, process.returncode, output)
        self.assertIn("commit 1 finished (session 1, status success,", output)
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
            runner.harnesses,
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
                    runner.run_control.RunControlListener,
                    "for_run",
                    side_effect=fake_for_run,
                ),
            ):
                lock_info = state.acquire_lock(plan_dir, state_dir)
                try:
                    exit_code, payload = runner._run_locked(
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

    def test_external_queue_select_future_harness_switches_next_turn(self) -> None:
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
            complete_next_pending(Path(kwargs["plan_dir"]))
            if len(calls) == 1:
                control_queue.append_command(
                    plan_dir_box[0],
                    control_queue.COMMAND_SELECT_FUTURE_HARNESS,
                    state_dir=state_dir_box[0],
                    harness="claude",
                    command_id="ext-sel-1",
                )
            return harnesses.HarnessResult(
                name=name,
                argv=[name, str(kwargs["prompt"])],
                returncode=0,
                stdout=f"{name} completed\n",
                stderr="",
            )

        plan_dir, state_dir, config_home, _fake_codex = self.setup_loop(
            statuses=("pending", "pending"),
            harness_config_overrides={"claude": sys.executable},
        )
        plan_dir_box.append(plan_dir)
        state_dir_box.append(state_dir)

        with mock.patch.object(
            runner.harnesses,
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
                    runner.run_control.RunControlListener,
                    "for_run",
                    side_effect=fake_for_run,
                ),
            ):
                lock_info = state.acquire_lock(plan_dir, state_dir)
                try:
                    exit_code, payload = runner._run_locked(
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
        self.assertEqual("all-complete", payload["reason"])
        self.assertEqual(["codex", "claude"], [call["harness"] for call in calls])
        self.assertEqual("claude", payload["control"]["next_harness"])

        control_events = [
            record["payload"]
            for record in read_jsonl(Path(payload["events_path"]))
            if record["name"] == events.RUN_CONTROL_EVENT
        ]
        actions = [event["action"] for event in control_events]
        self.assertIn("harness-selected", actions)

    def test_external_queue_applies_while_harness_subprocess_is_active(self) -> None:
        """A queue command written mid-turn is applied by periodic polling."""

        listener = FakeRunControlListener(current_harness="codex")
        calls: list[dict[str, Any]] = []
        plan_dir_box: list[Path] = []
        state_dir_box: list[Path] = []
        stop_state_during_call: list[bool] = []

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
            # Queue arrives while the harness is "running". Observe the
            # listener state before completing the session.
            control_queue.append_command(
                plan_dir_box[0],
                control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT,
                state_dir=state_dir_box[0],
                command_id=f"midflight-{len(calls)}",
            )
            wait_until(lambda: listener.state.stop_after_current)
            stop_state_during_call.append(listener.state.stop_after_current)
            complete_next_pending(plan_dir)
            return harnesses.HarnessResult(
                name=name,
                argv=[name, str(kwargs["prompt"])],
                returncode=0,
                stdout=f"{name} completed\n",
                stderr="",
            )

        plan_dir, state_dir, config_home, _fake_codex = self.setup_loop(
            statuses=("pending", "pending"),
        )
        plan_dir_box.append(plan_dir)
        state_dir_box.append(state_dir)

        with mock.patch.object(
            runner.harnesses,
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
                    runner.run_control.RunControlListener,
                    "for_run",
                    side_effect=fake_for_run,
                ),
                mock.patch.object(
                    runner,
                    "LIVE_CONTROL_POLL_INTERVAL_SECONDS",
                    0.01,
                ),
            ):
                lock_info = state.acquire_lock(plan_dir, state_dir)
                try:
                    exit_code, _payload = runner._run_locked(
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

        self.assertEqual(0, exit_code)
        self.assertTrue(stop_state_during_call[0])
        # Exactly one harness call happened because the queued stop was
        # consumed after the current session and ended the run.
        self.assertEqual(1, len(calls))

    def test_harness_selection_applies_to_next_session_and_recomputes_native_model(
        self,
    ) -> None:
        home_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(home_tmp.cleanup)
        home = Path(home_tmp.name)
        codex_config = home / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True)
        codex_config.write_text('model = "gpt-5-codex"\n', encoding="utf-8")

        exit_code, payload, calls, _plan_dir, _state_dir = (
            self.run_with_mocked_harness_switch(extra_env={"HOME": str(home)})
        )

        self.assertEqual(0, exit_code)
        self.assertEqual("all-complete", payload["reason"])
        self.assertEqual(["codex", "claude"], [call["harness"] for call in calls])
        # Packet mode is the default: both harnesses receive the harness-neutral
        # task packet rather than a `session-run` skill invocation.
        self.assertTrue(
            str(calls[0]["prompt"]).startswith(task_packet.TASK_PACKET_HEADING_PREFIX)
        )
        self.assertTrue(
            str(calls[1]["prompt"]).startswith(task_packet.TASK_PACKET_HEADING_PREFIX)
        )
        self.assertNotIn("session-run", str(calls[0]["prompt"]))
        self.assertNotIn("session-run", str(calls[1]["prompt"]))
        self.assertIsNone(calls[0]["model"])
        self.assertIsNone(calls[1]["model"])

        turns = payload["turns"]
        self.assertEqual(["codex", "claude"], [turn["harness"] for turn in turns])
        self.assertEqual("gpt-5-codex", turns[0]["model"])
        self.assertEqual(config.MODEL_SOURCE_CODEX_CONFIG, turns[0]["model_source"])
        self.assertEqual(config.NATIVE_MODEL_DEFAULT, turns[1]["model"])
        self.assertEqual(config.MODEL_SOURCE_UNRESOLVED, turns[1]["model_source"])
        self.assertEqual(
            {
                "stop_after_current": False,
                "abort_turn_requested": False,
                "abort_grace_seconds": None,
                "abort_reason": None,
                "abort_deadline": None,
                "tail_enabled": False,
                "next_harness": "claude",
                "target_harness": None,
                "changed_fields": [],
                "implementation_overrides": {},
                "guards": [],
                "consumed": False,
                "last_action": "harness-selected",
            },
            payload["control"],
        )

        control_events = [
            record["payload"]
            for record in read_jsonl(Path(payload["events_path"]))
            if record["name"] == events.RUN_CONTROL_EVENT
        ]
        self.assertEqual(
            ["harness-selected"],
            [event["action"] for event in control_events],
        )
        self.assertEqual("codex", control_events[0]["current_harness"])
        self.assertEqual("claude", control_events[0]["selected_harness"])
        self.assertEqual("claude", control_events[0]["next_harness"])
        self.assertEqual(1, control_events[0]["session"])
        self.assertEqual(1, control_events[0]["turn_index"])

    def test_harness_selection_does_not_carry_explicit_model_and_effort(self) -> None:
        exit_code, payload, calls, _plan_dir, _state_dir = (
            self.run_with_mocked_harness_switch(harness_model="gpt-5", effort="high")
        )

        self.assertEqual(0, exit_code)
        self.assertEqual(["codex", "claude"], [call["harness"] for call in calls])
        self.assertEqual(["gpt-5", None], [call["model"] for call in calls])
        self.assertEqual(["high", "high"], [call["effort"] for call in calls])

        turns = payload["turns"]
        self.assertEqual(["codex", "claude"], [turn["harness"] for turn in turns])
        self.assertEqual(
            ["gpt-5", config.NATIVE_MODEL_DEFAULT],
            [turn["model"] for turn in turns],
        )
        self.assertEqual(
            [config.MODEL_SOURCE_FLAG, config.MODEL_SOURCE_UNRESOLVED],
            [turn["model_source"] for turn in turns],
        )
        self.assertEqual(["high", "high"], [turn["effort"] for turn in turns])
        self.assertEqual(
            [config.EFFORT_SOURCE_FLAG, config.EFFORT_SOURCE_FLAG],
            [turn["effort_source"] for turn in turns],
        )

    def test_model_control_changes_next_invocation_and_clear_restores_defaults(
        self,
    ) -> None:
        listener = FakeRunControlListener()
        calls: list[dict[str, Any]] = []
        paths: dict[str, Path] = {}

        def remember_paths(plan_dir: Path, state_dir: Path) -> None:
            paths["plan_dir"] = plan_dir
            paths["state_dir"] = state_dir

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

        def fake_run_harness(name: str, **kwargs: object) -> harnesses.HarnessResult:
            calls.append({"harness": name, **kwargs})
            plan_dir = Path(kwargs["plan_dir"])
            complete_next_pending(plan_dir)
            if len(calls) == 1:
                control_queue.append_command(
                    plan_dir,
                    control_queue.COMMAND_SELECT_FUTURE_HARNESS,
                    state_dir=paths["state_dir"],
                    harness="claude",
                    command_id="select-claude",
                )
                control_queue.append_command(
                    plan_dir,
                    control_queue.COMMAND_SET_IMPLEMENTATION_MODEL,
                    state_dir=paths["state_dir"],
                    harness_model="claude-opus-control",
                    effort="high",
                    command_id="set-claude",
                )
            elif len(calls) == 2:
                control_queue.append_command(
                    plan_dir,
                    control_queue.COMMAND_SET_IMPLEMENTATION_MODEL,
                    state_dir=paths["state_dir"],
                    clear_harness_model=True,
                    clear_effort=True,
                    command_id="clear-claude",
                )
            return harnesses.HarnessResult(
                name=name,
                argv=[name, str(kwargs["model"]), str(kwargs["effort"])],
                returncode=0,
                stdout=f"{name} completed\n",
                stderr="",
            )

        with mock.patch.object(
            runner.harnesses,
            "run_harness",
            side_effect=fake_run_harness,
        ):
            exit_code, payload, _plan_dir, _state_dir, _fake_codex = (
                self.run_locked_with_listener(
                    mode="success",
                    statuses=("pending", "pending", "pending"),
                    listener=listener,
                    harness_model="gpt-5-flag",
                    effort="medium",
                    harness_config_overrides={"claude": sys.executable},
                    before_run=remember_paths,
                )
            )

        self.assertEqual(0, exit_code)
        self.assertEqual(
            ["codex", "claude", "claude"],
            [call["harness"] for call in calls],
        )
        self.assertEqual(
            ["gpt-5-flag", "claude-opus-control", None],
            [call["model"] for call in calls],
        )
        self.assertEqual(
            ["medium", "high", "medium"],
            [call["effort"] for call in calls],
        )
        turns = payload["turns"]
        self.assertEqual(
            [
                config.MODEL_SOURCE_FLAG,
                config.MODEL_SOURCE_CONTROL,
                config.MODEL_SOURCE_UNRESOLVED,
            ],
            [turn["model_source"] for turn in turns],
        )
        self.assertEqual(
            [
                config.EFFORT_SOURCE_FLAG,
                config.EFFORT_SOURCE_CONTROL,
                config.EFFORT_SOURCE_FLAG,
            ],
            [turn["effort_source"] for turn in turns],
        )
        self.assertEqual({}, payload["control"]["implementation_overrides"])
        control_events = [
            record["payload"]
            for record in read_jsonl(Path(payload["events_path"]))
            if record["name"] == events.RUN_CONTROL_EVENT
        ]
        self.assertEqual(
            [
                events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
                events.RUN_CONTROL_ACTION_HARNESS_SELECTED,
                events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
                events.RUN_CONTROL_ACTION_IMPLEMENTATION_MODEL_UPDATED,
                events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
                events.RUN_CONTROL_ACTION_IMPLEMENTATION_MODEL_UPDATED,
            ],
            [event["action"] for event in control_events],
        )
        self.assertEqual("claude", control_events[3]["target_harness"])

    def test_model_control_expires_when_a_new_runner_process_starts(self) -> None:
        plan_dir, state_dir, config_home, _fake_codex = self.setup_loop(
            statuses=("pending", "pending")
        )
        control_queue.append_command(
            plan_dir,
            control_queue.COMMAND_SET_IMPLEMENTATION_MODEL,
            state_dir=state_dir,
            harness="codex",
            harness_model="gpt-5-control",
            effort="high",
        )

        first_completed, first_payload = self.run_loop_command(
            mode="success",
            plan_dir=plan_dir,
            state_dir=state_dir,
            config_home=config_home,
            max_sessions=1,
            harness_model="gpt-5-flag",
            effort="medium",
        )
        second_completed, second_payload = self.run_loop_command(
            mode="success",
            plan_dir=plan_dir,
            state_dir=state_dir,
            config_home=config_home,
            max_sessions=1,
            harness_model="gpt-5-flag",
            effort="medium",
        )

        self.assertEqual(0, first_completed.returncode, first_completed.stderr)
        self.assertEqual(0, second_completed.returncode, second_completed.stderr)
        first_turn = first_payload["turns"][0]
        second_turn = second_payload["turns"][0]
        self.assertEqual("gpt-5-control", first_turn["model"])
        self.assertEqual(config.MODEL_SOURCE_CONTROL, first_turn["model_source"])
        self.assertEqual("high", first_turn["effort"])
        self.assertEqual(config.EFFORT_SOURCE_CONTROL, first_turn["effort_source"])
        self.assertEqual("gpt-5-flag", second_turn["model"])
        self.assertEqual(config.MODEL_SOURCE_FLAG, second_turn["model_source"])
        self.assertEqual("medium", second_turn["effort"])
        self.assertEqual(config.EFFORT_SOURCE_FLAG, second_turn["effort_source"])

    def test_model_control_does_not_change_generated_review_invocation(self) -> None:
        plan_dir, state_dir, config_home, fake_codex = self.setup_loop(
            statuses=("complete",)
        )
        config_path = config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        configured = json.loads(config_path.read_text(encoding="utf-8"))
        configured["claude"] = {"executable": str(fake_codex)}
        config_path.write_text(json.dumps(configured), encoding="utf-8")
        review_session_body.ensure_generated_review_session(
            plan_dir, review_harness="claude"
        )
        control_queue.append_command(
            plan_dir,
            control_queue.COMMAND_SET_IMPLEMENTATION_MODEL,
            state_dir=state_dir,
            harness="claude",
            harness_model="claude-control",
            effort="high",
        )

        completed, payload = self.run_loop_command(
            mode="success-with-review",
            plan_dir=plan_dir,
            state_dir=state_dir,
            config_home=config_home,
            review_harness="claude",
            review_harness_model="claude-review",
            effort="medium",
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        turn = payload["turns"][0]
        self.assertEqual(runner.SESSION_KIND_REVIEW, turn["session_kind"])
        self.assertEqual("claude", turn["harness"])
        self.assertEqual("claude-review", turn["model"])
        self.assertEqual(config.MODEL_SOURCE_FLAG, turn["model_source"])
        self.assertEqual("medium", turn["effort"])
        self.assertEqual(config.EFFORT_SOURCE_FLAG, turn["effort_source"])
        self.assertEqual(
            "claude-review", turn["argv"][turn["argv"].index("--model") + 1]
        )
        self.assertEqual("medium", turn["argv"][turn["argv"].index("--effort") + 1])

    def test_missing_switched_harness_pauses_before_next_turn(self) -> None:
        listener = FakeRunControlListener()
        calls: list[dict[str, Any]] = []

        with tempfile.TemporaryDirectory() as tmp:
            missing_claude = Path(tmp) / "missing-claude"

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
                complete_next_pending(Path(kwargs["plan_dir"]))
                if listener.on_action is not None:
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
                runner.harnesses,
                "run_harness",
                side_effect=fake_run_harness,
            ):
                exit_code, payload, plan_dir, state_dir, _fake_codex = (
                    self.run_locked_with_listener(
                        mode="success",
                        statuses=("pending", "pending"),
                        listener=listener,
                        harness_config_overrides={
                            "claude": str(missing_claude),
                        },
                    )
                )

        self.assertEqual(1, exit_code)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["paused"])
        self.assertEqual("harness-unavailable", payload["reason"])
        self.assertEqual("harness-unavailable", payload["pause_reason"])
        self.assertEqual([1], payload["completed_sessions"])
        self.assertEqual(["1"], payload["completed_session_ids"])
        self.assertEqual(1, len(payload["turns"]))
        self.assertEqual("complete", payload["turns"][0]["status_result"])
        self.assertEqual(2, payload["final_snapshot"]["next_pending"])
        self.assertEqual("2", payload["final_snapshot"]["next_pending_id"])
        self.assertEqual(["codex"], [call["harness"] for call in calls])
        self.assertFalse(state.lock_path_for_plan(plan_dir, state_dir).exists())

        details = payload["pause_details"]
        self.assertEqual(2, details["session"])
        self.assertEqual("2", details["session_id"])
        self.assertEqual("session-2.md", details["session_file"])
        self.assertEqual("claude", details["requested_harness"])
        self.assertEqual(str(missing_claude), details["executable"])
        self.assertIsNone(details["resolved_executable"])
        self.assertEqual(payload["harness_config"]["path"], details["config_path"])
        self.assertIn("claude executable not found", details["error"])

        event_records = read_jsonl(Path(payload["events_path"]))
        self.assertEqual(
            1,
            len(
                [record for record in event_records if record["name"] == "turn_started"]
            ),
        )
        paused_event = event_records[
            self.event_index(
                event_records,
                events.RUN_PAUSED_EVENT,
                reason="harness-unavailable",
            )
        ]
        paused_details = paused_event["payload"]["details"]
        self.assertEqual("claude", paused_details["requested_harness"])
        self.assertEqual(str(missing_claude), paused_details["executable"])

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
                    commit_mode=runner.COMMIT_MODE_PER_SESSION,
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

    def test_successful_fake_harness_turn_persists_state_and_artifacts(self) -> None:
        completed, payload, plan_dir, _state_dir, fake_codex = self.run_loop(
            mode="success",
            statuses=("pending", "pending"),
            max_sessions=1,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])
        self.assertEqual("max-sessions", payload["reason"])
        self.assertEqual([1], payload["completed_sessions"])
        self.assertEqual(2, payload["final_snapshot"]["next_pending"])
        self.assertEqual(["codex"], payload["harness_config"]["configured_harnesses"])

        turn = payload["turns"][0]
        self.assertEqual(str(fake_codex), turn["argv"][0])
        self.assertEqual(payload["run_id"], turn["run_id"])
        self.assertEqual(1, turn["turn_index"])
        self.assertEqual(1, turn["session"])
        self.assertEqual(
            Path(payload["state_dir"]) / "runs" / payload["run_id"],
            Path(payload["run_dir"]),
        )
        artifact_dir = Path(turn["artifact_dir"])
        self.assertEqual(
            Path(payload["run_dir"]) / "turn-0001-session-1",
            artifact_dir,
        )
        self.assertEqual(artifact_dir / "stdout.log", Path(turn["stdout_path"]))
        self.assertEqual(artifact_dir / "stderr.log", Path(turn["stderr_path"]))
        self.assertEqual(
            artifact_dir / "last-message.txt",
            Path(turn["last_message_path"]),
        )
        self.assertEqual(artifact_dir / "result.json", Path(turn["result_path"]))
        for key in ("stdout_path", "stderr_path", "last_message_path", "result_path"):
            self.assertTrue(Path(turn[key]).exists(), key)
        events_payloads = [
            json.loads(line)
            for line in Path(payload["events_path"])
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        turn_started = next(
            event for event in events_payloads if event["name"] == "turn_started"
        )
        self.assertEqual(payload["run_id"], turn_started["payload"]["run_id"])
        self.assertEqual(1, turn_started["payload"]["turn_index"])
        self.assertEqual(str(artifact_dir), turn_started["payload"]["artifact_dir"])
        turn_finished = next(
            event for event in events_payloads if event["name"] == "turn_finished"
        )
        self.assertEqual(payload["run_id"], turn_finished["payload"]["run_id"])
        self.assertEqual(1, turn_finished["payload"]["turn_index"])
        self.assertEqual(1, turn_finished["payload"]["session"])
        self.assertEqual(str(artifact_dir), turn_finished["payload"]["artifact_dir"])
        self.assertIn("total_duration_seconds", turn)
        self.assertIn("total_duration_seconds", turn_finished["payload"])
        self.assertGreaterEqual(
            turn["total_duration_seconds"],
            turn["duration_seconds"],
        )
        self.assertEqual(
            turn["total_duration_seconds"],
            turn_finished["payload"]["total_duration_seconds"],
        )
        for key in ("stdout_path", "stderr_path", "last_message_path", "result_path"):
            self.assertEqual(turn[key], turn_finished["payload"][key])
            self.assertEqual(artifact_dir, Path(turn_finished["payload"][key]).parent)
        stream_io = turn["stream_io"]
        self.assertGreater(stream_io["stdout_chunks"], 0)
        self.assertGreater(stream_io["stdout_chunks"] + stream_io["stderr_chunks"], 0)
        self.assertEqual(
            len(Path(turn["stdout_path"]).read_text(encoding="utf-8")),
            stream_io["stdout_chars"],
        )
        self.assertEqual(
            len(Path(turn["stderr_path"]).read_text(encoding="utf-8")),
            stream_io["stderr_chars"],
        )
        self.assertGreaterEqual(stream_io["callback_seconds"], 0.0)
        self.assertEqual(stream_io, turn_finished["payload"]["stream_io"])
        self.assertEqual(
            stream_io,
            json.loads(Path(turn["result_path"]).read_text(encoding="utf-8"))[
                "stream_io"
            ],
        )
        turn_execution_finished = next(
            event
            for event in events_payloads
            if event["name"] == events.TURN_EXECUTION_FINISHED_EVENT
        )
        self.assertEqual(stream_io, turn_execution_finished["payload"]["stream_io"])
        self.assertTrue(Path(payload["state_path"]).exists())
        self.assertTrue(Path(payload["events_path"]).exists())
        self.assertIn(
            "- **Status**: complete",
            (plan_dir / "session-1.md").read_text(encoding="utf-8"),
        )
        self.assertIn(
            "- **Status**: pending",
            (plan_dir / "session-2.md").read_text(encoding="utf-8"),
        )

    def test_dotted_fake_harness_run_auto_advances_nested_sessions(self) -> None:
        plan_dir, state_dir, config_home, _fake_codex = self.setup_loop_with_ids(
            statuses=(
                ("1", "complete"),
                ("2.1", "pending"),
                ("2.2", "pending"),
                ("3", "pending"),
            )
        )

        completed, payload = self.run_loop_command(
            mode="success",
            plan_dir=plan_dir,
            state_dir=state_dir,
            config_home=config_home,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual("all-complete", payload["reason"])
        self.assertEqual(["2.1", "2.2", 3], payload["completed_sessions"])
        self.assertEqual(["2.1", "2.2", "3"], payload["completed_session_ids"])
        self.assertIsNone(payload["final_snapshot"]["next_pending"])
        self.assertIsNone(payload["final_snapshot"]["next_pending_id"])

        turns = payload["turns"]
        self.assertEqual(["2.1", "2.2", 3], [turn["session"] for turn in turns])
        self.assertEqual(["2.1", "2.2", "3"], [turn["session_id"] for turn in turns])
        self.assertNotIn("session_number", turns[0])
        self.assertNotIn("session_number", turns[1])
        self.assertEqual(3, turns[2]["session_number"])
        self.assertEqual(
            [
                Path(payload["run_dir"]) / "turn-0001-session-2.1",
                Path(payload["run_dir"]) / "turn-0002-session-2.2",
                Path(payload["run_dir"]) / "turn-0003-session-3",
            ],
            [Path(turn["artifact_dir"]) for turn in turns],
        )

        event_records = read_jsonl(Path(payload["events_path"]))
        started_ids = [
            record["payload"]["session_id"]
            for record in event_records
            if record["name"] == "turn_started"
        ]
        self.assertEqual(["2.1", "2.2", "3"], started_ids)

    def test_fake_codex_ignores_explicit_session_token_and_advances_first_pending(
        self,
    ) -> None:
        prompts = (
            "$session-run <plan-dir>/ --session 2.1 --runner-managed",
            "$session-run <plan-dir>/session-2.1.md --runner-managed",
            "$session-run <plan-dir>/session-3.md --runner-managed",
        )
        for prompt_template in prompts:
            with (
                self.subTest(prompt=prompt_template),
                tempfile.TemporaryDirectory() as tmp,
            ):
                tmp_dir = Path(tmp)
                plan_dir = tmp_dir / "plan"
                write_plan_with_ids(
                    plan_dir,
                    (
                        ("2", "pending"),
                        ("2.1", "pending"),
                        ("3", "pending"),
                    ),
                )
                fake_codex = install_fake_codex(tmp_dir)
                last_message = tmp_dir / "last-message.txt"
                prompt = prompt_template.replace("<plan-dir>", str(plan_dir))

                completed = subprocess.run(
                    [
                        str(fake_codex),
                        "exec",
                        "--add-dir",
                        str(plan_dir),
                        "-o",
                        str(last_message),
                        prompt,
                    ],
                    cwd=REPO_ROOT,
                    text=True,
                    capture_output=True,
                    check=False,
                )

                self.assertEqual(0, completed.returncode, completed.stderr)
                self.assertIn(
                    "- **Status**: complete",
                    (plan_dir / "session-2.md").read_text(encoding="utf-8"),
                )
                self.assertIn(
                    "- **Status**: pending",
                    (plan_dir / "session-2.1.md").read_text(encoding="utf-8"),
                )
                self.assertIn(
                    "- **Status**: pending",
                    (plan_dir / "session-3.md").read_text(encoding="utf-8"),
                )
                self.assertIn(
                    "Runner status: complete",
                    last_message.read_text(encoding="utf-8"),
                )

    def test_unexpected_dotted_progress_reports_expected_and_observed_ids(
        self,
    ) -> None:
        plan_dir, state_dir, config_home, _fake_codex = self.setup_loop_with_ids(
            statuses=(("2.1", "pending"), ("2.2", "pending"))
        )

        completed, payload = self.run_loop_command(
            mode="unexpected-progress",
            plan_dir=plan_dir,
            state_dir=state_dir,
            config_home=config_home,
        )

        expected = [{"before": "pending", "after": "complete", "id": "2.1"}]
        observed = [{"before": "pending", "after": "complete", "id": "2.2"}]
        self.assertEqual(1, completed.returncode)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["paused"])
        self.assertEqual("unexpected-status-change", payload["pause_reason"])
        self.assertEqual(expected, payload["expected_status_changes"])
        self.assertEqual(observed, payload["observed_status_changes"])
        self.assertEqual("2.1", payload["final_snapshot"]["next_pending"])
        self.assertEqual("2.1", payload["final_snapshot"]["next_pending_id"])
        self.assertEqual([], payload["completed_sessions"])
        self.assertEqual([], payload["completed_session_ids"])
        self.assertEqual("2.1", payload["turns"][0]["session_id"])
        self.assertEqual(
            "unexpected-status-change",
            payload["turns"][0]["status_result"],
        )
        self.assertIn(
            "- **Status**: pending",
            (plan_dir / "session-2.1.md").read_text(encoding="utf-8"),
        )
        self.assertIn(
            "- **Status**: complete",
            (plan_dir / "session-2.2.md").read_text(encoding="utf-8"),
        )

    def test_integer_fake_harness_run_keeps_numeric_payload_contract(self) -> None:
        completed, payload, _plan_dir, _state_dir, _fake_codex = self.run_loop(
            mode="success",
            statuses=("pending", "pending"),
            max_sessions=1,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual([1], payload["completed_sessions"])
        self.assertEqual(["1"], payload["completed_session_ids"])
        self.assertEqual(2, payload["final_snapshot"]["next_pending"])
        self.assertEqual("2", payload["final_snapshot"]["next_pending_id"])
        turn = payload["turns"][0]
        self.assertEqual(1, turn["session"])
        self.assertEqual("1", turn["session_id"])
        self.assertEqual(1, turn["session_number"])
        self.assertEqual(
            Path(payload["run_dir"]) / "turn-0001-session-1",
            Path(turn["artifact_dir"]),
        )

    def test_json_run_reports_explicit_model_effort_metadata_and_argv(self) -> None:
        completed, payload, _plan_dir, _state_dir, _fake_codex = self.run_loop(
            mode="success",
            statuses=("pending",),
            max_sessions=1,
            harness_model="gpt-5",
            effort="high",
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual("gpt-5", payload["model"])
        self.assertEqual(config.MODEL_SOURCE_FLAG, payload["model_source"])
        self.assertEqual("high", payload["effort"])
        self.assertEqual(config.EFFORT_SOURCE_FLAG, payload["effort_source"])

        turn = payload["turns"][0]
        self.assertEqual("gpt-5", turn["model"])
        self.assertEqual(config.MODEL_SOURCE_FLAG, turn["model_source"])
        self.assertEqual("high", turn["effort"])
        self.assertEqual(config.EFFORT_SOURCE_FLAG, turn["effort_source"])
        self.assertEqual("gpt-5", turn["argv"][turn["argv"].index("--model") + 1])
        self.assertEqual(
            'model_reasoning_effort="high"',
            turn["argv"][turn["argv"].index("-c") + 1],
        )

        result_payload = json.loads(
            Path(turn["result_path"]).read_text(encoding="utf-8")
        )
        self.assertEqual("gpt-5", result_payload["model"])
        self.assertEqual(config.MODEL_SOURCE_FLAG, result_payload["model_source"])
        self.assertEqual("high", result_payload["effort"])
        self.assertEqual(config.EFFORT_SOURCE_FLAG, result_payload["effort_source"])

        event_payloads = [
            json.loads(line)
            for line in Path(payload["events_path"])
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        for event_name in ("run_started", "turn_started", "turn_finished"):
            with self.subTest(event_name=event_name):
                event_payload = next(
                    event["payload"]
                    for event in event_payloads
                    if event["name"] == event_name
                )
                self.assertEqual("gpt-5", event_payload["model"])
                self.assertEqual(
                    config.MODEL_SOURCE_FLAG, event_payload["model_source"]
                )
                self.assertEqual("high", event_payload["effort"])
                self.assertEqual(
                    config.EFFORT_SOURCE_FLAG, event_payload["effort_source"]
                )

    def test_json_run_reports_native_model_metadata_without_argv_flags(self) -> None:
        home_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(home_tmp.cleanup)
        home = Path(home_tmp.name)
        codex_config = home / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True)
        codex_config.write_text('model = "gpt-5-codex"\n', encoding="utf-8")

        completed, payload, _plan_dir, _state_dir, _fake_codex = self.run_loop(
            mode="success",
            statuses=("pending",),
            max_sessions=1,
            extra_env={"HOME": str(home)},
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual("gpt-5-codex", payload["model"])
        self.assertEqual(config.MODEL_SOURCE_CODEX_CONFIG, payload["model_source"])
        self.assertIsNone(payload["effort"])
        self.assertIsNone(payload["effort_source"])

        turn = payload["turns"][0]
        self.assertEqual("gpt-5-codex", turn["model"])
        self.assertEqual(config.MODEL_SOURCE_CODEX_CONFIG, turn["model_source"])
        self.assertIsNone(turn["effort"])
        self.assertIsNone(turn["effort_source"])
        self.assertNotIn("--model", turn["argv"])
        self.assertNotIn("-c", turn["argv"])

    def test_commit_preflight_rejects_dirty_initial_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            repo_dir = tmp_dir / "repo"
            init_git_repo(repo_dir)
            commit_log, commit_env = self.setup_commit_harness(tmp_dir)
            (repo_dir / "dirty.txt").write_text("dirty\n", encoding="utf-8")

            completed, payload, plan_dir, _state_dir, _fake_codex = self.run_loop(
                mode="success",
                statuses=("pending",),
                cwd=repo_dir,
                commit_mode=runner.COMMIT_MODE_PER_SESSION,
                extra_env=commit_env,
            )

            self.assertEqual(1, completed.returncode)
            self.assertFalse(payload["ok"])
            self.assertTrue(payload["paused"])
            self.assertEqual("commit-preflight", payload["pause_reason"])
            self.assertEqual("commit-preflight", payload["reason"])
            self.assertIn("git status --porcelain", payload["pause_message"])
            self.assertEqual([], payload["turns"])
            self.assertEqual([], payload["commits"])
            self.assertFalse(commit_log.exists())
            self.assertIn(
                "- **Status**: pending",
                (plan_dir / "session-1.md").read_text(encoding="utf-8"),
            )

    def test_commit_per_session_commits_after_each_completed_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            repo_dir = tmp_dir / "repo"
            init_git_repo(repo_dir)
            commit_log, commit_env = self.setup_commit_harness(tmp_dir)
            initial_commit_count = int(
                git_stdout(repo_dir, "rev-list", "--count", "HEAD")
            )

            completed, payload, _plan_dir, _state_dir, _fake_codex = self.run_loop(
                mode="success",
                statuses=("pending", "pending"),
                cwd=repo_dir,
                commit_mode=runner.COMMIT_MODE_PER_SESSION,
                extra_env=commit_env,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertTrue(payload["ok"])
            self.assertFalse(payload["paused"])
            self.assertEqual("all-complete", payload["reason"])
            self.assertEqual(runner.COMMIT_MODE_PER_SESSION, payload["commit_mode"])
            self.assertEqual([1, 2], payload["completed_sessions"])
            self.assertEqual(
                initial_commit_count + 2,
                int(git_stdout(repo_dir, "rev-list", "--count", "HEAD")),
            )

            expected_cwd = str(plans.canonicalize_path(repo_dir))
            commit_records = read_jsonl(commit_log)
            self.assertEqual(2, len(commit_records))
            for record in commit_records:
                self.assertEqual(expected_cwd, record["cwd"])

            self.assertEqual(2, len(payload["commits"]))
            for expected_session, commit in enumerate(payload["commits"], start=1):
                self.assertTrue(commit["success"])
                self.assertEqual("success", commit["status_result"])
                self.assertEqual("session", commit["scope"])
                self.assertEqual(expected_session, commit["session"])
                self.assertEqual(expected_cwd, commit["cwd"])
                # The run commits with the `asl commit` its own checkout
                # ships, under the run's declared implementation harness --
                # never with a `spec` resolved off PATH. A legacy target has
                # no recorded write scope, so it stages repository-wide and
                # carries no `--pathspec`. The bound is on every run-owned
                # commit argv, including this one: the run never leaves its
                # nested message turn unbounded.
                self.assertEqual(
                    [
                        sys.executable,
                        "-m",
                        "agent_session_loop",
                        "commit",
                        "--yes",
                        "--no-verify",
                        "--timeout",
                        str(commit_message.DEFAULT_GENERATION_TIMEOUT_SECONDS),
                        "--harness",
                        "codex",
                    ],
                    commit["argv"],
                )
                self.assertTrue(Path(commit["result_path"]).exists())

            # The session commits between the harness exiting and the turn
            # being accepted: a failed commit still refuses the turn, so
            # `turn_finished` cannot precede it.
            event_records = read_jsonl(Path(payload["events_path"]))
            turn_1_executed = self.event_index(
                event_records, events.TURN_EXECUTION_FINISHED_EVENT, session=1
            )
            turn_1_finished = self.event_index(
                event_records, events.TURN_FINISHED_EVENT, session=1
            )
            commit_1_started = self.event_index(
                event_records,
                events.COMMIT_STARTED_EVENT,
                session=1,
                scope="session",
            )
            commit_1_finished = self.event_index(
                event_records,
                events.COMMIT_FINISHED_EVENT,
                session=1,
                scope="session",
            )
            turn_2_started = self.event_index(event_records, "turn_started", session=2)
            self.assertLess(turn_1_executed, commit_1_started)
            self.assertLess(commit_1_started, commit_1_finished)
            self.assertLess(commit_1_finished, turn_1_finished)
            self.assertLess(turn_1_finished, turn_2_started)

    def test_commit_at_end_commits_once_after_all_sessions_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            repo_dir = tmp_dir / "repo"
            init_git_repo(repo_dir)
            commit_log, commit_env = self.setup_commit_harness(tmp_dir)
            initial_commit_count = int(
                git_stdout(repo_dir, "rev-list", "--count", "HEAD")
            )

            completed, payload, _plan_dir, _state_dir, _fake_codex = self.run_loop(
                mode="success",
                statuses=("pending", "pending"),
                cwd=repo_dir,
                commit_mode=runner.COMMIT_MODE_END,
                extra_env=commit_env,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertTrue(payload["ok"])
            self.assertEqual("all-complete", payload["reason"])
            self.assertEqual(runner.COMMIT_MODE_END, payload["commit_mode"])
            self.assertEqual([1, 2], payload["completed_sessions"])
            self.assertEqual(1, len(payload["commits"]))
            self.assertEqual(1, len(read_jsonl(commit_log)))
            self.assertEqual(
                initial_commit_count + 1,
                int(git_stdout(repo_dir, "rev-list", "--count", "HEAD")),
            )

            commit = payload["commits"][0]
            self.assertTrue(commit["success"])
            self.assertEqual("end", commit["scope"])
            self.assertIsNone(commit["session"])

            event_records = read_jsonl(Path(payload["events_path"]))
            turn_2_finished = self.event_index(
                event_records, "turn_finished", session=2
            )
            commit_started = self.event_index(
                event_records,
                events.COMMIT_STARTED_EVENT,
                session=None,
                scope="end",
            )
            commit_finished = self.event_index(
                event_records,
                events.COMMIT_FINISHED_EVENT,
                session=None,
                scope="end",
            )
            run_finished = self.event_index(event_records, "run_finished")
            self.assertLess(turn_2_finished, commit_started)
            self.assertLess(commit_started, commit_finished)
            self.assertLess(commit_finished, run_finished)

    def test_commit_failure_soft_fails_and_the_run_carries_on(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            repo_dir = tmp_dir / "repo"
            init_git_repo(repo_dir)
            commit_log, commit_env = self.setup_commit_harness(
                tmp_dir,
                mode="failure",
            )
            initial_head = git_stdout(repo_dir, "rev-parse", "HEAD")

            completed, payload, plan_dir, _state_dir, _fake_codex = self.run_loop(
                mode="success",
                statuses=("pending", "pending"),
                cwd=repo_dir,
                commit_mode=runner.COMMIT_MODE_PER_SESSION,
                extra_env=commit_env,
            )

            # A repository that refuses every commit costs the plan its
            # commits, not its remaining sessions: the run finishes the work
            # and reports the commits it never made.
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertTrue(payload["ok"])
            self.assertFalse(payload["paused"])
            self.assertEqual("all-complete", payload["reason"])
            self.assertEqual([1, 2], payload["completed_sessions"])
            self.assertEqual(2, len(payload["turns"]))
            self.assertTrue(read_jsonl(commit_log))
            self.assertEqual(initial_head, git_stdout(repo_dir, "rev-parse", "HEAD"))

            self.assertFalse(any(commit["success"] for commit in payload["commits"]))
            commit = payload["commits"][0]
            self.assertEqual("nonzero-exit", commit["status_result"])
            # `asl commit` reports every refusal as exit 1; the runner reads
            # the reason out of the captured stderr, not out of the code.
            self.assertEqual(1, commit["returncode"])

            failures = payload["commit_failures"]
            self.assertTrue(failures)
            self.assertEqual({"session"}, {record["scope"] for record in failures})
            # Both sessions tried and both retries tried, so each session is
            # named at least once.
            self.assertEqual({"1", "2"}, {record["session_id"] for record in failures})
            self.assertEqual(
                sorted({"1", "2"}),
                sorted(
                    {
                        str(summary["session_id"])
                        for summary in payload["unresolved_commit_groups"]
                    }
                ),
            )

            event_names = [
                record["name"] for record in read_jsonl(Path(payload["events_path"]))
            ]
            self.assertNotIn("run_paused", event_names)
            self.assertIn(
                "- **Status**: complete",
                (plan_dir / "session-1.md").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "- **Status**: complete",
                (plan_dir / "session-2.md").read_text(encoding="utf-8"),
            )

    def test_transient_commit_failure_heals_before_the_next_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            repo_dir = tmp_dir / "repo"
            init_git_repo(repo_dir)
            commit_log, commit_env = self.setup_commit_harness(
                tmp_dir,
                mode="failure",
                fail_limit=1,
            )
            initial_commit_count = int(
                git_stdout(repo_dir, "rev-list", "--count", "HEAD")
            )

            completed, payload, _plan_dir, _state_dir, _fake_codex = self.run_loop(
                mode="success",
                statuses=("pending", "pending"),
                cwd=repo_dir,
                commit_mode=runner.COMMIT_MODE_PER_SESSION,
                extra_env=commit_env,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual("all-complete", payload["reason"])
            self.assertEqual([1, 2], payload["completed_sessions"])
            # The one failure was retried inside the same run, so nothing is
            # left owed and no human had to resume anything.
            self.assertEqual(1, len(payload["commit_failures"]))
            self.assertEqual([], payload["unresolved_commit_groups"])
            self.assertTrue(payload["commit_group_recovery"]["success"])
            self.assertEqual(
                initial_commit_count + 2,
                int(git_stdout(repo_dir, "rev-list", "--count", "HEAD")),
            )
            self.assertEqual("", git_stdout(repo_dir, "status", "--porcelain"))

            event_records = read_jsonl(Path(payload["events_path"]))
            names = [record["name"] for record in event_records]
            self.assertNotIn("run_paused", names)
            recovery_commit = [
                index
                for index, record in enumerate(event_records)
                if record["name"] == events.COMMIT_FINISHED_EVENT
                and record["payload"].get("session") == 1
                and record["payload"].get("success")
            ][-1]
            turn_2_started = self.event_index(event_records, "turn_started", session=2)
            self.assertLess(recovery_commit, turn_2_started)

    def test_legacy_failed_commit_is_retried_before_next_session_on_resume(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, config_home, _fake_codex = self.setup_loop(
                statuses=("pending", "pending")
            )
            subprocess.run(
                ["git", "init"], cwd=plan_dir, capture_output=True, check=True
            )
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"],
                cwd=plan_dir,
                capture_output=True,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Test User"],
                cwd=plan_dir,
                capture_output=True,
                check=True,
            )
            progress_repair.repair_progress_journal(plan_dir)
            plans.write_readiness_metadata(
                plan_dir,
                plans.ReadinessMetadata(
                    state="loop-ready",
                    readiness_sha256=plans.stable_readiness_sha256(plan_dir),
                    checked_at="2026-08-08T00:00:00Z",
                ),
            )
            plans.save_plan_metadata(plan_dir, plan_dir)
            subprocess.run(
                ["git", "add", "-A"], cwd=plan_dir, capture_output=True, check=True
            )
            subprocess.run(
                ["git", "commit", "-m", "initial plan"],
                cwd=plan_dir,
                capture_output=True,
                check=True,
            )
            commit_log, failing_env = self.setup_commit_harness(
                tmp_dir, mode="failure", mutation_path=None
            )
            # `--max-sessions 1` ends the run while the group is still owed,
            # which is the only way a failed commit now outlives its run: the
            # retry that would otherwise heal it happens at the next commit
            # boundary, and this run has none left.
            first, first_payload = self.run_loop_command(
                mode="success",
                plan_dir=plan_dir,
                state_dir=state_dir,
                config_home=config_home,
                cwd=plan_dir,
                max_sessions=1,
                commit_mode=runner.COMMIT_MODE_PER_SESSION,
                extra_env=failing_env,
            )
            self.assertEqual(0, first.returncode, first.stderr)
            self.assertFalse(first_payload["paused"])
            self.assertEqual([1], first_payload["completed_sessions"])
            self.assertEqual(
                ["1"],
                [
                    str(summary["session_id"])
                    for summary in first_payload["unresolved_commit_groups"]
                ],
            )

            env = os.environ.copy()
            env.update(
                {
                    "XDG_CONFIG_HOME": str(config_home),
                    "FAKE_CODEX_MODE": "success",
                    "FAKE_CODEX_COMMIT_LOG": str(commit_log),
                    "PYTHONPATH": str(REPO_ROOT)
                    + os.pathsep
                    + os.environ.get("PYTHONPATH", ""),
                }
            )
            env.pop("FAKE_CODEX_COMMIT_MODE", None)
            dry_run = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "run",
                    str(plan_dir),
                    "--resume",
                    "--dry-run",
                    "--state-dir",
                    str(state_dir),
                    "--json",
                ],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, dry_run.returncode, dry_run.stderr)
            dry_payload = json.loads(dry_run.stdout)
            self.assertTrue(dry_payload["commit_group_recovery"]["pending"])
            # The first run spent its in-run retry on the same group, so two
            # refused turns are on the log and a dry run adds none.
            self.assertEqual(2, len(read_jsonl(commit_log)))

            second = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "run",
                    str(plan_dir),
                    "--resume",
                    "--state-dir",
                    str(state_dir),
                    "--json",
                ],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(0, second.returncode, second.stderr)
            second_payload = json.loads(second.stdout)
            self.assertTrue(second_payload["commit_group_recovery"]["success"])
            self.assertEqual([2], second_payload["completed_sessions"])
            self.assertEqual([], second_payload["unresolved_commit_groups"])
            self.assertEqual(4, len(read_jsonl(commit_log)))
            event_records = read_jsonl(Path(second_payload["events_path"]))
            recovery_commit = [
                index
                for index, record in enumerate(event_records)
                if record["name"] == events.COMMIT_FINISHED_EVENT
                and record["payload"].get("session") == 1
            ][-1]
            turn_2_started = self.event_index(event_records, "turn_started", session=2)
            self.assertLess(recovery_commit, turn_2_started)

    def test_default_run_does_not_require_or_create_commits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp) / "repo"
            init_git_repo(repo_dir)
            initial_commit_count = int(
                git_stdout(repo_dir, "rev-list", "--count", "HEAD")
            )

            completed, payload, _plan_dir, _state_dir, _fake_codex = self.run_loop(
                mode="success",
                statuses=("pending",),
                cwd=repo_dir,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertTrue(payload["ok"])
            self.assertEqual("all-complete", payload["reason"])
            self.assertIsNone(payload["commit_mode"])
            self.assertEqual([], payload["commits"])
            self.assertEqual([1], payload["completed_sessions"])
            self.assertEqual(
                initial_commit_count,
                int(git_stdout(repo_dir, "rev-list", "--count", "HEAD")),
            )

            event_names = [
                record["name"] for record in read_jsonl(Path(payload["events_path"]))
            ]
            self.assertNotIn(events.COMMIT_STARTED_EVENT, event_names)
            self.assertNotIn(events.COMMIT_FINISHED_EVENT, event_names)

    def test_text_run_emits_human_progress_to_stderr(self) -> None:
        plan_dir, state_dir, config_home, _fake_codex = self.setup_loop(
            statuses=("pending",)
        )

        completed = subprocess.run(
            self.run_command(plan_dir=plan_dir, state_dir=state_dir),
            cwd=REPO_ROOT,
            env=self.loop_env(
                config_home=config_home,
                mode="streaming-success",
                extra={"FAKE_CODEX_STEP_SLEEP": "0.05"},
            ),
            text=True,
            capture_output=True,
            timeout=LOOP_RUN_TIMEOUT_SECONDS,
            check=False,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("Plan:", completed.stdout)
        self.assertIn("Completed session 1", completed.stdout)
        self.assertIn("Run complete: 1 session(s) completed", completed.stdout)
        timestamp = r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] "
        self.assertRegex(
            completed.stderr,
            timestamp + r"run started: harness codex; run ",
        )
        self.assertIn("run dir ", completed.stderr)
        self.assertIn("state ", completed.stderr)
        self.assertIn("events ", completed.stderr)
        self.assertRegex(
            completed.stderr,
            timestamp + r"session 1/1 started",
        )
        self.assertRegex(
            completed.stderr,
            timestamp
            + r"session 1/1 finished "
            + r"\(status complete, return code 0, duration [^,]+, total [^)]+\)",
        )
        self.assertIn(
            "session 1/1 finished (status complete, return code 0, duration ",
            completed.stderr,
        )
        self.assertIn("run finished: run ", completed.stderr)
        self.assertIn("reason all-complete", completed.stderr)
        self.assertNotIn("stdout=", completed.stderr)
        self.assertNotIn("stderr=", completed.stderr)
        self.assertNotIn("result=", completed.stderr)

    def test_run_note_is_persisted_and_printed_on_completion(self) -> None:
        plan_dir, state_dir, config_home, _fake_codex = self.setup_loop(
            statuses=("pending",)
        )
        note = "operator reference ABC-123"

        completed = subprocess.run(
            self.run_command(
                plan_dir=plan_dir,
                state_dir=state_dir,
                note=note,
            ),
            cwd=REPO_ROOT,
            env=self.loop_env(config_home=config_home, mode="success"),
            text=True,
            capture_output=True,
            timeout=LOOP_RUN_TIMEOUT_SECONDS,
            check=False,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn(f"Note: {note}", completed.stdout)
        self.assertIn(f"note: {note}", completed.stderr)
        payload = self.read_run_payload(plan_dir=plan_dir, state_dir=state_dir)
        self.assertTrue(payload["ok"])
        self.assertEqual("all-complete", payload["reason"])
        self.assertEqual(note, payload["note"])

    def test_text_run_emits_final_paused_progress_line(self) -> None:
        plan_dir, state_dir, config_home, _fake_codex = self.setup_loop(
            statuses=("pending",)
        )

        completed = subprocess.run(
            self.run_command(plan_dir=plan_dir, state_dir=state_dir),
            cwd=REPO_ROOT,
            env=self.loop_env(config_home=config_home, mode="failure"),
            text=True,
            capture_output=True,
            timeout=LOOP_RUN_TIMEOUT_SECONDS,
            check=False,
        )

        self.assertEqual(1, completed.returncode)
        self.assertIn("Run paused: nonzero-exit", completed.stderr)
        self.assertRegex(
            completed.stderr,
            r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] "
            r"run paused: run [^,]+, "
            r"reason nonzero-exit, duration \d+\.\ds",
        )

    def test_json_run_stays_quiet_and_status_reports_active_turn(self) -> None:
        plan_dir, state_dir, config_home, _fake_codex = self.setup_loop(
            statuses=("pending",)
        )
        run_process = subprocess.Popen(
            self.run_command(plan_dir=plan_dir, state_dir=state_dir, json_output=True),
            cwd=REPO_ROOT,
            env=self.loop_env(
                config_home=config_home,
                mode="streaming-success",
                extra={"FAKE_CODEX_STEP_SLEEP": "0.8"},
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        def cleanup_run_process() -> None:
            if run_process.poll() is None:
                run_process.kill()
            try:
                run_process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                run_process.kill()
                run_process.communicate(timeout=5)

        self.addCleanup(cleanup_run_process)

        state_path = state.plan_state_dir(plan_dir, state_dir) / events.STATE_FILE_NAME

        def active_turn_payload():
            if not state_path.exists():
                return None
            raw = json.loads(state_path.read_text(encoding="utf-8"))
            active_turn = raw.get("active_turn")
            return active_turn if isinstance(active_turn, dict) else None

        active_turn = wait_until(active_turn_payload)
        self.assertEqual(1, active_turn["session"])
        self.assertEqual(1, active_turn["turn_index"])
        self.assertTrue(active_turn["stdout_path"].endswith("/stdout.log"))
        self.assertTrue(active_turn["stderr_path"].endswith("/stderr.log"))

        status_completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "agent_session_loop",
                "status",
                str(plan_dir),
                "--state-dir",
                str(state_dir),
                "--json",
            ],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        status_payload = json.loads(status_completed.stdout)
        self.assertTrue(status_payload["ok"])
        self.assertEqual(1, status_payload["active_turn"]["session"])
        self.assertIn("elapsed_seconds", status_payload["active_turn"])
        self.assertIn("stdout_path", status_payload["active_turn"])

        status_text = subprocess.run(
            [
                sys.executable,
                "-m",
                "agent_session_loop",
                "status",
                str(plan_dir),
                "--state-dir",
                str(state_dir),
            ],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertIn("Active turn: session 1 turn 1", status_text.stdout)
        self.assertIn("Stdout:", status_text.stdout)
        self.assertIn("Stderr:", status_text.stdout)

        run_stdout, run_stderr = run_process.communicate(
            timeout=RUN_EXIT_TIMEOUT_SECONDS
        )
        self.assertEqual("", run_stderr)
        run_payload = json.loads(run_stdout)
        self.assertTrue(run_payload["ok"])
        self.assertEqual([1], run_payload["completed_sessions"])

    def test_tail_follows_streaming_logs_across_sessions(self) -> None:
        plan_dir, state_dir, config_home, _fake_codex = self.setup_loop(
            statuses=("pending", "pending")
        )
        run_process = subprocess.Popen(
            self.run_command(plan_dir=plan_dir, state_dir=state_dir, json_output=True),
            cwd=REPO_ROOT,
            env=self.loop_env(
                config_home=config_home,
                mode="streaming-success",
                extra={"FAKE_CODEX_STEP_SLEEP": "0.15"},
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(
            lambda: run_process.kill() if run_process.poll() is None else None
        )

        events_path = (
            state.plan_state_dir(plan_dir, state_dir) / events.EVENTS_FILE_NAME
        )

        def has_first_turn_started():
            if not events_path.exists():
                return False
            return "turn_started" in events_path.read_text(encoding="utf-8")

        wait_until(has_first_turn_started)
        tail_process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "agent_session_loop",
                "tail",
                str(plan_dir),
                "--state-dir",
                str(state_dir),
                "--lines",
                "200",
            ],
            cwd=REPO_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(
            lambda: tail_process.kill() if tail_process.poll() is None else None
        )

        run_stdout, run_stderr = run_process.communicate(
            timeout=RUN_EXIT_TIMEOUT_SECONDS
        )
        tail_stdout, tail_stderr = tail_process.communicate(
            timeout=RUN_EXIT_TIMEOUT_SECONDS
        )
        self.assertEqual("", run_stderr)
        run_payload = json.loads(run_stdout)
        self.assertTrue(run_payload["ok"])
        self.assertEqual([1, 2], run_payload["completed_sessions"])

        self.assertEqual(0, tail_process.returncode, tail_stderr)
        self.assertEqual("", tail_stderr)
        timestamp = r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] "
        self.assertRegex(
            tail_stdout,
            timestamp + r"session 1/2 started",
        )
        self.assertIn("session 1/2 finished", tail_stdout)
        self.assertIn("session 2/2 started", tail_stdout)
        self.assertIn("run finished: run ", tail_stdout)
        self.assertNotIn("event turn_started", tail_stdout)
        self.assertIn(
            "stdout session=1 turn=1 | session 1 stdout before completion", tail_stdout
        )
        self.assertIn(
            "stderr session=1 turn=1 | session 1 stderr before completion", tail_stdout
        )
        self.assertIn(
            "stdout session=2 turn=2 | session 2 stdout before completion", tail_stdout
        )
        self.assertIn(
            "stderr session=2 turn=2 | session 2 stderr after completion", tail_stdout
        )

    def test_live_turn_heartbeat_updates_state_events_and_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            event_path = tmp_dir / events.EVENTS_FILE_NAME
            state_path = tmp_dir / events.STATE_FILE_NAME
            payload = {"state_path": str(state_path), "active_turn": None}
            active_turn = {
                "run_id": "run-1",
                "turn_index": 1,
                "session": 1,
                "total_sessions": 3,
                "harness": "codex",
                "artifact_dir": str(tmp_dir / "turn"),
                "stdout_path": str(tmp_dir / "stdout.log"),
                "stderr_path": str(tmp_dir / "stderr.log"),
                "last_message_path": str(tmp_dir / "last-message.txt"),
                "result_path": str(tmp_dir / "result.json"),
                "started_at": runner._isoformat(runner._utc_now()),
                "finished_at": None,
                "duration_seconds": None,
                "idle_seconds_since_previous_turn": 0.0,
                "last_activity_at": None,
                "last_activity_stream": None,
                "last_activity_preview": "",
                "last_heartbeat_at": None,
                "elapsed_seconds": 0.0,
                "idle_seconds": 0.0,
                "heartbeat_count": 0,
                "stdout_chars": 0,
                "stderr_chars": 0,
                "action_required": None,
            }
            progress = runner._ProgressReporter(enabled=True)
            stderr = io.StringIO()

            live_turn = runner._LiveTurn(
                payload=payload,
                event_path=event_path,
                progress=progress,
                active_turn=active_turn,
                heartbeat_interval=0.01,
            )
            with redirect_stderr(stderr):
                live_turn.start()
                wait_until(
                    lambda: (
                        event_path.exists()
                        and events.TURN_HEARTBEAT_EVENT
                        in event_path.read_text(encoding="utf-8")
                    )
                )
                live_turn.stop()
                live_turn.finish(runner._utc_now())

            state_payload = json.loads(state_path.read_text(encoding="utf-8"))
            event_records = [
                json.loads(line)
                for line in event_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertIsNone(state_payload["active_turn"])
            self.assertTrue(
                any(
                    record["name"] == events.TURN_HEARTBEAT_EVENT
                    for record in event_records
                )
            )
            heartbeat_payloads = [
                record["payload"]
                for record in event_records
                if record["name"] == events.TURN_HEARTBEAT_EVENT
            ]
            self.assertEqual(
                {"codex"}, {payload["harness"] for payload in heartbeat_payloads}
            )
            stderr_text = stderr.getvalue()
            timestamp = r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] "
            self.assertRegex(
                stderr_text,
                timestamp + r"session 1/3 started",
            )
            self.assertRegex(
                stderr_text,
                timestamp + r"session 1/3 heartbeat \(elapsed",
            )

    def test_initial_and_live_turn_state_include_active_verification_key(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            write_plan(plan_dir, ("pending",))
            snapshot = plans.inspect_plan(plan_dir)
            plan_state_dir = state.plan_state_dir(plan_dir, state_dir)
            run_id = "run-1"
            run_dir = plan_state_dir / "runs" / run_id
            event_path = plan_state_dir / events.EVENTS_FILE_NAME
            state_path = plan_state_dir / events.STATE_FILE_NAME
            lock_info = state.LockInfo(
                path=plan_state_dir / state.LOCK_FILE_NAME,
                pid=123,
                plan_dir=str(plans.canonicalize_path(plan_dir)),
                created_at="2026-06-20T10:00:00+00:00",
                stale=False,
                reason="active",
                raw={},
            )
            args = argparse.Namespace(
                dry_run=False,
                review_harness=None,
                harness="codex",
                harness_model=None,
                effort=None,
                unsafe_bypass=True,
                no_mailbox=False,
                note=None,
                allow_not_loop_ready=False,
            )
            mailbox_activation = runner._mailbox_activation_payload(args, plan_dir)
            payload = runner._initial_payload(
                args=args,
                snapshot=snapshot,
                state_dir=plan_state_dir,
                run_dir=run_dir,
                event_path=event_path,
                state_path=state_path,
                lock_info=lock_info,
                cwd=tmp_dir,
                run_id=run_id,
                commit_mode=None,
                mailbox_activation=mailbox_activation,
            )
            persistence = runner._RunPersistence()

            persistence.write_state(payload)
            initial_state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertIn("active_verification", initial_state)
            self.assertIsNone(initial_state["active_verification"])
            self.assertFalse(initial_state["mailbox_enabled"])
            self.assertEqual(
                mailbox_activation,
                initial_state["mailbox_activation"],
            )

            active_turn = {
                "run_id": run_id,
                "turn_index": 1,
                "session": 1,
                "session_id": "1",
                "total_sessions": 1,
                "harness": "codex",
                "artifact_dir": str(run_dir / "turn-0001-session-1"),
                "stdout_path": str(run_dir / "turn-0001-session-1" / "stdout.log"),
                "stderr_path": str(run_dir / "turn-0001-session-1" / "stderr.log"),
                "last_message_path": str(
                    run_dir / "turn-0001-session-1" / "last-message.txt"
                ),
                "result_path": str(run_dir / "turn-0001-session-1" / "result.json"),
                "started_at": runner._isoformat(runner._utc_now()),
                "finished_at": None,
                "duration_seconds": None,
                "idle_seconds_since_previous_turn": 0.0,
                "last_activity_at": None,
                "last_activity_stream": None,
                "last_activity_preview": "",
                "last_heartbeat_at": None,
                "elapsed_seconds": 0.0,
                "idle_seconds": 0.0,
                "heartbeat_count": 0,
                "stdout_chars": 0,
                "stderr_chars": 0,
                "action_required": None,
            }
            live_turn = runner._LiveTurn(
                payload=payload,
                event_path=event_path,
                progress=runner._ProgressReporter(enabled=False),
                active_turn=active_turn,
                persistence=persistence,
                heartbeat_interval=60,
            )
            try:
                live_turn.start()
                active_state = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertIsInstance(active_state["active_turn"], dict)
                self.assertIn("active_verification", active_state)
                self.assertIsNone(active_state["active_verification"])
            finally:
                live_turn.stop()
                live_turn.finish(runner._utc_now())

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
            persistence = runner._RunPersistence()
            progress = runner._ProgressReporter(enabled=True)
            stderr = io.StringIO()

            live_verification = runner._LiveVerification(
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
                live_verification.start()
                live_verification.check_started(
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
                live_verification.check_finished(
                    check_index=0,
                    name="pytest",
                    kind="command",
                    role=verification.CHECK_ROLE_GATE,
                    status="pass",
                    duration_seconds=1.25,
                    failure_reason=None,
                    returncode=0,
                )
                live_verification.check_skipped(
                    check_index=1,
                    name="focused",
                    kind="command",
                    on_failure_of=("pytest",),
                    reason="condition-not-met",
                )
                live_verification.finish(
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
            persistence = runner._RunPersistence()
            live_verification = runner._LiveVerification(
                event_path=event_path,
                payload=payload,
                progress=runner._ProgressReporter(enabled=False),
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

            live_verification.start()
            after_start_state = json.loads(state_path.read_text(encoding="utf-8"))[
                "active_verification"
            ]
            self.assertEqual(2, after_start_state["check_count"])
            self.assertEqual(
                ["file_exists", "regex_in_file"], after_start_state["check_kinds"]
            )
            self.assertEqual(["SVC", "OAK"], after_start_state["check_repos"])
            self.assertIsNone(after_start_state["current_check_index"])

            live_verification.check_started(
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

            live_verification.check_finished(
                check_index=0,
                name="readme",
                kind="file_exists",
                role=verification.CHECK_ROLE_GATE,
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

            live_verification.finish(
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
            live_verification = runner._LiveVerification(
                event_path=event_path,
                payload=payload,
                progress=runner._ProgressReporter(enabled=False),
                persistence=runner._RunPersistence(),
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

            live_verification.start()
            started_event = next(
                record["payload"]
                for record in read_jsonl(event_path)
                if record["name"] == events.VERIFICATION_STARTED_EVENT
            )
            self.assertTrue(started_event["parallel"])

            for index, name in ((0, "fast"), (1, "slow")):
                live_verification.check_started(
                    check_index=index,
                    name=name,
                    kind="command",
                    argv=("python3",),
                    cwd=None,
                )
            live_verification.check_finished(
                check_index=0,
                name="fast",
                kind="command",
                role=verification.CHECK_ROLE_GATE,
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

            live_verification.check_finished(
                check_index=1,
                name="slow",
                kind="command",
                role=verification.CHECK_ROLE_GATE,
                status="pass",
                duration_seconds=0.5,
                failure_reason=None,
                returncode=0,
            )
            active = json.loads(state_path.read_text(encoding="utf-8"))[
                "active_verification"
            ]
            self.assertIsNone(active["current_check_started_at"])

            live_verification.finish(passed=True, passed_count=2, reason="ok")

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
            live_verification = runner._LiveVerification(
                event_path=event_path,
                payload=payload,
                progress=runner._ProgressReporter(enabled=False),
                persistence=runner._RunPersistence(),
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

            live_verification.start()
            for index, name in ((0, "slow"), (1, "medium"), (2, "fast")):
                live_verification.check_started(
                    check_index=index,
                    name=name,
                    kind="command",
                    argv=("python3",),
                    cwd=None,
                )

            # `fast` holds the display slot and retires first, while `slow` and
            # `medium` are still running.
            live_verification.check_finished(
                check_index=2,
                name="fast",
                kind="command",
                role=verification.CHECK_ROLE_GATE,
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
            heartbeat = live_verification._record_heartbeat()
            self.assertIsNotNone(heartbeat)
            self.assertEqual(1, heartbeat["check_index"])
            self.assertEqual("medium", heartbeat["name"])

            live_verification.check_finished(
                check_index=1,
                name="medium",
                kind="command",
                role=verification.CHECK_ROLE_GATE,
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
            heartbeat = live_verification._record_heartbeat()
            self.assertIsNotNone(heartbeat)
            self.assertEqual(0, heartbeat["check_index"])
            self.assertEqual("slow", heartbeat["name"])

            # Only the last active check clears the timer and silences
            # heartbeats.
            live_verification.check_finished(
                check_index=0,
                name="slow",
                kind="command",
                role=verification.CHECK_ROLE_GATE,
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
            self.assertIsNone(live_verification._record_heartbeat())

            live_verification.finish(passed=True, passed_count=3, reason="ok")

    def test_live_verification_finish_drops_late_check_callbacks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            event_path = tmp_dir / events.EVENTS_FILE_NAME
            state_path = tmp_dir / events.STATE_FILE_NAME
            payload = {
                "state_path": str(state_path),
                "active_turn": None,
                "active_verification": None,
            }
            progress = runner._ProgressReporter(enabled=True)
            stderr = io.StringIO()
            live_verification = runner._LiveVerification(
                event_path=event_path,
                payload=payload,
                progress=progress,
                persistence=runner._RunPersistence(),
                run_id="run-6",
                turn_index=1,
                session_id="1",
                session_label="1/1",
                total_sessions=1,
                check_count=2,
                check_names=("first", "second"),
                check_kinds=("command", "command"),
                parallel=True,
                heartbeat_interval=60,
            )

            with redirect_stderr(stderr):
                live_verification.start()
                live_verification.check_started(
                    check_index=0,
                    name="first",
                    kind="command",
                    argv=("python3",),
                    cwd=None,
                )
                live_verification.finish(
                    passed=False, passed_count=0, reason="interrupted"
                )
                event_count_at_finish = len(read_jsonl(event_path))

                # Late callbacks from checks the interrupt abandoned.
                live_verification.check_finished(
                    check_index=0,
                    name="first",
                    kind="command",
                    role=verification.CHECK_ROLE_GATE,
                    status="pass",
                    duration_seconds=1.0,
                    failure_reason=None,
                    returncode=0,
                )
                live_verification.check_started(
                    check_index=1,
                    name="second",
                    kind="command",
                    argv=("python3",),
                    cwd=None,
                )
                self.assertIsNone(live_verification._record_heartbeat())

            self.assertEqual(event_count_at_finish, len(read_jsonl(event_path)))
            self.assertEqual(
                events.VERIFICATION_FINISHED_EVENT,
                read_jsonl(event_path)[-1]["name"],
            )
            self.assertIsNone(
                json.loads(state_path.read_text(encoding="utf-8"))[
                    "active_verification"
                ]
            )
            stderr_text = stderr.getvalue()
            self.assertNotIn("verification check 1/2 finished", stderr_text)
            self.assertNotIn("verification check 2/2 started", stderr_text)

    def test_live_verification_callback_during_heartbeat_shutdown_is_inert(
        self,
    ) -> None:
        """A callback landing while `finish` joins the heartbeat thread is inert.

        `finish` claims the terminal transition before the join, so a worker
        callback that arrives during that wait can neither append a check event
        nor resurrect `active_verification`. The heartbeat thread is parked
        inside its own progress write, which holds `finish` in its shutdown for
        as long as the test needs.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            event_path = tmp_dir / events.EVENTS_FILE_NAME
            state_path = tmp_dir / events.STATE_FILE_NAME
            progress = runner._ProgressReporter(enabled=True)
            stderr = io.StringIO()
            live_verification = runner._LiveVerification(
                event_path=event_path,
                payload={
                    "state_path": str(state_path),
                    "active_turn": None,
                    "active_verification": None,
                },
                progress=progress,
                persistence=runner._RunPersistence(),
                run_id="run-8",
                turn_index=1,
                session_id="1",
                session_label="1/1",
                total_sessions=1,
                check_count=2,
                check_names=("first", "second"),
                check_kinds=("command", "command"),
                parallel=True,
                heartbeat_interval=0.01,
            )

            parked = threading.Event()
            release = threading.Event()
            emit_heartbeat = progress.verification_heartbeat

            def parked_heartbeat(heartbeat: dict[str, Any]) -> None:
                parked.set()
                release.wait(timeout=30)
                emit_heartbeat(heartbeat)

            finisher = threading.Thread(
                target=lambda: live_verification.finish(
                    passed=False, passed_count=0, reason="interrupted"
                )
            )
            with (
                redirect_stderr(stderr),
                mock.patch.object(progress, "verification_heartbeat", parked_heartbeat),
            ):
                live_verification.start()
                live_verification.check_started(
                    check_index=0,
                    name="first",
                    kind="command",
                    argv=("python3",),
                    cwd=None,
                )
                wait_until(parked.is_set)
                finisher.start()
                try:
                    # `finish` sets the stop flag on its way into the heartbeat
                    # join, and the parked heartbeat holds the progress lock,
                    # so it provably has not returned when this callback lands.
                    wait_until(live_verification._stop.is_set)
                    self.assertTrue(finisher.is_alive())
                    live_verification.check_finished(
                        check_index=0,
                        name="first",
                        kind="command",
                        role=verification.CHECK_ROLE_GATE,
                        status="pass",
                        duration_seconds=1.0,
                        failure_reason=None,
                        returncode=0,
                    )
                finally:
                    release.set()
                    finisher.join(timeout=30)
            self.assertFalse(finisher.is_alive())

            self.assertEqual(
                [
                    events.VERIFICATION_STARTED_EVENT,
                    events.VERIFICATION_CHECK_STARTED_EVENT,
                    events.VERIFICATION_HEARTBEAT_EVENT,
                    events.VERIFICATION_FINISHED_EVENT,
                ],
                [record["name"] for record in read_jsonl(event_path)],
            )
            self.assertIsNone(
                json.loads(state_path.read_text(encoding="utf-8"))[
                    "active_verification"
                ]
            )
            stderr_lines = [
                line for line in stderr.getvalue().splitlines() if line.strip()
            ]
            self.assertNotIn("verification check 1/2 finished", stderr.getvalue())
            self.assertIn("verification failed for session 1/1", stderr_lines[-1])

    def test_live_verification_finish_waits_for_stalled_check_progress(
        self,
    ) -> None:
        """A callback stalled mid-write cannot print behind the terminal line.

        The callback passes its lifecycle check and stalls before its progress
        line reaches stderr, then `finish` runs to completion in that window.
        The terminal message has to queue behind the stalled write instead of
        racing ahead of it.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            event_path = tmp_dir / events.EVENTS_FILE_NAME
            state_path = tmp_dir / events.STATE_FILE_NAME
            progress = runner._ProgressReporter(enabled=True)
            stderr = io.StringIO()
            live_verification = runner._LiveVerification(
                event_path=event_path,
                payload={
                    "state_path": str(state_path),
                    "active_turn": None,
                    "active_verification": None,
                },
                progress=progress,
                persistence=runner._RunPersistence(),
                run_id="run-9",
                turn_index=1,
                session_id="1",
                session_label="1/1",
                total_sessions=1,
                check_count=2,
                check_names=("first", "second"),
                check_kinds=("command", "command"),
                parallel=True,
                heartbeat_interval=60,
            )

            parked = threading.Event()
            release = threading.Event()
            emit_check_finished = progress.verification_check_finished

            def parked_check_finished(check: dict[str, Any]) -> None:
                parked.set()
                release.wait(timeout=30)
                emit_check_finished(check)

            worker = threading.Thread(
                target=lambda: live_verification.check_finished(
                    check_index=0,
                    name="first",
                    kind="command",
                    role=verification.CHECK_ROLE_GATE,
                    status="pass",
                    duration_seconds=1.0,
                    failure_reason=None,
                    returncode=0,
                )
            )
            finisher = threading.Thread(
                target=lambda: live_verification.finish(
                    passed=False, passed_count=0, reason="interrupted"
                )
            )
            with (
                redirect_stderr(stderr),
                mock.patch.object(
                    progress, "verification_check_finished", parked_check_finished
                ),
            ):
                live_verification.start()
                live_verification.check_started(
                    check_index=0,
                    name="first",
                    kind="command",
                    argv=("python3",),
                    cwd=None,
                )
                worker.start()
                try:
                    wait_until(parked.is_set)
                    finisher.start()
                    # The terminal event is durable before the terminal
                    # message is written, so `finish` is now past its
                    # lifecycle block and can only be waiting for the stalled
                    # write to release the output.
                    wait_until(
                        lambda: (
                            read_jsonl(event_path)[-1]["name"]
                            == events.VERIFICATION_FINISHED_EVENT
                        )
                    )
                    self.assertTrue(finisher.is_alive())
                finally:
                    release.set()
                    worker.join(timeout=30)
                    finisher.join(timeout=30)
            self.assertFalse(worker.is_alive())
            self.assertFalse(finisher.is_alive())

            self.assertEqual(
                [
                    events.VERIFICATION_STARTED_EVENT,
                    events.VERIFICATION_CHECK_STARTED_EVENT,
                    events.VERIFICATION_CHECK_FINISHED_EVENT,
                    events.VERIFICATION_FINISHED_EVENT,
                ],
                [record["name"] for record in read_jsonl(event_path)],
            )
            stderr_lines = [
                line for line in stderr.getvalue().splitlines() if line.strip()
            ]
            self.assertIn("verification check 1/2 finished", stderr_lines[-2])
            self.assertIn("verification failed for session 1/1", stderr_lines[-1])

    def test_parallel_interrupt_leaves_verification_finished_terminal(
        self,
    ) -> None:
        """A main-thread interrupt mid-parallel-recipe ends the phase cleanly.

        `as_completed` is where a real Ctrl-C surfaces: the main thread waits
        there while worker threads run the command checks, so the
        KeyboardInterrupt is raised in `run_verification`'s own frame with
        several checks still live. Every process of every live check that is
        still in the check's process group — the command itself and the
        descendants it spawned — must be dead before the interrupt reaches the
        runner, so nothing abandoned can resurrect `active_verification` or
        append behind `verification_finished`.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            script, grandchild_script = write_process_tree_scripts(tmp_dir)
            started_dir = tmp_dir / "started"
            total = 6
            running = 4  # `run_verification` caps the pool at four workers.
            checks = tuple(
                contracts.CommandCheck(
                    name=f"cmd-{index}",
                    argv=(
                        "python3",
                        str(script),
                        f"cmd-{index}",
                        str(tmp_dir),
                        str(grandchild_script),
                    ),
                    timeout_seconds=120.0,
                )
                for index in range(total)
            )
            contract = contracts.CompletionContract(
                session_id=plans.parse_session_id("1"),
                done_when=("done",),
                verify=contracts.VerifyRecipe(checks=checks, parallel=True),
            )

            event_path = tmp_dir / events.EVENTS_FILE_NAME
            state_path = tmp_dir / events.STATE_FILE_NAME
            payload = {
                "state_path": str(state_path),
                "active_turn": None,
                "active_verification": None,
            }
            live_verification = runner._LiveVerification(
                event_path=event_path,
                payload=payload,
                progress=runner._ProgressReporter(enabled=False),
                persistence=runner._RunPersistence(),
                run_id="run-7",
                turn_index=1,
                session_id="1",
                session_label="1/1",
                total_sessions=1,
                check_count=total,
                check_names=tuple(check.name for check in checks),
                check_kinds=("command",) * total,
                parallel=True,
                heartbeat_interval=0.01,
            )

            finishes: list[int] = []

            def on_check_started(event: verification.CheckStartedEvent) -> None:
                live_verification.check_started(
                    check_index=event.check_index,
                    name=event.name,
                    kind=event.kind,
                    argv=event.argv,
                    cwd=event.cwd,
                )

            def on_check_finished(event: verification.CheckFinishedEvent) -> None:
                live_verification.check_finished(
                    check_index=event.check_index,
                    name=event.name,
                    kind=event.kind,
                    role=event.role,
                    status=event.result.status,
                    duration_seconds=event.result.duration_seconds,
                    failure_reason=event.result.failure_reason,
                    returncode=event.result.returncode,
                )
                finishes.append(event.check_index)

            def interrupt_once_all_running(_futures: object) -> None:
                wait_until(lambda: len(list(started_dir.iterdir())) == running)
                raise KeyboardInterrupt

            live_verification.start()
            with (
                mock.patch.object(
                    verification, "as_completed", interrupt_once_all_running
                ),
                self.assertRaises(KeyboardInterrupt),
            ):
                verification.run_verification(
                    contract,
                    repo_root=repo_root,
                    observer=verification.VerificationObserver(
                        on_check_started=on_check_started,
                        on_check_finished=on_check_finished,
                    ),
                )

            # Every process the interrupt abandoned — command and grandchild
            # alike — is already dead, so nothing can report back or touch the
            # repository after control returned.
            started_markers = sorted(started_dir.iterdir())
            assert_marked_pids_exited(started_dir, label="command process")
            assert_marked_pids_exited(tmp_dir / "grandchildren", label="grandchild")
            self.assertEqual([], list((tmp_dir / "late").iterdir()))
            self.assertEqual([], finishes)

            live_verification.finish(passed=False, passed_count=0, reason="interrupted")
            events_at_finish = read_jsonl(event_path)

            # The two futures still queued behind the four workers were
            # cancelled rather than executed.
            self.assertEqual(running, len(started_markers))
            self.assertEqual(
                events.VERIFICATION_FINISHED_EVENT, events_at_finish[-1]["name"]
            )
            self.assertEqual("interrupted", events_at_finish[-1]["payload"]["reason"])
            self.assertEqual(events_at_finish, read_jsonl(event_path))
            self.assertIsNone(
                json.loads(state_path.read_text(encoding="utf-8"))[
                    "active_verification"
                ]
            )

    def test_live_verification_started_event_omits_parallel_when_sequential(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            event_path = tmp_dir / events.EVENTS_FILE_NAME
            state_path = tmp_dir / events.STATE_FILE_NAME
            live_verification = runner._LiveVerification(
                event_path=event_path,
                payload={
                    "state_path": str(state_path),
                    "active_turn": None,
                    "active_verification": None,
                },
                progress=runner._ProgressReporter(enabled=False),
                persistence=runner._RunPersistence(),
                run_id="run-4",
                turn_index=1,
                session_id="1",
                session_label="1/1",
                total_sessions=1,
                check_count=1,
                check_names=("only",),
                check_kinds=("command",),
                heartbeat_interval=60,
            )

            live_verification.start()
            live_verification.finish(passed=True, passed_count=1, reason="ok")

            started_event = next(
                record["payload"]
                for record in read_jsonl(event_path)
                if record["name"] == events.VERIFICATION_STARTED_EVENT
            )
            self.assertNotIn("parallel", started_event)

    def test_live_turn_action_required_payload_includes_harness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            event_path = tmp_dir / events.EVENTS_FILE_NAME
            state_path = tmp_dir / events.STATE_FILE_NAME
            payload = {"state_path": str(state_path), "active_turn": None}
            active_turn = {
                "run_id": "run-1",
                "turn_index": 1,
                "session": 1,
                "total_sessions": 3,
                "harness": "codex",
                "artifact_dir": str(tmp_dir / "turn"),
                "stdout_path": str(tmp_dir / "stdout.log"),
                "stderr_path": str(tmp_dir / "stderr.log"),
                "last_message_path": str(tmp_dir / "last-message.txt"),
                "result_path": str(tmp_dir / "result.json"),
                "started_at": runner._isoformat(runner._utc_now()),
                "finished_at": None,
                "duration_seconds": None,
                "idle_seconds_since_previous_turn": 0.0,
                "last_activity_at": None,
                "last_activity_stream": None,
                "last_activity_preview": "",
                "last_heartbeat_at": None,
                "elapsed_seconds": 0.0,
                "idle_seconds": 0.0,
                "heartbeat_count": 0,
                "stdout_chars": 0,
                "stderr_chars": 0,
                "action_required": None,
            }
            live_turn = runner._LiveTurn(
                payload=payload,
                event_path=event_path,
                progress=runner._ProgressReporter(enabled=False),
                active_turn=active_turn,
                heartbeat_interval=60,
            )

            live_turn.start()
            live_turn.record_activity("stderr", "approval required before continuing")
            # The exceptional transition is durable before the turn ends;
            # ordinary activity waits for the heartbeat instead.
            state_payload = json.loads(state_path.read_text(encoding="utf-8"))
            live_turn.stop()
            live_turn.finish(runner._utc_now())

            event_records = read_jsonl(event_path)
            action_events = [
                record["payload"]
                for record in event_records
                if record["name"] == events.ACTION_REQUIRED_EVENT
            ]
            self.assertEqual(1, len(action_events))
            self.assertEqual("codex", action_events[0]["harness"])
            self.assertEqual("codex", state_payload["action_required"]["harness"])

    def test_idle_time_excludes_the_previous_turns_verification(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        slow_check = Path(tmp.name) / "slow-check.sh"
        slow_check.write_text("#!/bin/sh\nsleep 1\nexit 0\n", encoding="utf-8")
        contract = "\n".join(
            [
                "## Completion Contract",
                "",
                "### Done when",
                "",
                "- it runs",
                "",
                "### Verify",
                "",
                "```json",
                json.dumps(
                    {
                        "session": "1",
                        "checks": [
                            {
                                "name": "slow-check",
                                "type": "command",
                                "argv": ["sh", str(slow_check)],
                                "timeout_seconds": 60,
                            }
                        ],
                    },
                    indent=2,
                ),
                "```",
            ]
        )

        def inject_contract(plan_dir: Path, _state_dir: Path) -> None:
            write_plan(
                plan_dir, ("pending", "pending"), contracts_by_id={"1": contract}
            )

        exit_code, payload, _plan_dir, _state_dir, _fake_codex = (
            self.run_locked_with_listener(
                mode="success",
                statuses=("pending", "pending"),
                listener=FakeRunControlListener(),
                before_run=inject_contract,
            )
        )

        self.assertEqual(0, exit_code, payload)
        turns = payload["turns"]
        self.assertEqual(2, len(turns))
        verification_finished_at = next(
            runner._parse_datetime(record["payload"]["finished_at"])
            for record in read_jsonl(Path(payload["events_path"]))
            if record["name"] == events.VERIFICATION_FINISHED_EVENT
        )
        harness_exited_at = runner._parse_datetime(turns[0]["finished_at"])
        idle_started_at = self.idle_started_at(turns[1])
        # The idle clock starts after the runner's own verification and
        # finalization, so it cannot begin back at the harness's exit.
        self.assertGreaterEqual(idle_started_at, verification_finished_at)
        self.assertGreaterEqual(
            (idle_started_at - harness_exited_at).total_seconds(), 1.0
        )

    def test_idle_time_excludes_generated_review_creation(self) -> None:
        plan_dir, state_dir, config_home, fake_codex = self.setup_loop(
            statuses=("pending",)
        )
        self.enable_fake_review_harness(config_home, fake_codex)

        exit_code, payload = self.run_locked_review_run(
            plan_dir=plan_dir,
            state_dir=state_dir,
            config_home=config_home,
            run_id="review-creation-idle",
            review_outcomes="all-clear",
            max_review_fix_cycles=0,
        )

        self.assertEqual(0, exit_code, payload)
        turns = payload["turns"]
        self.assertEqual(["1", "2"], [turn["session_id"] for turn in turns])
        review_created_at = self.last_event_created_at(
            Path(payload["events_path"]),
            events.REVIEW_SESSION_CREATED_EVENT,
        )
        # Creating the generated review session is runner-owned work between
        # the two turns, so the review turn's idle clock starts after it.
        self.assertGreaterEqual(self.idle_started_at(turns[1]), review_created_at)

    def test_idle_time_excludes_review_repair_materialization(self) -> None:
        plan_dir, state_dir, config_home, fake_codex = self.setup_loop(
            statuses=("pending",)
        )
        self.enable_fake_review_harness(config_home, fake_codex)
        # The repair child runs behind `_repair_commit_preflight`, which
        # refuses to start on a dirty writable repository. Give the run its own
        # clean repository so the gate answers to this test rather than to
        # whatever is uncommitted in this checkout.
        repo_dir = plan_dir.parent / "repo"
        init_git_repo(repo_dir)

        _first_exit, first_payload = self.run_locked_review_run(
            plan_dir=plan_dir,
            state_dir=state_dir,
            config_home=config_home,
            run_id="repair-idle-seed",
            review_outcomes="findings,all-clear",
            max_review_fix_cycles=0,
            cwd=repo_dir,
        )
        self.assertEqual(
            "higher-cap-required",
            first_payload["review_resume"]["blocker"],
            first_payload,
        )
        # Reopening the implementation session makes the resumed run execute a
        # turn *before* the all-complete branch materializes the repair child,
        # which is the only arrangement where that materialization can be
        # charged to a later turn as idle time.
        session_one = plan_dir / "session-1.md"
        session_one.write_text(
            session_one.read_text(encoding="utf-8").replace(
                "- **Status**: complete", "- **Status**: pending", 1
            ),
            encoding="utf-8",
        )

        exit_code, payload = self.run_locked_review_run(
            plan_dir=plan_dir,
            state_dir=state_dir,
            config_home=config_home,
            run_id="repair-idle-resume",
            review_outcomes="findings,all-clear",
            max_review_fix_cycles=1,
            cwd=repo_dir,
        )

        self.assertEqual(0, exit_code, payload)
        turns = payload["turns"]
        self.assertEqual(["1", "2.1", "3"], [turn["session_id"] for turn in turns])
        reopened_at = self.last_event_created_at(
            Path(payload["events_path"]),
            events.REVIEW_REPAIR_REOPENED_EVENT,
        )
        # Materializing the repair child, re-gating the grown plan and
        # refreshing readiness are all runner-owned work, so the repair turn's
        # idle clock cannot start back at the previous turn's finalization.
        self.assertGreaterEqual(self.idle_started_at(turns[1]), reopened_at)

    def test_idle_time_excludes_review_verdict_retry(self) -> None:
        plan_dir, state_dir, config_home, fake_codex = self.setup_loop(
            statuses=("pending",)
        )
        self.enable_fake_review_harness(config_home, fake_codex)

        exit_code, payload = self.run_locked_review_run(
            plan_dir=plan_dir,
            state_dir=state_dir,
            config_home=config_home,
            run_id="verdict-retry-idle",
            review_outcomes="invalid-artifact,all-clear",
            max_review_fix_cycles=0,
        )

        self.assertEqual(0, exit_code, payload)
        turns = payload["turns"]
        self.assertEqual(["1", "2", "2"], [turn["session_id"] for turn in turns])
        retried_at = self.last_event_created_at(
            Path(payload["events_path"]),
            events.REVIEW_VERDICT_RETRY_EVENT,
        )
        # Rejecting the verdict and reverting the review session is
        # runner-owned work on a path that returns to the loop top with a
        # bare `continue`, so the retried turn's idle clock starts after it.
        self.assertGreaterEqual(self.idle_started_at(turns[2]), retried_at)

    def runner_source(self) -> str:
        return Path(runner.__file__).read_text(encoding="utf-8")

    def session_loop_node(self, tree: ast.Module | None = None) -> ast.While:
        if tree is None:
            tree = ast.parse(self.runner_source())
        loops = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.While)
            and any(
                isinstance(name, ast.Name) and name.id == "runner_work_finished_at"
                for statement in node.body
                for name in ast.walk(statement)
            )
        ]
        self.assertEqual(1, len(loops), "expected exactly one session loop")
        return loops[0]

    def idle_baseline_assignments(self, node: ast.AST) -> list[ast.Assign]:
        return [
            statement
            for statement in ast.walk(node)
            if isinstance(statement, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "runner_work_finished_at"
                for target in statement.targets
            )
        ]

    def assigns_idle_baseline_pin(self, statement: ast.AST, value: bool) -> bool:
        return (
            isinstance(statement, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "idle_baseline_pinned"
                for target in statement.targets
            )
            and isinstance(statement.value, ast.Constant)
            and statement.value.value is value
        )

    def pins_idle_baseline(self, statement: ast.stmt) -> bool:
        return self.assigns_idle_baseline_pin(statement, True)

    def clears_idle_baseline_pin(self, statement: ast.AST) -> bool:
        return self.assigns_idle_baseline_pin(statement, False)

    def pinned_loop_head_arm(self, loop: ast.While) -> ast.If:
        arm: ast.stmt | None = loop.body[0]
        while isinstance(arm, ast.If):
            if isinstance(arm.test, ast.Name) and arm.test.id == "idle_baseline_pinned":
                return arm
            arm = arm.orelse[0] if len(arm.orelse) == 1 else None
        self.fail(
            "the session loop's first statement must branch on "
            "idle_baseline_pinned alone; an arm selected by anything else "
            "leaves paths where the pin is neither honoured nor cleared"
        )

    def followed_by_pin(self, loop: ast.While, assignment: ast.Assign) -> bool:
        for node in ast.walk(loop):
            for field in ("body", "orelse", "finalbody"):
                block = getattr(node, field, None)
                if not isinstance(block, list):
                    continue
                for index, statement in enumerate(block):
                    if statement is not assignment:
                        continue
                    following = block[index + 1 :]
                    return bool(following) and self.pins_idle_baseline(following[0])
        return False

    def check_the_pinned_arm_clears_the_pin(self, tree: ast.Module) -> None:
        arm = self.pinned_loop_head_arm(self.session_loop_node(tree))
        for statement in arm.body:
            if self.clears_idle_baseline_pin(statement):
                return
            self.assertFalse(
                any(
                    self.clears_idle_baseline_pin(node) for node in ast.walk(statement)
                ),
                "the pinned arm clears idle_baseline_pinned only on some "
                "branch; a pass that leaves it set suppresses every later "
                "loop-top refresh, so the baseline stays at the pinning "
                f"path's instant (agent_session_loop/runner.py:{statement.lineno})",
            )
            self.assertFalse(
                any(
                    isinstance(node, ast.Break | ast.Continue | ast.Return | ast.Raise)
                    for node in ast.walk(statement)
                ),
                "the pinned arm can leave the loop head before clearing "
                "idle_baseline_pinned, which strands the pin for every later "
                f"pass (agent_session_loop/runner.py:{statement.lineno})",
            )
        self.fail(
            "the pinned arm of the session loop's first statement must reset "
            "idle_baseline_pinned to False: the pin is a one-shot opt-out, and "
            "leaving it set turns the single structural refresh into a "
            "baseline that never moves again"
        )

    def tree_with_pinned_arm_body(self, body: tuple[str, ...]) -> ast.Module:
        source = self.runner_source()
        arm = self.pinned_loop_head_arm(self.session_loop_node(ast.parse(source)))
        first, last = arm.body[0], arm.body[-1]
        assert last.end_lineno is not None
        lines = source.splitlines()
        lines[first.lineno - 1 : last.end_lineno] = [
            " " * first.col_offset + line for line in body
        ]
        return ast.parse("\n".join(lines))

    def test_idle_baseline_is_refreshed_where_the_loop_cannot_bypass_it(self) -> None:
        tree = ast.parse(self.runner_source())
        self.check_the_pinned_arm_clears_the_pin(tree)
        for mutation in (
            ("pass",),
            ("if first_loop_pass:", "    idle_baseline_pinned = False"),
        ):
            with (
                self.subTest(mutation=mutation),
                self.assertRaises(AssertionError),
            ):
                self.check_the_pinned_arm_clears_the_pin(
                    self.tree_with_pinned_arm_body(mutation)
                )

        loop = self.session_loop_node(tree)
        head = loop.body[0]
        structural = self.idle_baseline_assignments(head)
        self.assertEqual(
            1,
            len(structural),
            "the idle baseline must be refreshed by the session loop's first "
            "statement, so no path back to the loop top can bypass it",
        )

        pins = [
            statement
            for statement in ast.walk(loop)
            if self.pins_idle_baseline(statement)
        ]
        self.assertEqual(
            1,
            len(pins),
            "only the mailbox reply wait may opt out of the structural "
            "refresh; a second opt-out is a path whose idle clock silently "
            "stops tracking runner work",
        )

        for assignment in self.idle_baseline_assignments(loop):
            if assignment in structural:
                continue
            self.assertTrue(
                self.followed_by_pin(loop, assignment),
                "a hand-stamped idle baseline outside the loop top must pin "
                "itself, otherwise the next pass overwrites it and the "
                "per-path stamping this replaced is back "
                f"(agent_session_loop/runner.py:{assignment.lineno})",
            )

    def test_a_new_turn_supersedes_the_previous_turns_action_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            event_path = tmp_dir / events.EVENTS_FILE_NAME
            state_path = tmp_dir / events.STATE_FILE_NAME
            payload: dict[str, Any] = {
                "state_path": str(state_path),
                "active_turn": None,
                "action_required": {
                    "run_id": "run-1",
                    "turn_index": 1,
                    "reason": "approval-required",
                    "message": "approval required before continuing",
                    "source": "stderr",
                    "matched": "approval required",
                },
            }
            active_turn = {
                "run_id": "run-1",
                "turn_index": 2,
                "session": 2,
                "total_sessions": 3,
                "harness": "codex",
                "artifact_dir": str(tmp_dir / "turn"),
                "stdout_path": str(tmp_dir / "stdout.log"),
                "stderr_path": str(tmp_dir / "stderr.log"),
                "last_message_path": str(tmp_dir / "last-message.txt"),
                "result_path": str(tmp_dir / "result.json"),
                "started_at": runner._isoformat(runner._utc_now()),
                "finished_at": None,
                "duration_seconds": None,
                "idle_seconds_since_previous_turn": 0.0,
                "last_activity_at": None,
                "last_activity_stream": None,
                "last_activity_preview": "",
                "last_heartbeat_at": None,
                "elapsed_seconds": 0.0,
                "idle_seconds": 0.0,
                "heartbeat_count": 0,
                "stdout_chars": 0,
                "stderr_chars": 0,
                "action_required": None,
            }
            live_turn = runner._LiveTurn(
                payload=payload,
                event_path=event_path,
                progress=runner._ProgressReporter(enabled=False),
                active_turn=active_turn,
                heartbeat_interval=60,
            )

            live_turn.start()
            live_turn.stop()
            live_turn.finish(runner._utc_now())

            state_payload = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertIsNone(payload["action_required"])
        self.assertIsNone(state_payload["action_required"])

    def test_burst_activity_is_coalesced_and_control_persists_latest_snapshot(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            event_path = tmp_dir / events.EVENTS_FILE_NAME
            state_path = tmp_dir / events.STATE_FILE_NAME
            payload = {
                "state_path": str(state_path),
                "run_id": "run-1",
                "harness": "codex",
                "active_turn": None,
                "control": runner._initial_control_payload(),
            }
            active_turn = {
                "run_id": "run-1",
                "turn_index": 1,
                "session": 1,
                "total_sessions": 2,
                "artifact_dir": str(tmp_dir / "turn"),
                "stdout_path": str(tmp_dir / "stdout.log"),
                "stderr_path": str(tmp_dir / "stderr.log"),
                "last_message_path": str(tmp_dir / "last-message.txt"),
                "result_path": str(tmp_dir / "result.json"),
                "started_at": runner._isoformat(runner._utc_now()),
                "finished_at": None,
                "duration_seconds": None,
                "idle_seconds_since_previous_turn": 0.0,
                "last_activity_at": None,
                "last_activity_stream": None,
                "last_activity_preview": "",
                "last_heartbeat_at": None,
                "elapsed_seconds": 0.0,
                "idle_seconds": 0.0,
                "heartbeat_count": 0,
                "stdout_chars": 0,
                "stderr_chars": 0,
                "action_required": None,
            }
            persistence = runner._RunPersistence()
            live_turn = runner._LiveTurn(
                payload=payload,
                event_path=event_path,
                progress=runner._ProgressReporter(enabled=False),
                active_turn=active_turn,
                persistence=persistence,
                heartbeat_interval=60,
            )
            control_write_started = threading.Event()
            release_control_write = threading.Event()
            preview_started = threading.Event()
            release_preview = threading.Event()
            delayed_once = False
            thread_errors: list[BaseException] = []
            original_write_json = runner._write_json_file
            original_preview_text = turn_execution.preview_text
            state_write_count = 0

            def delayed_write_json(
                path: Path,
                payload_arg: dict[str, Any],
                *,
                durable: bool = False,
            ) -> None:
                nonlocal delayed_once, state_write_count
                path = Path(path)
                active = payload_arg.get("active_turn")
                control = payload_arg.get("control")
                if path == state_path:
                    state_write_count += 1
                if (
                    path == state_path
                    and not delayed_once
                    and isinstance(active, dict)
                    and active.get("last_activity_stream") == "stdout"
                    and isinstance(control, dict)
                    and control.get("next_harness") == "claude"
                ):
                    delayed_once = True
                    serialized = (
                        json.dumps(payload_arg, sort_keys=True, indent=2) + "\n"
                    )
                    control_write_started.set()
                    release_control_write.wait(timeout=2)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    temporary_path = path.with_name(f".{path.name}.blocked.tmp")
                    temporary_path.write_text(serialized, encoding="utf-8")
                    os.replace(temporary_path, path)
                    return
                original_write_json(path, payload_arg, durable=durable)

            def record_control() -> None:
                try:
                    runner._record_run_control_action(
                        event_path=event_path,
                        payload=payload,
                        control_payload=dict(payload["control"]),
                        action=run_control.RunControlAction(
                            action="harness-selected",
                            stop_after_current=False,
                            next_harness="claude",
                        ),
                        context=active_turn,
                        persistence=persistence,
                    )
                except BaseException as error:
                    thread_errors.append(error)

            def record_latest_activity() -> None:
                try:
                    live_turn.record_activity("stdout", "latest output\n")
                except BaseException as error:
                    thread_errors.append(error)

            def delayed_preview(text: str, *, limit: int = 160) -> str:
                preview_started.set()
                release_preview.wait(timeout=2)
                return original_preview_text(text, limit=limit)

            with mock.patch.object(
                runner,
                "_write_json_file",
                side_effect=delayed_write_json,
            ):
                for index in range(5_000):
                    live_turn.record_activity("stdout", f"output {index}\n")

                self.assertEqual(0, state_write_count)
                self.assertFalse(state_path.exists())
                self.assertEqual(
                    sum(len(f"output {index}\n") for index in range(5_000)),
                    payload["active_turn"]["stdout_chars"],
                )
                self.assertEqual(
                    "output 4999", payload["active_turn"]["last_activity_preview"]
                )

                with mock.patch.object(
                    turn_execution, "preview_text", side_effect=delayed_preview
                ):
                    activity_thread = threading.Thread(target=record_latest_activity)
                    activity_thread.start()
                    self.assertTrue(preview_started.wait(timeout=2))

                    control_thread = threading.Thread(target=record_control)
                    control_thread.start()
                    time.sleep(0.05)
                    self.assertTrue(control_thread.is_alive())
                    self.assertFalse(control_write_started.is_set())

                    release_preview.set()
                    activity_thread.join(timeout=2)
                self.assertTrue(control_write_started.wait(timeout=2))

                release_control_write.set()
                control_thread.join(timeout=2)

            self.assertFalse(control_thread.is_alive())
            self.assertFalse(activity_thread.is_alive())
            if thread_errors:
                raise thread_errors[0]
            self.assertEqual(1, state_write_count)

            state_payload = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(
                "stdout", state_payload["active_turn"]["last_activity_stream"]
            )
            self.assertEqual(
                "latest output",
                state_payload["active_turn"]["last_activity_preview"],
            )
            self.assertEqual("claude", state_payload["control"]["next_harness"])
            self.assertEqual(
                "harness-selected", state_payload["control"]["last_action"]
            )
            control_events = [
                record["payload"]
                for record in read_jsonl(event_path)
                if record["name"] == events.RUN_CONTROL_EVENT
            ]
            self.assertEqual(
                ["harness-selected"], [event["action"] for event in control_events]
            )

    def test_heartbeat_persists_accumulated_activity_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            event_path = tmp_dir / events.EVENTS_FILE_NAME
            state_path = tmp_dir / events.STATE_FILE_NAME
            payload = {"state_path": str(state_path), "active_turn": None}
            started_at = runner._isoformat(runner._utc_now())
            active_turn = {
                "run_id": "run-1",
                "turn_index": 1,
                "session": 1,
                "total_sessions": 1,
                "artifact_dir": str(tmp_dir / "turn"),
                "stdout_path": str(tmp_dir / "stdout.log"),
                "stderr_path": str(tmp_dir / "stderr.log"),
                "last_message_path": str(tmp_dir / "last-message.txt"),
                "result_path": str(tmp_dir / "result.json"),
                "started_at": started_at,
                "last_activity_at": started_at,
                "heartbeat_count": 0,
                "stdout_chars": 0,
                "stderr_chars": 0,
            }
            persistence = runner._RunPersistence()
            live_turn = runner._LiveTurn(
                payload=payload,
                event_path=event_path,
                progress=runner._ProgressReporter(enabled=False),
                active_turn=active_turn,
                persistence=persistence,
                heartbeat_interval=60,
            )
            write_state = mock.Mock(wraps=persistence.write_state)

            with mock.patch.object(persistence, "write_state", write_state):
                live_turn.record_activity("stdout", "first\n")
                live_turn.record_activity("stderr", "second\n")
                self.assertEqual(0, write_state.call_count)

                heartbeat = live_turn._record_heartbeat()

            self.assertEqual(1, write_state.call_count)
            persisted = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(6, persisted["active_turn"]["stdout_chars"])
            self.assertEqual(7, persisted["active_turn"]["stderr_chars"])
            self.assertEqual("stderr", persisted["active_turn"]["last_activity_stream"])
            self.assertEqual(
                "second", persisted["active_turn"]["last_activity_preview"]
            )
            self.assertEqual(1, heartbeat["heartbeat_count"])

    def test_retry_after_failed_turn_keeps_first_run_artifacts(self) -> None:
        plan_dir, state_dir, config_home, _fake_codex = self.setup_loop(
            statuses=("pending",)
        )

        first_completed, first_payload = self.run_loop_command(
            mode="failure",
            plan_dir=plan_dir,
            state_dir=state_dir,
            config_home=config_home,
        )

        self.assertEqual(1, first_completed.returncode)
        self.assertFalse(first_payload["ok"])
        first_turn = first_payload["turns"][0]
        first_stderr = Path(first_turn["stderr_path"])
        first_result = Path(first_turn["result_path"])
        first_artifact_dir = Path(first_turn["artifact_dir"])
        self.assertEqual(
            Path(first_payload["run_dir"]) / "turn-0001-session-1",
            first_artifact_dir,
        )
        self.assertTrue(first_stderr.exists())
        self.assertTrue(first_result.exists())
        original_stderr = first_stderr.read_text(encoding="utf-8")
        original_result = json.loads(first_result.read_text(encoding="utf-8"))
        self.assertIn("fake codex failure", original_stderr)
        self.assertEqual(7, original_result["returncode"])
        self.assertEqual(first_payload["run_id"], original_result["run_id"])

        second_completed, second_payload = self.run_loop_command(
            mode="success",
            plan_dir=plan_dir,
            state_dir=state_dir,
            config_home=config_home,
            max_sessions=1,
        )

        self.assertEqual(0, second_completed.returncode, second_completed.stderr)
        self.assertTrue(second_payload["ok"])
        self.assertNotEqual(first_payload["run_id"], second_payload["run_id"])
        second_turn = second_payload["turns"][0]
        self.assertNotEqual(first_artifact_dir, Path(second_turn["artifact_dir"]))
        self.assertTrue(first_stderr.exists())
        self.assertTrue(first_result.exists())
        self.assertEqual(original_stderr, first_stderr.read_text(encoding="utf-8"))
        self.assertEqual(
            original_result,
            json.loads(first_result.read_text(encoding="utf-8")),
        )
        self.assertIn(
            "- **Status**: complete",
            (plan_dir / "session-1.md").read_text(encoding="utf-8"),
        )

    def test_nonzero_fake_harness_pauses_without_status_change(self) -> None:
        completed, payload, _plan_dir, _state_dir, _fake_codex = self.run_loop(
            mode="failure",
            statuses=("pending",),
        )

        self.assertEqual(1, completed.returncode)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["paused"])
        self.assertEqual("nonzero-exit", payload["pause_reason"])
        self.assertIn(payload["pause_reason"], events.PAUSE_REASONS)

    def test_timed_out_fake_harness_reports_timeout_pause(self) -> None:
        completed, payload, _plan_dir, _state_dir, _fake_codex = self.run_loop(
            mode="timeout",
            statuses=("pending",),
            timeout=1,
        )

        self.assertEqual(1, completed.returncode)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["paused"])
        self.assertEqual("timeout", payload["pause_reason"])
        self.assertTrue(payload["turns"][0]["timed_out"])

    def test_bad_fake_harness_progress_reports_multiple_status_changes(self) -> None:
        completed, payload, _plan_dir, _state_dir, _fake_codex = self.run_loop(
            mode="multiple-progress",
            statuses=("pending", "pending"),
        )

        self.assertEqual(1, completed.returncode)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["paused"])
        self.assertEqual("multiple-status-changes", payload["pause_reason"])
        self.assertEqual(2, len(payload["observed_status_changes"]))

    def test_no_progress_fake_harness_reports_no_status_change(self) -> None:
        completed, payload, _plan_dir, _state_dir, _fake_codex = self.run_loop(
            mode="no-progress",
            statuses=("pending",),
        )

        expected = [{"number": 1, "before": "pending", "after": "complete"}]
        self.assertEqual(1, completed.returncode)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["paused"])
        self.assertEqual("no-status-change", payload["pause_reason"])
        self.assertEqual("no-status-change", payload["reason"])
        self.assertEqual(expected, payload["expected_status_changes"])
        self.assertEqual([], payload["observed_status_changes"])
        self.assertEqual(payload["pause_details"], payload["details"])
        self.assertEqual(expected, payload["details"]["expected_status_changes"])
        self.assertEqual([], payload["details"]["observed_status_changes"])

    def test_unexpected_progress_reports_unexpected_status_change(self) -> None:
        completed, payload, plan_dir, _state_dir, _fake_codex = self.run_loop(
            mode="unexpected-progress",
            statuses=("pending", "pending"),
        )

        expected = [{"number": 1, "before": "pending", "after": "complete"}]
        observed = [{"number": 2, "before": "pending", "after": "complete"}]
        self.assertEqual(1, completed.returncode)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["paused"])
        self.assertEqual("unexpected-status-change", payload["pause_reason"])
        self.assertEqual(expected, payload["expected_status_changes"])
        self.assertEqual(observed, payload["observed_status_changes"])
        self.assertEqual(observed, payload["details"]["observed_status_changes"])
        self.assertEqual(1, payload["final_snapshot"]["next_pending"])
        self.assertIn(
            "- **Status**: pending",
            (plan_dir / "session-1.md").read_text(encoding="utf-8"),
        )
        self.assertIn(
            "- **Status**: complete",
            (plan_dir / "session-2.md").read_text(encoding="utf-8"),
        )

    def test_runner_pause_signal_reports_runner_pause(self) -> None:
        completed, payload, _plan_dir, _state_dir, _fake_codex = self.run_loop(
            mode="runner-pause",
            statuses=("pending",),
        )

        self.assertEqual(1, completed.returncode)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["paused"])
        self.assertEqual("runner-pause", payload["pause_reason"])
        self.assertEqual("operator requested pause", payload["pause_message"])
        self.assertEqual({"session": 1, "source": "last_message"}, payload["details"])
        turn = payload["turns"][0]
        self.assertEqual("operator requested pause", turn["runner_pause"])
        self.assertEqual("last_message", turn["runner_pause_source"])

    def test_structured_runner_pause_signal_uses_embedded_reason(self) -> None:
        completed, payload, _plan_dir, _state_dir, _fake_codex = self.run_loop(
            mode="runner-pause-unsafe",
            statuses=("pending",),
        )

        self.assertEqual(1, completed.returncode)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["paused"])
        self.assertEqual("unsafe", payload["pause_reason"])
        self.assertEqual("unsafe", payload["reason"])
        self.assertIn("plan readiness is not current", payload["pause_message"])
        self.assertIn(payload["pause_reason"], events.PAUSE_REASONS)
        self.assertEqual("unsafe", payload["turns"][0]["status_result"])

    def test_noop_runner_pause_signal_completes_without_pausing(self) -> None:
        completed, payload, _plan_dir, _state_dir, _fake_codex = self.run_loop(
            mode="success-runner-pause-none",
            statuses=("pending",),
        )

        self.assertEqual(0, completed.returncode)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])
        self.assertEqual("all-complete", payload["reason"])
        self.assertEqual([1], payload["completed_sessions"])
        turn = payload["turns"][0]
        self.assertEqual("complete", turn["status_result"])
        self.assertNotIn("runner_pause", turn)

    def test_malformed_after_turn_reports_malformed_plan_pause(self) -> None:
        completed, payload, _plan_dir, _state_dir, _fake_codex = self.run_loop(
            mode="malformed-after-turn",
            statuses=("pending",),
        )

        self.assertEqual(1, completed.returncode)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["paused"])
        self.assertEqual("malformed-plan", payload["pause_reason"])
        self.assertEqual(
            "plan could not be inspected after the harness turn",
            payload["pause_message"],
        )
        self.assertEqual(1, payload["details"]["session"])
        self.assertIn("missing context.md", payload["details"]["error"])
        turn = payload["turns"][0]
        self.assertIsNone(turn["after"])
        self.assertIn("missing context.md", turn["plan_error"])

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

    def test_turn_sweep_that_killed_something_appends_a_process_sweep_event(
        self,
    ) -> None:
        """A leaking turn is recorded; the sweeps that found nothing are not."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmp_dir = Path(tmp.name)
        run_cwd = tmp_dir / "repo"
        init_git_repo(run_cwd)
        marker_dir = tmp_dir / "leftovers"
        marker_dir.mkdir()
        self.addCleanup(kill_marked_pids, marker_dir)
        marker = marker_dir / "turn"
        leftover = self._leftover_script(tmp_dir, marker)
        harness = tmp_dir / "leaking-codex"
        harness.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env python3",
                    "import pathlib, subprocess, sys, time",
                    f"sys.path.insert(0, {str(FAKE_CODEX_FIXTURE.parent)!r})",
                    f"marker = pathlib.Path({str(marker)!r})",
                    "subprocess.Popen(",
                    f"    [sys.executable, {str(leftover)!r}, str(marker)],",
                    "    stdout=subprocess.DEVNULL,",
                    "    stderr=subprocess.DEVNULL,",
                    ")",
                    "while not marker.exists():",
                    "    time.sleep(0.01)",
                    "import fake_codex",
                    "raise SystemExit(fake_codex.main())",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        harness.chmod(harness.stat().st_mode | stat.S_IXUSR)

        exit_code, payload, plan_dir, state_dir, _fake_codex = (
            self.run_locked_with_listener(
                mode="success",
                statuses=("pending",),
                listener=FakeRunControlListener(),
                cwd=run_cwd,
                harness_config_overrides={"codex": str(harness)},
            )
        )

        self.assertEqual(0, exit_code, payload)
        leaked = int(marker.read_text(encoding="utf-8"))
        sweeps = self.sweep_events(plan_dir, state_dir)
        # One turn leaked, and nothing else in the run did: a sweep with an
        # empty report is the normal case and stays out of the log.
        self.assertEqual(1, len(sweeps), sweeps)
        self.assertEqual(events.EVENT_SCHEMA_VERSION, sweeps[0]["version"])
        sweep = sweeps[0]["payload"]
        self.assertEqual("turn", sweep["phase"])
        self.assertEqual("test-run", sweep["run_id"])
        self.assertEqual(1, sweep["turn_index"])
        self.assertEqual(1, sweep["session"])
        self.assertEqual("1", sweep["session_id"])
        self.assertNotIn("check", sweep)
        self.assertEqual([leaked], sweep["killed"])
        self.assertEqual([], sweep["kill_failed"])
        self.assertEqual([], sweep["survivors_after"])
        # It stayed in the turn's group, so the pgid enumeration named it.
        self.assertEqual([leaked], sweep["enumeration"]["pgid_members"])
        assert_marked_pids_exited(marker_dir, label="leaked turn child")

    def test_verification_sweep_records_the_check_that_leaked(self) -> None:
        """The same event, from the other sweep site, carries the check name."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmp_dir = Path(tmp.name)
        run_cwd = tmp_dir / "repo"
        init_git_repo(run_cwd)
        marker_dir = tmp_dir / "leftovers"
        marker_dir.mkdir()
        self.addCleanup(kill_marked_pids, marker_dir)
        marker = marker_dir / "check"
        leftover = self._leftover_script(tmp_dir, marker)
        check_script = tmp_dir / "leaky-check.sh"
        check_script.write_text(
            "#!/bin/sh\n"
            f'"{sys.executable}" "{leftover}" "{marker}" '
            "</dev/null >/dev/null 2>&1 &\n"
            # Exiting before the leftover has published its PID would leave the
            # assertions below racing a process the sweep already killed.
            f'until [ -f "{marker}" ]; do sleep 0.05; done\n'
            "exit 0\n",
            encoding="utf-8",
        )
        contract = "\n".join(
            [
                "## Completion Contract",
                "",
                "### Done when",
                "",
                "- it runs",
                "",
                "### Verify",
                "",
                "```json",
                json.dumps(
                    {
                        "session": "1",
                        "checks": [
                            {
                                "name": "leaky-check",
                                "type": "command",
                                "argv": ["sh", str(check_script)],
                                "timeout_seconds": 60,
                            }
                        ],
                    },
                    indent=2,
                ),
                "```",
            ]
        )

        def inject_contract(plan_dir: Path, _state_dir: Path) -> None:
            write_plan(plan_dir, ("pending",), contracts_by_id={"1": contract})

        exit_code, payload, plan_dir, state_dir, _fake_codex = (
            self.run_locked_with_listener(
                mode="success",
                statuses=("pending",),
                listener=FakeRunControlListener(),
                cwd=run_cwd,
                before_run=inject_contract,
            )
        )

        self.assertEqual(0, exit_code, payload)
        leaked = int(marker.read_text(encoding="utf-8"))
        sweeps = self.sweep_events(plan_dir, state_dir)
        self.assertEqual(1, len(sweeps), sweeps)
        self.assertEqual(events.EVENT_SCHEMA_VERSION, sweeps[0]["version"])
        sweep = sweeps[0]["payload"]
        self.assertEqual("verification", sweep["phase"])
        self.assertEqual("leaky-check", sweep["check"])
        self.assertEqual("test-run", sweep["run_id"])
        self.assertEqual(1, sweep["session"])
        self.assertEqual([leaked], sweep["killed"])
        self.assertEqual([], sweep["kill_failed"])
        self.assertEqual([], sweep["survivors_after"])
        assert_marked_pids_exited(marker_dir, label="leaked check child")

    def test_post_result_leaf_recovery_advances_and_releases_lock(self) -> None:
        """A terminal fake-Claude result with a live leaf recovers in-place."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            workspace = tmp_dir / "workspace"
            config_home = tmp_dir / "config"
            control_dir = tmp_dir / "fake-claude-state"
            control_dir.mkdir()
            write_plan(plan_dir, ("pending", "pending"))
            init_git_repo(workspace)

            leaf_script = tmp_dir / "fake-claude-leaf.py"
            leaf_script.write_text(
                textwrap.dedent(
                    """\
                    import os
                    import pathlib
                    import signal
                    import sys
                    import time

                    control = pathlib.Path(os.environ["FAKE_CLAUDE_STATE"])
                    (control / "leaf-pid").write_text(str(os.getpid()))

                    def stop(*_args):
                        (control / "leaf-termed").write_text("yes")
                        raise SystemExit(0)

                    signal.signal(signal.SIGTERM, stop)
                    (control / "leaf-ready").write_text("yes")
                    time.sleep(60)
                    """
                ),
                encoding="utf-8",
            )
            fake_claude = tmp_dir / "fake-claude"
            fake_claude.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import json
                    import os
                    import pathlib
                    import subprocess
                    import sys

                    plan_index = sys.argv.index("--add-dir") + 1
                    plan = pathlib.Path(sys.argv[plan_index])
                    control = pathlib.Path(os.environ["FAKE_CLAUDE_STATE"])
                    pending = [
                        path
                        for path in sorted(plan.glob("session-*.md"))
                        if "- **Status**: pending" in path.read_text()
                    ]
                    target = pending[0]
                    text = target.read_text()
                    text = text.replace(
                        "- **Status**: pending", "- **Status**: complete", 1
                    )
                    text = text.replace(
                        "- **Key decisions**: (none)",
                        "- **Key decisions**: fake Claude completed the session.",
                        1,
                    )
                    target.write_text(text)
                    result = {
                        "type": "result",
                        "subtype": "success",
                        "result": "Runner status: complete",
                        "num_turns": 1,
                    }
                    first = not (control / "first-used").exists()
                    if first:
                        (control / "first-used").write_text("yes")
                        leaf = subprocess.Popen(
                            [sys.executable, os.environ["FAKE_CLAUDE_LEAF"]]
                        )
                        while not (control / "leaf-ready").exists():
                            pass
                        print(json.dumps(result), flush=True)
                        leaf.wait()
                        message = {
                            "type": "assistant",
                            "message": {
                                "content": [
                                    {
                                        "type": "text",
                                        "text": "background task reaped",
                                    }
                                ]
                            },
                        }
                        print(json.dumps(message), flush=True)
                        print(json.dumps(result), flush=True)
                    else:
                        print(json.dumps(result), flush=True)
                    """
                ),
                encoding="utf-8",
            )
            fake_claude.chmod(fake_claude.stat().st_mode | stat.S_IXUSR)
            config_path = config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                json.dumps({"claude": {"executable": str(fake_claude)}}),
                encoding="utf-8",
            )
            argv = [
                "run",
                str(plan_dir),
                "--cwd",
                str(workspace),
                "--harness",
                "claude",
                "--state-dir",
                str(state_dir),
                "--json",
                "--unsafe-bypass",
                "--allow-not-loop-ready",
                "--no-mailbox",
            ]
            args = cli.build_parser().parse_args(argv)
            real_live_turn = runner._LiveTurn

            def fast_live_turn(*call_args, **call_kwargs):
                call_kwargs["heartbeat_interval"] = 0.05
                return real_live_turn(*call_args, **call_kwargs)

            output = io.StringIO()
            with (
                patched_env(
                    XDG_CONFIG_HOME=str(config_home),
                    FAKE_CLAUDE_STATE=str(control_dir),
                    FAKE_CLAUDE_LEAF=str(leaf_script),
                    ASL_HANG_RESULT_GRACE_SECONDS="0.05",
                    ASL_HANG_CONFIRM_SAMPLES="1",
                ),
                mock.patch.object(runner, "_LiveTurn", side_effect=fast_live_turn),
                redirect_stdout(output),
            ):
                code = runner.run_from_args(args, run_argv=argv)

            self.assertEqual(0, code, output.getvalue())
            self.assertTrue((control_dir / "leaf-termed").exists())
            self.assertEqual(
                ["complete", "complete"],
                [session.status for session in plans.inspect_plan(plan_dir).sessions],
            )
            lock = state.read_lock_info(plan_dir, state_dir)
            self.assertIsNone(lock)
            records = read_jsonl(
                state.plan_state_dir(plan_dir, state_dir) / events.EVENTS_FILE_NAME
            )
            suspected = [
                record
                for record in records
                if record["name"] == events.TURN_HANG_SUSPECTED_EVENT
            ]
            self.assertEqual(1, len(suspected))
            recovery = suspected[0]["payload"]["hang_suspected"]["recovery"]
            leaf_pid = int((control_dir / "leaf-pid").read_text())
            self.assertEqual([leaf_pid], recovery["candidate_pids"])
            self.assertEqual([leaf_pid], recovery["signaled_pids"])
            self.assertEqual([], recovery["signal_failures"])
            self.assertNotIn("worker_hung", output.getvalue())


class HarnessReaderJoinAndHookTests(unittest.TestCase):
    """Direct adapter-run tests for bounded reader joins and observation hooks."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp_dir = Path(tmp.name)

    def _write_executable(self, name: str, source: str) -> Path:
        path = self.tmp_dir / name
        path.write_text(source, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def test_sixty_thousand_stream_lines_keep_exact_logs_and_bounded_writes(
        self,
    ) -> None:
        line_count = 30_000
        fake_harness = self._write_executable(
            "fake-copilot-burst",
            "\n".join(
                [
                    "#!/usr/bin/env python3",
                    "import sys, threading",
                    f"line_count = {line_count}",
                    "def emit(stream, prefix):",
                    "    for index in range(line_count):",
                    "        stream.write(f'{prefix}-{index:05d}\\n')",
                    "    stream.flush()",
                    "threads = [",
                    "    threading.Thread(target=emit, args=(sys.stdout, 'out')) ,",
                    "    threading.Thread(target=emit, args=(sys.stderr, 'err')) ,",
                    "]",
                    "for thread in threads:",
                    "    thread.start()",
                    "for thread in threads:",
                    "    thread.join()",
                    "",
                ]
            ),
        )
        state_path = self.tmp_dir / "state.json"
        event_path = self.tmp_dir / "events.jsonl"
        started_at = runner._isoformat(runner._utc_now())
        payload: dict[str, Any] = {"state_path": str(state_path)}
        active_turn = {
            "run_id": "run-burst",
            "turn_index": 1,
            "session": 1,
            "session_id": "1",
            "total_sessions": 1,
            "harness": "copilot",
            "artifact_dir": str(self.tmp_dir / "turn"),
            "stdout_path": str(self.tmp_dir / "stdout.log"),
            "stderr_path": str(self.tmp_dir / "stderr.log"),
            "last_message_path": str(self.tmp_dir / "last-message.txt"),
            "result_path": str(self.tmp_dir / "result.json"),
            "started_at": started_at,
            "last_activity_at": started_at,
            "heartbeat_count": 0,
            "stdout_chars": 0,
            "stderr_chars": 0,
        }
        persistence = runner._RunPersistence(write_state=False, append_events=False)
        live_turn = runner._LiveTurn(
            payload=payload,
            event_path=event_path,
            progress=runner._ProgressReporter(enabled=False),
            active_turn=active_turn,
            persistence=persistence,
            heartbeat_interval=3600,
        )
        write_state = mock.Mock(wraps=persistence.write_state)

        with mock.patch.object(persistence, "write_state", write_state):
            live_turn.start()
            result = _PythonScriptCopilotAdapter(fake_harness).run(
                cwd=self.tmp_dir,
                plan_dir=self.tmp_dir,
                prompt="noop",
                stream_callback=live_turn.record_activity,
            )
            active_snapshot = dict(payload["active_turn"])
            live_turn.finish(runner._utc_now())

        expected_stdout = "".join(f"out-{index:05d}\n" for index in range(line_count))
        expected_stderr = "".join(f"err-{index:05d}\n" for index in range(line_count))
        self.assertEqual(expected_stdout, result.stdout)
        self.assertEqual(expected_stderr, result.stderr)
        self.assertEqual(2, write_state.call_count)
        self.assertEqual(len(expected_stdout), active_snapshot["stdout_chars"])
        self.assertEqual(len(expected_stderr), active_snapshot["stderr_chars"])
        self.assertIn(active_snapshot["last_activity_stream"], {"stdout", "stderr"})
        assert result.stream_io is not None
        self.assertEqual(line_count, result.stream_io["stdout_chunks"])
        self.assertEqual(line_count, result.stream_io["stderr_chunks"])
        self.assertEqual(len(expected_stdout), result.stream_io["stdout_chars"])
        self.assertEqual(len(expected_stderr), result.stream_io["stderr_chars"])
        self.assertGreater(result.stream_io["callback_seconds"], 0.0)

    def test_callback_seconds_accumulates_across_both_reader_threads(self) -> None:
        fake_harness = self._write_executable(
            "fake-copilot-callback-timing",
            "\n".join(
                [
                    "#!/usr/bin/env python3",
                    "import sys, threading",
                    "def emit(stream, prefix):",
                    "    stream.write(f'{prefix}-1\\n{prefix}-2\\n')",
                    "    stream.flush()",
                    "threads = [",
                    "    threading.Thread(target=emit, args=(sys.stdout, 'out')) ,",
                    "    threading.Thread(target=emit, args=(sys.stderr, 'err')) ,",
                    "]",
                    "for thread in threads:",
                    "    thread.start()",
                    "for thread in threads:",
                    "    thread.join()",
                    "",
                ]
            ),
        )

        def slow_callback(stream_name: harnesses.StreamName, text: str) -> None:
            del stream_name, text
            time.sleep(0.003)

        result = _PythonScriptCopilotAdapter(fake_harness).run(
            cwd=self.tmp_dir,
            plan_dir=self.tmp_dir,
            prompt="noop",
            stream_callback=slow_callback,
        )

        assert result.stream_io is not None
        self.assertEqual(2, result.stream_io["stdout_chunks"])
        self.assertEqual(2, result.stream_io["stderr_chunks"])
        self.assertGreaterEqual(result.stream_io["callback_seconds"], 0.01)

    def test_reader_join_bounded_despite_pipe_holding_grandchild(self) -> None:
        root = self.tmp_dir / "trees"
        _parent_script, grandchild_script = write_process_tree_scripts(root)
        self.addCleanup(kill_marked_pids, root / "grandchildren")
        fake_harness = self._write_executable(
            "fake-copilot",
            "\n".join(
                [
                    "#!/usr/bin/env python3",
                    "import pathlib, subprocess, sys, time",
                    f"root = pathlib.Path({str(root)!r})",
                    "subprocess.Popen(",
                    "    [",
                    "        sys.executable,",
                    f"        {str(grandchild_script)!r},",
                    "        'harness',",
                    "        str(root),",
                    "    ],",
                    "    start_new_session=True,",
                    ")",
                    "while not (root / 'grandchildren' / 'harness').exists():",
                    "    time.sleep(0.01)",
                    "print('harness done', flush=True)",
                    "",
                ]
            ),
        )
        fired: list[int] = []

        start = time.monotonic()
        result = _PythonScriptCopilotAdapter(fake_harness).run(
            cwd=self.tmp_dir,
            plan_dir=self.tmp_dir,
            prompt="noop",
            on_final_result=lambda: fired.append(1),
        )
        elapsed = time.monotonic() - start

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("harness done", result.stdout)
        # The detached grandchild inherited the output pipes and is still
        # alive, so the reader threads never saw EOF: control returned through
        # the bounded join instead of waiting out the grandchild's 60s sleep.
        self.assertLess(elapsed, 30.0)
        assert_marked_pids_alive(root / "grandchildren", label="detached grandchild")
        # The passthrough processor never reports a final result, so the hook
        # stays silent for harnesses without structured output.
        self.assertEqual([], fired)

    def test_hooks_receive_real_pid_and_fire_final_result_once(self) -> None:
        pid_path = self.tmp_dir / "harness-pid"
        fake_claude = self._write_executable(
            "fake-claude",
            "\n".join(
                [
                    "#!/usr/bin/env python3",
                    "import json, os, pathlib",
                    f"pathlib.Path({str(pid_path)!r}).write_text(str(os.getpid()))",
                    "records = [",
                    "    {",
                    "        'type': 'system',",
                    "        'subtype': 'init',",
                    "        'model': 'claude-test',",
                    "        'session_id': 'session-hooks',",
                    "    },",
                    "    {",
                    "        'type': 'assistant',",
                    "        'message': {",
                    "            'content': [{'type': 'text', 'text': 'working'}],",
                    "        },",
                    "    },",
                    "    {",
                    "        'type': 'result',",
                    "        'subtype': 'success',",
                    "        'result': 'done',",
                    "        'num_turns': 1,",
                    "        'duration_ms': 10,",
                    "    },",
                    "    {'type': 'system', 'subtype': 'status', 'status': 'late'},",
                    "]",
                    "for record in records:",
                    "    print(json.dumps(record), flush=True)",
                    "print('trailing plain line', flush=True)",
                    "",
                ]
            ),
        )
        config_home = self.tmp_dir / "config"
        config_path = config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps({"claude": {"executable": str(fake_claude)}}),
            encoding="utf-8",
        )
        spawn_pids: list[int] = []
        fired: list[int] = []
        observed: list[str] = []

        with patched_env(XDG_CONFIG_HOME=str(config_home)):
            result = harnesses.run_harness(
                "claude",
                cwd=self.tmp_dir,
                plan_dir=self.tmp_dir,
                prompt="/session-run plan/",
                on_spawn=spawn_pids.append,
                on_final_result=lambda: fired.append(1),
                on_observed_model=observed.append,
            )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([int(pid_path.read_text(encoding="utf-8"))], spawn_pids)
        # Two stdout reads follow the result record; a second fire would have
        # landed there if the Event guard did not hold.
        self.assertEqual(1, len(fired))
        # The init record names the model the harness actually ran, and the
        # same Event guard keeps a later record from firing the hook again.
        self.assertEqual(["claude-test"], observed)
        self.assertEqual("claude-test", result.observed_model)
        self.assertIn("claude result: success", result.stdout)
        self.assertIn("claude status: late", result.stdout)

    def test_late_write_after_abandoned_readers_cannot_resurrect_finished_state(
        self,
    ) -> None:
        root = self.tmp_dir / "trees"
        for name in ("pending", "grandchildren", "late"):
            (root / name).mkdir(parents=True, exist_ok=True)
        self.addCleanup(kill_marked_pids, root / "grandchildren")
        grandchild_script = root / "late_writer.py"
        grandchild_script.write_text(
            "\n".join(
                [
                    "import json, os, pathlib, sys, time",
                    "root = pathlib.Path(sys.argv[1])",
                    "pending = root / 'pending' / 'harness-grandchild'",
                    "pending.write_text(str(os.getpid()))",
                    "os.replace(pending, root / 'grandchildren' / 'harness')",
                    "while not (root / 'trigger').exists():",
                    "    time.sleep(0.01)",
                    "record = {",
                    "    'type': 'result',",
                    "    'subtype': 'success',",
                    "    'result': 'resurrection',",
                    "}",
                    "sys.stdout.write(json.dumps(record) + '\\n')",
                    "sys.stdout.write('resurrection plain line\\n')",
                    "sys.stdout.flush()",
                    "(root / 'late' / 'harness').write_text('wrote')",
                    "time.sleep(60)",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        fake_claude = self._write_executable(
            "fake-claude",
            "\n".join(
                [
                    "#!/usr/bin/env python3",
                    "import json, pathlib, subprocess, sys, time",
                    f"root = pathlib.Path({str(root)!r})",
                    "subprocess.Popen(",
                    "    [",
                    "        sys.executable,",
                    f"        {str(grandchild_script)!r},",
                    "        str(root),",
                    "    ],",
                    "    start_new_session=True,",
                    ")",
                    "while not (root / 'grandchildren' / 'harness').exists():",
                    "    time.sleep(0.01)",
                    "message = {'content': [{'type': 'text', 'text': 'working'}]}",
                    "print(",
                    "    json.dumps({'type': 'assistant', 'message': message}),",
                    "    flush=True,",
                    ")",
                    "while not (root / 'drained').exists():",
                    "    time.sleep(0.01)",
                    "",
                ]
            ),
        )

        state_path = self.tmp_dir / "state.json"
        event_path = self.tmp_dir / "events.jsonl"
        stdout_log = self.tmp_dir / "stdout.log"
        stderr_log = self.tmp_dir / "stderr.log"
        payload: dict[str, Any] = {"state_path": str(state_path)}
        started_at = runner._isoformat(runner._utc_now())

        def make_active_turn(session: int) -> dict[str, Any]:
            return {
                "run_id": "run-late-writer",
                "turn_index": session,
                "session": session,
                "session_id": str(session),
                "total_sessions": 2,
                "harness": "claude",
                "started_at": started_at,
                "last_activity_at": started_at,
                "heartbeat_count": 0,
            }

        live_turn = runner._LiveTurn(
            payload=payload,
            event_path=event_path,
            progress=mock.Mock(),
            active_turn=make_active_turn(1),
            heartbeat_interval=3600.0,
        )
        fired: list[int] = []

        def stream_callback(stream_name: harnesses.StreamName, text: str) -> None:
            log_path = stdout_log if stream_name == "stdout" else stderr_log
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(text)
            live_turn.record_activity(stream_name, text)
            if "working" in text:
                (root / "drained").write_text("ok", encoding="utf-8")

        live_turn.start()
        with mock.patch.object(harnesses, "_ABANDONED_READER_JOIN_SECONDS", 0.5):
            result = harnesses.ClaudeAdapter(executable=str(fake_claude)).run(
                cwd=self.tmp_dir,
                plan_dir=self.tmp_dir,
                prompt="noop",
                stream_callback=stream_callback,
                on_final_result=lambda: fired.append(1),
            )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("claude text: working", result.stdout)
        self.assertEqual([], fired)

        live_turn.finish(runner._utc_now())
        later_turn = runner._LiveTurn(
            payload=payload,
            event_path=event_path,
            progress=mock.Mock(),
            active_turn=make_active_turn(2),
            heartbeat_interval=3600.0,
        )
        later_turn.start()
        self.addCleanup(later_turn.stop)

        state_before = state_path.read_bytes()
        events_before = event_path.read_bytes() if event_path.exists() else b""
        stdout_log_before = stdout_log.read_bytes()
        stderr_log_before = stderr_log.read_bytes() if stderr_log.exists() else b""

        (root / "trigger").write_text("go", encoding="utf-8")
        wait_until((root / "late" / "harness").exists)
        # Grace period for the abandoned reader to wake on the late write:
        # the quiescence guard makes it exit without side effects, so nothing
        # asserted below may change.
        time.sleep(1.0)

        self.assertEqual([], fired)
        self.assertNotIn("resurrection", result.stdout)
        self.assertEqual(state_before, state_path.read_bytes())
        self.assertEqual(
            events_before,
            event_path.read_bytes() if event_path.exists() else b"",
        )
        self.assertEqual(stdout_log_before, stdout_log.read_bytes())
        self.assertEqual(
            stderr_log_before,
            stderr_log.read_bytes() if stderr_log.exists() else b"",
        )
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(2, state["active_turn"]["session"])
        assert_marked_pids_alive(
            root / "grandchildren", label="late-writing grandchild"
        )


class HarnessProcessGroupSweepTests(unittest.TestCase):
    """Each turn owns a process group, and nothing it spawned outlives it."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp_dir = Path(tmp.name)
        self.root = self.tmp_dir / "trees"
        _parent_script, self.grandchild_script = write_process_tree_scripts(self.root)
        # Registered after the tempdir cleanup so it runs first: a survivor is
        # killed while its PID marker still exists.
        self.addCleanup(kill_marked_pids, self.root / "grandchildren")

    def _fake_harness(self, *, detached: bool, tail: tuple[str, ...]) -> Path:
        """A harness that spawns one grandchild, waits for it, then runs ``tail``.

        ``detached`` makes the grandchild call ``setsid``, taking it out of the
        turn's process group and so out of reach of ``killpg``.
        """
        path = self.tmp_dir / "fake-copilot"
        path.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env python3",
                    "import pathlib, subprocess, sys, time",
                    f"root = pathlib.Path({str(self.root)!r})",
                    "subprocess.Popen(",
                    "    [",
                    "        sys.executable,",
                    f"        {str(self.grandchild_script)!r},",
                    "        'turn',",
                    "        str(root),",
                    "    ],",
                    f"    start_new_session={detached!r},",
                    ")",
                    "while not (root / 'grandchildren' / 'turn').exists():",
                    "    time.sleep(0.01)",
                    *tail,
                    "",
                ]
            ),
            encoding="utf-8",
        )
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def _grandchild_pid(self) -> int:
        return int((self.root / "grandchildren" / "turn").read_text(encoding="utf-8"))

    def _wait_for_grandchild_on_spawn(self, _pid: int) -> None:
        """Start the turn timeout only after the synthetic tree is live."""
        wait_until(
            (self.root / "grandchildren" / "turn").exists,
            timeout=30,
        )

    def test_lingering_in_group_child_is_swept_after_a_successful_turn(
        self,
    ) -> None:
        """The port-4173 shape at the harness layer: a turn that exits 0 dirty."""
        fake_harness = self._fake_harness(
            detached=False, tail=("print('turn done', flush=True)",)
        )
        spawn_pids: list[int] = []

        result = _PythonScriptCopilotAdapter(fake_harness).run(
            cwd=self.tmp_dir,
            plan_dir=self.tmp_dir,
            prompt="noop",
            on_spawn=spawn_pids.append,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("turn done", result.stdout)
        # start_new_session made the turn its own group leader, which is what
        # gives the sweep a positive identification to enumerate.
        self.assertEqual(
            spawn_pids, [result.process_sweep["enumeration"]["expected_pgid"]]
        )
        lingering = self._grandchild_pid()
        # It never left the group, so the pgid enumeration alone finds it.
        self.assertEqual(
            [lingering], result.process_sweep["enumeration"]["pgid_members"]
        )
        self.assertEqual([lingering], result.process_sweep["killed"])
        self.assertEqual([], result.process_sweep["survivors_after"])
        assert_marked_pids_exited(self.root / "grandchildren", label="lingering child")

    def test_timeout_kills_the_whole_process_group(self) -> None:
        fake_harness = self._fake_harness(detached=False, tail=("time.sleep(60)",))
        spawn_pids: list[int] = []

        def on_spawn(pid: int) -> None:
            spawn_pids.append(pid)
            self._wait_for_grandchild_on_spawn(pid)

        result = _PythonScriptCopilotAdapter(fake_harness).run(
            cwd=self.tmp_dir,
            plan_dir=self.tmp_dir,
            prompt="noop",
            # Keep process startup outside the timeout: heavily parallel suite
            # runs can otherwise expire before the synthetic tree publishes
            # its PID, leaving the timeout/sweep behavior unexercised.
            timeout=2,
            on_spawn=on_spawn,
        )

        self.assertTrue(result.timed_out)
        self.assertEqual(124, result.returncode)
        assert_marked_pids_exited(
            self.root / "grandchildren", label="in-group grandchild"
        )
        # The proof that the group died rather than just the direct child: a
        # grandchild that merely outlived its parent would have observed being
        # orphaned and written its late marker before any sweep reached it.
        self.assertEqual([], list((self.root / "late").iterdir()))
        # Nothing was left for the sweep, but it still reports on the group.
        self.assertEqual(
            spawn_pids, [result.process_sweep["enumeration"]["expected_pgid"]]
        )
        self.assertEqual([], result.process_sweep["enumeration"]["pgid_members"])
        self.assertEqual([], result.process_sweep["survivors_after"])

    def test_timeout_sweeps_a_setsid_escapee_through_the_pre_kill_snapshot(
        self,
    ) -> None:
        """`killpg` cannot reach an escapee; the snapshot taken before it can.

        Once the group kill lands the escapee is reparented to pid 1 and no
        descendant walk can reach it, so a snapshot taken while the turn's root
        was still alive is the sweep's only route to the 2h20m hang class.
        """
        fake_harness = self._fake_harness(detached=True, tail=("time.sleep(60)",))

        result = _PythonScriptCopilotAdapter(fake_harness).run(
            cwd=self.tmp_dir,
            plan_dir=self.tmp_dir,
            prompt="noop",
            timeout=2,
            on_spawn=self._wait_for_grandchild_on_spawn,
        )

        self.assertTrue(result.timed_out)
        escapee = self._grandchild_pid()
        # The late marker is the proof the escapee outlived the group kill: it
        # is only written once being orphaned changes its parent PID.
        self.assertEqual(["turn"], [p.name for p in (self.root / "late").iterdir()])
        # The group kill had already emptied the group, so the snapshot -- not
        # the pgid enumeration -- is what identified the escapee.
        self.assertEqual([], result.process_sweep["enumeration"]["pgid_members"])
        self.assertEqual(
            [escapee], result.process_sweep["enumeration"]["snapshot_verified"]
        )
        self.assertEqual([escapee], result.process_sweep["killed"])
        self.assertEqual([], result.process_sweep["survivors_after"])
        assert_marked_pids_exited(
            self.root / "grandchildren", label="detached grandchild"
        )

    def test_recorded_sample_sweeps_a_setsid_escapee_of_a_clean_turn(
        self,
    ) -> None:
        """The hang sentinel's sample is the only route left on a clean exit.

        A turn that exits on its own takes no pre-kill snapshot, and by then the
        escapee has been reparented to pid 1, so a descendant walk cannot reach
        it either. A tree recorded while the turn was still alive can.
        """
        gate = self.tmp_dir / "sampled"
        fake_harness = self._fake_harness(
            detached=True,
            tail=(
                f"gate = pathlib.Path({str(gate)!r})",
                "while not gate.exists():",
                "    time.sleep(0.01)",
                "print('turn done', flush=True)",
            ),
        )
        # The cache outlives this test; leaving a sample behind would let a
        # later sweep union a tree that has nothing to do with it.
        self.addCleanup(process_tree.record_process_tree, 0, ())
        turn_pids: list[int] = []

        def on_spawn(pid: int) -> None:
            turn_pids.append(pid)
            wait_until((self.root / "grandchildren" / "turn").exists, timeout=30)
            process_tree.record_process_tree(pid, process_tree.sample_process_tree(pid))
            gate.write_text("go", encoding="utf-8")

        result = _PythonScriptCopilotAdapter(fake_harness).run(
            cwd=self.tmp_dir,
            plan_dir=self.tmp_dir,
            prompt="noop",
            on_spawn=on_spawn,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("turn done", result.stdout)
        escapee = self._grandchild_pid()
        self.assertEqual([], result.process_sweep["enumeration"]["pgid_members"])
        self.assertEqual(
            [escapee], result.process_sweep["enumeration"]["snapshot_verified"]
        )
        self.assertEqual([escapee], result.process_sweep["killed"])
        self.assertEqual([], result.process_sweep["survivors_after"])
        # Keyed by the root the sample was taken for, so no other turn's sweep
        # can union it -- and consumed by that sweep, so a later turn handed the
        # same pid cannot union it either.
        self.assertEqual((), process_tree.last_process_tree(escapee))
        self.assertEqual((), process_tree.last_process_tree(turn_pids[0]))
        assert_marked_pids_exited(self.root / "grandchildren", label="sampled escapee")

    def _interrupting_popen(self, ready: Path):
        """Patch ``Popen`` so the first ``wait`` raises once ``ready`` exists.

        Reproduces a Ctrl-C landing mid-turn: the caller is inside
        ``process.wait`` when the interrupt arrives, so the harness and
        everything it spawned are still running.
        """
        real_popen = subprocess.Popen
        raised = threading.Event()

        def popen(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            original_wait = process.wait

            def wait(timeout=None):
                if not raised.is_set():
                    raised.set()
                    wait_until(ready.exists, timeout=30)
                    raise KeyboardInterrupt
                return original_wait(timeout=timeout)

            process.wait = wait
            return process

        return mock.patch.object(subprocess, "Popen", popen)

    def test_interrupt_path_stays_intact_and_sweeps_the_group(self) -> None:
        """Explicit signalling is unchanged; the sweep takes what it left."""
        fake_harness = self._fake_harness(detached=False, tail=("time.sleep(60)",))
        spawn_pids: list[int] = []
        ready = self.root / "grandchildren" / "turn"

        with self._interrupting_popen(ready):
            result = _PythonScriptCopilotAdapter(fake_harness).run(
                cwd=self.tmp_dir,
                plan_dir=self.tmp_dir,
                prompt="noop",
                on_spawn=spawn_pids.append,
            )

        # The interrupt contract: a 130 result flagged interrupted, not a
        # timeout and not a raised KeyboardInterrupt.
        self.assertEqual(130, result.returncode)
        self.assertTrue(result.interrupted)
        self.assertFalse(result.timed_out)
        with self.assertRaises(ProcessLookupError):
            os.kill(spawn_pids[0], 0)
        self.assertEqual(
            spawn_pids, [result.process_sweep["enumeration"]["expected_pgid"]]
        )
        # terminate() only signalled the direct child, so the grandchild was
        # still in the group when the sweep enumerated it.
        lingering = self._grandchild_pid()
        self.assertEqual(
            [lingering], result.process_sweep["enumeration"]["pgid_members"]
        )
        self.assertEqual([lingering], result.process_sweep["killed"])
        self.assertEqual([], result.process_sweep["survivors_after"])
        assert_marked_pids_exited(
            self.root / "grandchildren", label="in-group grandchild"
        )


class _StubWaitProcess:
    """A process that never exits and whose wait always expires after a sleep.

    Lets the two deadlines be compared at a tick that both have already passed,
    which no real timing can be relied on to produce.
    """

    def __init__(self, wait_seconds: float) -> None:
        self.wait_seconds = wait_seconds
        self.pid = os.getpid()

    def poll(self) -> None:
        return None

    def wait(self, timeout: float | None = None) -> int:
        time.sleep(self.wait_seconds)
        raise subprocess.TimeoutExpired(cmd="stub", timeout=timeout)


class HarnessAbortDeadlineTests(unittest.TestCase):
    """The wait loop's deadline arithmetic, with no real harness involved."""

    def test_unarmed_signal_has_no_expiry(self) -> None:
        signal_ = harnesses.AbortSignal()
        self.assertFalse(signal_.armed)
        self.assertIsNone(signal_.expires_at(time.monotonic()))

    def test_armed_signal_carries_its_grace_window_and_reason(self) -> None:
        signal_ = harnesses.AbortSignal()
        before = time.monotonic()
        signal_.arm("supervisor stop", grace_seconds=5.0)

        self.assertTrue(signal_.armed)
        self.assertEqual("supervisor stop", signal_.reason)
        self.assertGreaterEqual(signal_.expires_at(before), before + 5.0)

    def test_armed_without_a_deadline_is_due_at_once(self) -> None:
        signal_ = harnesses.AbortSignal()
        signal_.event.set()

        now = time.monotonic()
        self.assertEqual(now, signal_.expires_at(now))

    def test_earlier_timeout_wins_when_both_deadlines_have_passed(self) -> None:
        abort = harnesses.AbortSignal()
        abort.arm("supervisor stop", grace_seconds=0.1)

        expired = harnesses._wait_for_turn_exit(
            _StubWaitProcess(0.2), timeout=0.05, abort=abort
        )

        self.assertEqual("timeout", expired)

    def test_earlier_abort_wins_when_both_deadlines_have_passed(self) -> None:
        abort = harnesses.AbortSignal()
        abort.arm("supervisor stop", grace_seconds=0.05)

        expired = harnesses._wait_for_turn_exit(
            _StubWaitProcess(0.2), timeout=0.1, abort=abort
        )

        self.assertEqual("abort", expired)


class HarnessAbortSignalTests(unittest.TestCase):
    """An armed abort kills the turn's whole tree and is told from a timeout."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp_dir = Path(tmp.name)
        self.root = self.tmp_dir / "trees"
        _parent_script, self.grandchild_script = write_process_tree_scripts(self.root)
        self.addCleanup(kill_marked_pids, self.root / "grandchildren")
        self.started = self.tmp_dir / "harness-started"

    def _fake_harness(self, *, spawns: bool, detached: bool, tail: str) -> Path:
        """A harness that optionally fans out, publishes liveness, then waits."""
        spawn_lines = (
            [
                "subprocess.Popen(",
                "    [",
                "        sys.executable,",
                f"        {str(self.grandchild_script)!r},",
                "        'turn',",
                "        str(root),",
                "    ],",
                f"    start_new_session={detached!r},",
                ")",
                "while not (root / 'grandchildren' / 'turn').exists():",
                "    time.sleep(0.01)",
            ]
            if spawns
            else []
        )
        path = self.tmp_dir / "fake-copilot"
        path.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env python3",
                    "import pathlib, subprocess, sys, time",
                    f"root = pathlib.Path({str(self.root)!r})",
                    *spawn_lines,
                    f"pathlib.Path({str(self.started)!r}).write_text('go')",
                    tail,
                    "",
                ]
            ),
            encoding="utf-8",
        )
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def _arm_once_live(self, abort: harnesses.AbortSignal, *, grace: float):
        """Start the grace window only once the turn's tree is up.

        Mirrors the timeout tests: process startup under a heavily parallel
        suite run can otherwise eat the whole window.
        """

        def on_spawn(_pid: int) -> None:
            wait_until(self.started.exists, timeout=30)
            abort.arm("supervisor abort", grace_seconds=grace)

        return on_spawn

    def _grandchild_pid(self) -> int:
        return int((self.root / "grandchildren" / "turn").read_text(encoding="utf-8"))

    def test_abort_deadline_kills_the_turn_and_empties_its_group(self) -> None:
        fake_harness = self._fake_harness(
            spawns=False, detached=False, tail="time.sleep(60)"
        )
        abort = harnesses.AbortSignal()
        spawn_pids: list[int] = []
        arm = self._arm_once_live(abort, grace=0.5)

        def on_spawn(pid: int) -> None:
            spawn_pids.append(pid)
            arm(pid)

        result = _PythonScriptCopilotAdapter(fake_harness).run(
            cwd=self.tmp_dir,
            plan_dir=self.tmp_dir,
            prompt="noop",
            on_spawn=on_spawn,
            abort=abort,
        )

        self.assertTrue(result.aborted)
        self.assertFalse(result.timed_out)
        # 137, not the timeout path's 124: readable without the flags.
        self.assertEqual(137, result.returncode)
        with self.assertRaises(ProcessLookupError):
            os.killpg(spawn_pids[0], 0)

    def test_turn_that_exits_inside_the_window_is_never_signalled(self) -> None:
        fake_harness = self._fake_harness(
            spawns=False, detached=False, tail="print('turn done', flush=True)"
        )
        abort = harnesses.AbortSignal()
        abort.arm("supervisor abort", grace_seconds=60.0)

        result = _PythonScriptCopilotAdapter(fake_harness).run(
            cwd=self.tmp_dir,
            plan_dir=self.tmp_dir,
            prompt="noop",
            abort=abort,
        )

        self.assertFalse(result.aborted)
        self.assertFalse(result.timed_out)
        # A killed turn reports -SIGKILL, so a clean 0 is the proof that the
        # armed-but-unexpired signal never reached the process.
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("turn done", result.stdout)

    def test_abort_kills_the_whole_process_group_and_reports_the_sweep(
        self,
    ) -> None:
        fake_harness = self._fake_harness(
            spawns=True, detached=False, tail="time.sleep(60)"
        )
        abort = harnesses.AbortSignal()
        spawn_pids: list[int] = []
        arm = self._arm_once_live(abort, grace=0.5)

        def on_spawn(pid: int) -> None:
            spawn_pids.append(pid)
            arm(pid)

        result = _PythonScriptCopilotAdapter(fake_harness).run(
            cwd=self.tmp_dir,
            plan_dir=self.tmp_dir,
            prompt="noop",
            on_spawn=on_spawn,
            abort=abort,
        )

        self.assertTrue(result.aborted)
        assert_marked_pids_exited(
            self.root / "grandchildren", label="in-group grandchild"
        )
        # The group died as one: a grandchild that merely outlived its parent
        # would have seen itself orphaned and written its late marker first.
        self.assertEqual([], list((self.root / "late").iterdir()))
        self.assertEqual(
            spawn_pids, [result.process_sweep["enumeration"]["expected_pgid"]]
        )
        self.assertEqual([], result.process_sweep["enumeration"]["pgid_members"])
        self.assertEqual([], result.process_sweep["survivors_after"])

    def test_abort_sweeps_a_setsid_escapee_through_the_pre_kill_snapshot(
        self,
    ) -> None:
        """The abort path takes the same pre-kill snapshot the timeout path does."""
        fake_harness = self._fake_harness(
            spawns=True, detached=True, tail="time.sleep(60)"
        )
        abort = harnesses.AbortSignal()

        result = _PythonScriptCopilotAdapter(fake_harness).run(
            cwd=self.tmp_dir,
            plan_dir=self.tmp_dir,
            prompt="noop",
            on_spawn=self._arm_once_live(abort, grace=0.5),
            abort=abort,
        )

        self.assertTrue(result.aborted)
        escapee = self._grandchild_pid()
        # killpg emptied the group, so only a snapshot taken while the turn was
        # still alive could name the escapee.
        self.assertEqual([], result.process_sweep["enumeration"]["pgid_members"])
        self.assertEqual(
            [escapee], result.process_sweep["enumeration"]["snapshot_verified"]
        )
        self.assertEqual([escapee], result.process_sweep["killed"])
        self.assertEqual([], result.process_sweep["survivors_after"])
        assert_marked_pids_exited(
            self.root / "grandchildren", label="detached grandchild"
        )

    def test_timeout_first_sets_only_timed_out(self) -> None:
        fake_harness = self._fake_harness(
            spawns=False, detached=False, tail="time.sleep(60)"
        )
        abort = harnesses.AbortSignal()

        result = _PythonScriptCopilotAdapter(fake_harness).run(
            cwd=self.tmp_dir,
            plan_dir=self.tmp_dir,
            prompt="noop",
            timeout=2,
            on_spawn=self._arm_once_live(abort, grace=60.0),
            abort=abort,
        )

        self.assertTrue(result.timed_out)
        self.assertFalse(result.aborted)
        self.assertEqual(124, result.returncode)

    def test_abort_first_sets_only_aborted(self) -> None:
        fake_harness = self._fake_harness(
            spawns=False, detached=False, tail="time.sleep(60)"
        )
        abort = harnesses.AbortSignal()

        result = _PythonScriptCopilotAdapter(fake_harness).run(
            cwd=self.tmp_dir,
            plan_dir=self.tmp_dir,
            prompt="noop",
            timeout=60,
            on_spawn=self._arm_once_live(abort, grace=0.5),
            abort=abort,
        )

        self.assertTrue(result.aborted)
        self.assertFalse(result.timed_out)
        self.assertEqual(137, result.returncode)


class LiveTurnHangSentinelIntegrationTests(unittest.TestCase):
    """_LiveTurn + fake sampler end-to-end persistence of hang suspicion."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp_dir = Path(tmp.name)
        self.state_path = self.tmp_dir / "state.json"
        self.event_path = self.tmp_dir / "events.jsonl"

    @staticmethod
    def tree(cpu_seconds: float) -> list[dict[str, Any]]:
        return [
            {
                "pid": 4242,
                "ppid": 1,
                "state": "S",
                "cpu_seconds": cpu_seconds,
                "command": "claude --print",
            }
        ]

    def test_suspicion_persists_and_clears_on_activity(self) -> None:
        samples = [self.tree(9.0) for _ in range(4)]

        def sample_tree(pid: int):
            return samples.pop(0)

        sentinel = runner._HangSentinel(
            sample_tree=sample_tree,
            monotonic=time.monotonic,
            trigger_idle_seconds=120.0,
            confirm_samples=2,
            cpu_epsilon_seconds=0.5,
            result_grace_seconds=45.0,
        )
        started_at = runner._isoformat(runner._utc_now() - timedelta(seconds=600))
        active_turn: dict[str, Any] = {
            "run_id": "run-hang",
            "turn_index": 1,
            "session": 3,
            "session_id": "3",
            "total_sessions": 3,
            "harness": "claude",
            "artifact_dir": str(self.tmp_dir),
            "stdout_path": str(self.tmp_dir / "stdout.log"),
            "stderr_path": str(self.tmp_dir / "stderr.log"),
            "last_message_path": str(self.tmp_dir / "last-message.txt"),
            "result_path": str(self.tmp_dir / "result.json"),
            "started_at": started_at,
            "last_activity_at": started_at,
            "heartbeat_count": 0,
        }
        live_turn = runner._LiveTurn(
            payload={"state_path": str(self.state_path)},
            event_path=self.event_path,
            progress=mock.Mock(),
            active_turn=active_turn,
            hang_sentinel=sentinel,
        )
        live_turn.note_harness_pid(4242)

        # Baseline, first confirm, second confirm: suspicion lands on the
        # third heartbeat tick.
        for _ in range(3):
            live_turn._record_heartbeat()

        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        block = state["active_turn"]["hang_suspected"]
        self.assertEqual(runner.HANG_REASON_ZERO_CPU, block["reason"])
        self.assertEqual(4242, block["harness_pid"])
        self.assertEqual(2, block["confirm_samples"])
        self.assertGreaterEqual(block["idle_seconds"], 120.0)
        self.assertEqual(4242, block["process_tree"][0]["pid"])

        records = read_jsonl(self.event_path)
        suspected = [
            record
            for record in records
            if record["name"] == events.TURN_HANG_SUSPECTED_EVENT
        ]
        self.assertEqual(1, len(suspected))
        self.assertEqual(
            runner.HANG_REASON_ZERO_CPU,
            suspected[0]["payload"]["hang_suspected"]["reason"],
        )

        # A later heartbeat refreshes in place: no duplicate suspected event,
        # and the heartbeat payload carries the block for the status tailer.
        live_turn._record_heartbeat()
        records = read_jsonl(self.event_path)
        suspected = [
            record
            for record in records
            if record["name"] == events.TURN_HANG_SUSPECTED_EVENT
        ]
        self.assertEqual(1, len(suspected))
        heartbeats = [
            record
            for record in records
            if record["name"] == events.TURN_HEARTBEAT_EVENT
        ]
        self.assertEqual(
            runner.HANG_REASON_ZERO_CPU,
            heartbeats[-1]["payload"]["hang_suspected"]["reason"],
        )

        live_turn.record_activity("stdout", "resumed output")

        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertNotIn("hang_suspected", state["active_turn"])
        records = read_jsonl(self.event_path)
        cleared = [
            record
            for record in records
            if record["name"] == events.TURN_HANG_CLEARED_EVENT
        ]
        self.assertEqual(1, len(cleared))
        self.assertEqual(
            runner.HANG_REASON_ZERO_CPU,
            cleared[0]["payload"]["hang_suspected"]["reason"],
        )

    def _live_turn(
        self, sample_tree, *, result_grace_seconds: float = 45.0
    ) -> runner._LiveTurn:
        sentinel = runner._HangSentinel(
            sample_tree=sample_tree,
            monotonic=time.monotonic,
            trigger_idle_seconds=120.0,
            confirm_samples=2,
            cpu_epsilon_seconds=0.5,
            result_grace_seconds=result_grace_seconds,
        )
        started_at = runner._isoformat(runner._utc_now() - timedelta(seconds=600))
        active_turn: dict[str, Any] = {
            "run_id": "run-hang",
            "turn_index": 1,
            "session": 3,
            "session_id": "3",
            "total_sessions": 3,
            "harness": "claude",
            "artifact_dir": str(self.tmp_dir),
            "stdout_path": str(self.tmp_dir / "stdout.log"),
            "stderr_path": str(self.tmp_dir / "stderr.log"),
            "last_message_path": str(self.tmp_dir / "last-message.txt"),
            "result_path": str(self.tmp_dir / "result.json"),
            "started_at": started_at,
            "last_activity_at": started_at,
            "heartbeat_count": 0,
        }
        return runner._LiveTurn(
            payload={"state_path": str(self.state_path)},
            event_path=self.event_path,
            progress=mock.Mock(),
            active_turn=active_turn,
            hang_sentinel=sentinel,
        )

    def test_recovery_report_is_live_in_status_heartbeat_and_hang_event(self) -> None:
        tree = [
            {
                "pid": 4242,
                "ppid": 1,
                "state": "S",
                "cpu_seconds": 9.0,
                "birth": "root",
                "command": "claude --print",
            },
            {
                "pid": 5001,
                "ppid": 4242,
                "state": "S",
                "cpu_seconds": 1.0,
                "birth": "leaf",
                "command": "eza -la",
            },
        ]
        live_turn = self._live_turn(
            lambda pid: [dict(entry) for entry in tree],
            result_grace_seconds=0.0,
        )
        live_turn.note_harness_pid(4242)
        live_turn.note_final_result()
        report = {
            "policy": process_tree.POST_RESULT_RECOVERY_POLICY,
            "attempted_at": "now",
            "signal": "SIGTERM",
            "candidate_pids": [5001],
            "signaled_pids": [],
            "already_gone_pids": [],
            "identity_mismatches": [],
            "signal_failures": [],
        }
        with (
            mock.patch.object(
                process_tree,
                "prepare_verified_leaf_recovery",
                return_value=(report, (5001,)),
            ),
            mock.patch.object(
                process_tree,
                "signal_verified_leaf_recovery",
                return_value={**report, "signaled_pids": [5001]},
            ),
        ):
            live_turn._record_heartbeat()
            live_turn._record_heartbeat()
            heartbeat = live_turn._record_heartbeat()

        recovery = heartbeat["hang_suspected"]["recovery"]
        self.assertEqual([5001], recovery["signaled_pids"])
        state_payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(
            recovery,
            state_payload["active_turn"]["hang_suspected"]["recovery"],
        )
        suspected = [
            record
            for record in read_jsonl(self.event_path)
            if record["name"] == events.TURN_HANG_SUSPECTED_EVENT
        ]
        self.assertEqual(
            recovery,
            suspected[0]["payload"]["hang_suspected"]["recovery"],
        )

        # A harness that exits without another output chunk still clears the
        # live episode at turn finalization, preserving the recovery evidence.
        live_turn.finish(runner._utc_now())
        cleared = [
            record
            for record in read_jsonl(self.event_path)
            if record["name"] == events.TURN_HANG_CLEARED_EVENT
        ]
        self.assertEqual(1, len(cleared))
        self.assertEqual(
            recovery,
            cleared[0]["payload"]["hang_suspected"]["recovery"],
        )

    def test_cpu_progress_publishes_without_suspicion_and_clears(self) -> None:
        # A worker burning CPU behind a buffering pipe: silent past the
        # trigger, but its tree keeps advancing.
        samples = [self.tree(cpu) for cpu in (9.0, 12.0, 15.0, 18.0)]
        live_turn = self._live_turn(lambda pid: samples.pop(0))
        live_turn.note_harness_pid(4242)

        for _ in range(3):
            live_turn._record_heartbeat()

        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        active_turn = state["active_turn"]
        self.assertNotIn("hang_suspected", active_turn)
        progress = active_turn["hang_cpu_progress"]
        self.assertTrue(progress["progressing"])
        self.assertEqual(4242, progress["harness_pid"])
        self.assertEqual(3.0, progress["cpu_delta_seconds"])
        self.assertEqual(0, progress["confirm_samples"])
        self.assertIn("last_checked_at", progress)

        records = read_jsonl(self.event_path)
        self.assertEqual(
            [],
            [
                record
                for record in records
                if record["name"] == events.TURN_HANG_SUSPECTED_EVENT
            ],
        )
        heartbeats = [
            record
            for record in records
            if record["name"] == events.TURN_HEARTBEAT_EVENT
        ]
        self.assertTrue(
            heartbeats[-1]["payload"]["hang_cpu_progress"]["progressing"],
        )
        self.assertNotIn("hang_suspected", heartbeats[-1]["payload"])

        # Resumed output resets the CPU baseline, so the published sample is
        # no longer fresh and must not linger.
        live_turn.record_activity("stdout", "resumed output")
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertNotIn("hang_cpu_progress", state["active_turn"])

    def test_churning_tree_persists_a_non_decreasing_cpu_total(self) -> None:
        """ASI-58 end to end: worker churn must not persist a hang suspicion."""

        def proc(pid: int, cpu_seconds: float, birth: str) -> dict[str, Any]:
            return {
                "pid": pid,
                "ppid": 1 if pid == 4242 else 4242,
                "state": "S",
                "cpu_seconds": cpu_seconds,
                "birth": birth,
                "command": "python3 -m worker",
            }

        samples = [
            [proc(4242, 9.0, "root"), proc(5001, 6.0, "w1")],
            [proc(4242, 9.2, "root"), proc(5002, 4.0, "w2")],
            [proc(4242, 9.4, "root")],
            [proc(4242, 9.6, "root"), proc(5003, 5.0, "w3")],
        ]
        live_turn = self._live_turn(lambda pid: samples.pop(0))
        live_turn.note_harness_pid(4242)

        for _ in range(4):
            live_turn._record_heartbeat()

        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        active_turn = state["active_turn"]
        self.assertNotIn("hang_suspected", active_turn)
        progress = active_turn["hang_cpu_progress"]
        self.assertTrue(progress["progressing"])
        self.assertEqual(4, progress["tracked_processes"])
        self.assertEqual(2, progress["sampled_processes"])

        records = read_jsonl(self.event_path)
        self.assertEqual(
            [],
            [
                record
                for record in records
                if record["name"] == events.TURN_HANG_SUSPECTED_EVENT
            ],
        )
        totals = [
            record["payload"]["hang_cpu_progress"]["cpu_total_seconds"]
            for record in records
            if record["name"] == events.TURN_HEARTBEAT_EVENT
        ]
        self.assertEqual(4, len(totals))
        self.assertEqual(sorted(totals), totals)

    def test_cpu_progress_absent_while_the_turn_is_not_sampled(self) -> None:
        def never_sampled(pid: int):  # pragma: no cover - guard
            raise AssertionError("a busy turn must not be sampled")

        live_turn = self._live_turn(never_sampled)
        # No harness pid: the sentinel cannot sample, so it must publish
        # nothing rather than a stale or invented reading.
        live_turn._record_heartbeat()

        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertNotIn("hang_cpu_progress", state["active_turn"])
        heartbeats = [
            record
            for record in read_jsonl(self.event_path)
            if record["name"] == events.TURN_HEARTBEAT_EVENT
        ]
        self.assertNotIn("hang_cpu_progress", heartbeats[-1]["payload"])


class AuditAccessProfileTests(unittest.TestCase):
    """The read-only profile expressed in argv, per adapter.

    Real sandbox enforcement lives inside the installed harnesses and cannot be
    exercised offline. What is testable — and what actually decides whether the
    boundary holds — is that every audit invocation asks for the restricted
    mode, names no writable root, and carries no widening flag.
    """

    #: Flags that widen a harness past its default sandbox, by any spelling.
    BYPASS_TOKENS = (
        "--dangerously-bypass-approvals-and-sandbox",
        "--allow-all-tools",
        "--allow-all-paths",
        "bypassPermissions",
        "workspace-write",
        "dontAsk",
    )

    def _audit_argv(self, name: str, read_roots: tuple[Path, ...]) -> list[str]:
        adapter = harnesses.get_adapter(name)
        # Codex states the audit profile over the app-server protocol now, so
        # its *argv* boundary is the fallback transport's argv; the protocol
        # side is asserted separately against the turn plan.
        builder = getattr(adapter, "build_exec_argv", adapter.build_argv)
        return builder(
            cwd=Path("/audit"),
            plan_dir=None,
            prompt="audit",
            last_message_path=Path("/audit/last.txt"),
            access=harnesses.audit_read_only_access(read_roots),
        )

    def _audit_access_text(self, name: str, read_roots: tuple[Path, ...]) -> str:
        """Everything that describes one audit turn's access, on either transport.

        Argv stopped being the whole story once a harness could be driven over
        a protocol, so a bypass check that read argv alone would pass
        vacuously for codex. This renders argv *and* the turn plan, which is
        where the sandbox, the approval policy, and the writable roots now
        live.
        """
        adapter = harnesses.get_adapter(name)
        invocation = adapter.build_invocation(
            cwd=Path("/audit"),
            plan_dir=None,
            prompt="audit",
            last_message_path=Path("/audit/last.txt"),
            access=harnesses.audit_read_only_access(read_roots),
        )
        rendered = "\n".join(invocation.argv)
        if invocation.turn_plan is not None:
            rendered += "\n" + json.dumps(
                invocation.turn_plan.to_payload(), sort_keys=True
            )
        return rendered

    def _granted_dirs(self, argv: list[str]) -> list[str]:
        return [
            argv[index + 1] for index, token in enumerate(argv) if token == "--add-dir"
        ]

    def test_worker_argv_grants_exactly_the_access_it_always_did(self) -> None:
        # Claude's argv gained ``--input-format stream-json`` and lost its
        # prompt when the turn became a conversation, so this is no longer a
        # byte-for-byte identity test for that harness. Every access-bearing
        # token is unchanged, which is what the assertion is actually for.
        expected = {
            "claude": [
                "claude",
                "--print",
                "--verbose",
                "--output-format",
                "stream-json",
                "--include-partial-messages",
                "--input-format",
                "stream-json",
                "--add-dir",
                "/plan",
                "--permission-mode",
                "dontAsk",
            ],
            "copilot": [
                "copilot",
                "-C",
                "/work",
                "--add-dir",
                "/plan",
                "--no-ask-user",
                "--silent",
                "-p",
                "prompt",
            ],
        }

        for name, argv in expected.items():
            with self.subTest(harness=name):
                self.assertEqual(
                    argv,
                    harnesses.get_adapter(name).build_argv(
                        cwd=Path("/work"),
                        plan_dir=Path("/plan"),
                        prompt="prompt",
                        last_message_path=Path("/work/last.txt"),
                    ),
                )

    def test_claude_worker_prompt_never_reaches_argv(self) -> None:
        adapter = harnesses.get_adapter("claude")
        invocation = adapter.build_invocation(
            cwd=Path("/work"),
            plan_dir=Path("/plan"),
            prompt="prompt",
            last_message_path=Path("/work/last.txt"),
        )

        # Under streaming input the prompt is the conversation's first stdin
        # message, so it must not appear in a process listing at all.
        self.assertNotIn("prompt", invocation.argv)
        self.assertEqual(harnesses.TRANSPORT_STREAM_JSON, invocation.transport)

    def test_audit_argv_never_carries_a_bypass_flag(self) -> None:
        for name in harnesses.HARNESS_NAMES:
            with self.subTest(harness=name):
                argv = self._audit_argv(name, (Path("/evidence"),))
                described = self._audit_access_text(name, (Path("/evidence"),))

                for token in self.BYPASS_TOKENS:
                    self.assertNotIn(token, argv)
                    self.assertNotIn(token, described)

    def test_codex_worker_turns_run_over_the_app_server(self) -> None:
        adapter = harnesses.get_adapter("codex")

        invocation = adapter.build_invocation(
            cwd=Path("/work"),
            plan_dir=Path("/plan"),
            prompt="prompt",
            last_message_path=Path("/work/last.txt"),
        )

        # The prompt is no longer a command-line argument at all, so it no
        # longer appears in any process listing.
        self.assertEqual(["codex", "app-server"], invocation.argv)
        self.assertEqual(harnesses.TRANSPORT_APP_SERVER, invocation.transport)
        self.assertIsNotNone(invocation.turn_plan)
        assert invocation.turn_plan is not None
        plan = invocation.turn_plan.to_payload()
        self.assertEqual(
            {
                "cwd": "/work",
                "approvalPolicy": "never",
                "sandbox": "workspace-write",
                "config": {"sandbox_workspace_write": {"writable_roots": ["/plan"]}},
            },
            plan["thread_params"],
        )
        self.assertEqual("workspaceWrite", plan["expected_sandbox"])

    def test_codex_worker_argv_still_carries_the_legacy_fallback_turn(self) -> None:
        self.assertEqual(
            [
                "codex",
                "exec",
                "-C",
                "/work",
                "--add-dir",
                "/plan",
                "-s",
                "workspace-write",
                "-o",
                "/work/last.txt",
                "prompt",
            ],
            harnesses.CodexAdapter().build_exec_argv(
                cwd=Path("/work"),
                plan_dir=Path("/plan"),
                prompt="prompt",
                last_message_path=Path("/work/last.txt"),
            ),
        )

    def test_codex_unsafe_bypass_reaches_the_app_server_as_full_access(self) -> None:
        invocation = harnesses.get_adapter("codex").build_invocation(
            cwd=Path("/work"),
            plan_dir=Path("/plan"),
            prompt="prompt",
            unsafe_bypass=True,
            last_message_path=Path("/work/last.txt"),
        )

        assert invocation.turn_plan is not None
        plan = invocation.turn_plan.to_payload()
        self.assertEqual("danger-full-access", plan["thread_params"]["sandbox"])
        self.assertEqual("dangerFullAccess", plan["expected_sandbox"])

    def test_codex_audits_run_in_its_read_only_sandbox(self) -> None:
        argv = self._audit_argv("codex", (Path("/evidence"), Path("/skills")))
        invocation = harnesses.get_adapter("codex").build_invocation(
            cwd=Path("/audit"),
            plan_dir=None,
            prompt="audit",
            last_message_path=Path("/audit/last.txt"),
            access=harnesses.audit_read_only_access(
                (Path("/evidence"), Path("/skills"))
            ),
        )

        assert invocation.turn_plan is not None
        plan = invocation.turn_plan.to_payload()
        self.assertEqual("read-only", plan["thread_params"]["sandbox"])
        self.assertEqual("readOnly", plan["expected_sandbox"])
        # No writable root of any kind: the read-only sandbox already reads
        # anywhere, so the audit's read roots need no grant at all.
        self.assertNotIn("config", plan["thread_params"])
        # The fallback transport states the same profile the old way.
        self.assertEqual(["-s", "read-only"], argv[4:6])
        self.assertIn("--skip-git-repo-check", argv)
        # For codex ``--add-dir`` grants *write* access, so read roots must not
        # be passed through it.
        self.assertNotIn("--add-dir", argv)

    def test_claude_audits_expose_only_read_and_search_tools(self) -> None:
        argv = self._audit_argv("claude", (Path("/evidence"), Path("/skills")))

        mode = argv.index("--permission-mode")
        self.assertEqual(["--permission-mode", "plan"], argv[mode : mode + 2])
        self.assertEqual(
            ["--tools", "Read,Glob,Grep", "--allowed-tools", "Read,Glob,Grep"],
            argv[mode + 2 : mode + 6],
        )
        self.assertEqual(
            [
                "Bash",
                "Edit",
                "Write",
                "NotebookEdit",
                "WebFetch",
                "WebSearch",
                "Task",
                "TodoWrite",
            ],
            argv[argv.index("--disallowed-tools") + 1].split(","),
        )
        for flag in (
            "--strict-mcp-config",
            "--no-session-persistence",
            "--disable-slash-commands",
        ):
            self.assertIn(flag, argv)
        self.assertEqual(["/evidence", "/skills"], self._granted_dirs(argv))

    def test_copilot_audits_deny_write_shell_and_url_access(self) -> None:
        argv = self._audit_argv("copilot", (Path("/evidence"), Path("/skills")))

        self.assertIn("--available-tools=view,rg,glob", argv)
        for denied in ("write", "shell", "url"):
            self.assertIn(f"--deny-tool={denied}", argv)
        for flag in (
            "--disable-builtin-mcps",
            "--no-custom-instructions",
            "--no-remote",
            "--no-remote-export",
            "--disallow-temp-dir",
        ):
            self.assertIn(flag, argv)
        self.assertEqual(["/evidence", "/skills"], self._granted_dirs(argv))

    def test_an_audit_invocation_refuses_worker_grants(self) -> None:
        adapter = harnesses.get_adapter("codex")
        audit = harnesses.audit_read_only_access((Path("/evidence"),))

        with self.assertRaises(harnesses.HarnessError) as bypass:
            adapter.build_invocation(
                cwd=Path("/audit"),
                plan_dir=None,
                prompt="audit",
                unsafe_bypass=True,
                last_message_path=Path("/audit/last.txt"),
                access=audit,
            )
        with self.assertRaises(harnesses.HarnessError) as writable:
            adapter.build_invocation(
                cwd=Path("/audit"),
                plan_dir=Path("/plan"),
                prompt="audit",
                last_message_path=Path("/audit/last.txt"),
                access=audit,
            )

        self.assertIn("unsafe bypass", str(bypass.exception))
        self.assertIn("writable plan_dir", str(writable.exception))

    def test_a_harness_that_cannot_enforce_the_profile_refuses_to_build(
        self,
    ) -> None:
        self.addCleanup(
            setattr, harnesses.CodexAdapter, "enforces_audit_read_only", True
        )
        harnesses.CodexAdapter.enforces_audit_read_only = False

        with self.assertRaises(harnesses.HarnessError) as refused:
            harnesses.get_adapter("codex").build_invocation(
                cwd=Path("/audit"),
                plan_dir=None,
                prompt="audit",
                last_message_path=Path("/audit/last.txt"),
                access=harnesses.audit_read_only_access((Path("/evidence"),)),
            )

        self.assertIn("cannot enforce", str(refused.exception))
        self.assertEqual(
            "profile-unenforceable",
            harnesses.audit_read_only_unavailable_reason("codex"),
        )


class HarnessTransportClassificationTests(unittest.TestCase):
    """Transport failures the classifier retries, and the ones it survives."""

    def _result(self, **overrides: Any) -> harnesses.HarnessResult:
        fields: dict[str, Any] = {
            "name": "codex",
            "argv": ["codex"],
            "returncode": 1,
            "stdout": "",
            "stderr": "",
            "last_message": "",
        }
        fields.update(overrides)
        return harnesses.HarnessResult(**fields)

    def test_connection_was_closed_unexpectedly_classifies_as_retryable_transport(
        self,
    ) -> None:
        classification = runner.classify_harness_failure(
            self._result(last_message=OBSERVED_SOCKET_CLOSURE)
        )

        self.assertEqual(events.PAUSE_REASON_TRANSPORT_FAILED, classification.reason)
        self.assertTrue(classification.retryable)
        policy = pause_policy.classify_pause(classification.reason)
        self.assertTrue(policy["recoverable"])
        self.assertFalse(policy["requires_user"])
        self.assertEqual(pause_policy.DEFAULT_MAX_RETRIES, policy["max_retries"])
        self.assertEqual(
            list(pause_policy.TRANSPORT_RETRY_DELAYS_SECONDS),
            policy["backoff_seconds"],
        )

    def test_widened_transport_wording_stays_retryable(self) -> None:
        for text in (
            "the connection was closed by the remote host",
            "Error: connection reset by peer",
            "the connection has been refused",
        ):
            with self.subTest(text=text):
                classification = runner.classify_harness_failure(
                    self._result(stderr=text)
                )
                self.assertEqual(
                    events.PAUSE_REASON_TRANSPORT_FAILED, classification.reason
                )
                self.assertTrue(classification.retryable)

    def test_unrecognized_failure_still_fails_closed(self) -> None:
        classification = runner.classify_harness_failure(
            self._result(stderr="fake codex failure", returncode=3)
        )

        self.assertEqual(events.PAUSE_REASON_NONZERO_EXIT, classification.reason)
        self.assertFalse(classification.retryable)

    def test_invalid_model_and_capacity_outrank_the_widened_transport_rule(
        self,
    ) -> None:
        invalid = runner.classify_harness_failure(
            self._result(stderr="model not found; the connection was closed")
        )
        self.assertEqual(events.PAUSE_REASON_INVALID_MODEL, invalid.reason)
        self.assertFalse(invalid.retryable)

        capacity = runner.classify_harness_failure(
            self._result(stderr="model is at capacity; the connection was closed")
        )
        self.assertEqual(harness_failures.MODEL_CAPACITY_MARKER, capacity.marker)

    def test_launch_transport_closure_is_a_degradation_not_a_failure(self) -> None:
        result = self._result(
            returncode=0,
            stderr=f"{OBSERVED_LAUNCH_CLOSURE}\nOpenAI Codex v0.147.0\n",
        )

        degradation = harness_failures.classify_harness_degradation(result)

        assert degradation is not None
        self.assertEqual(
            harness_failures.TRANSPORT_DEGRADATION_MARKER, degradation.marker
        )
        self.assertFalse(degradation.affected_turn)
        self.assertIn("Transport channel closed", degradation.evidence)

    def test_degradation_records_whether_the_turn_also_failed(self) -> None:
        degradation = harness_failures.classify_harness_degradation(
            self._result(returncode=7, stderr=OBSERVED_LAUNCH_CLOSURE)
        )

        assert degradation is not None
        self.assertTrue(degradation.affected_turn)

    def test_a_transcript_quoting_the_closure_is_not_a_degradation(self) -> None:
        transcript = f"OpenAI Codex v0.147.0\n{'session transcript ' * 400}"

        self.assertIsNone(
            harness_failures.classify_harness_degradation(
                self._result(
                    returncode=0, stderr=f"{transcript}\n{OBSERVED_LAUNCH_CLOSURE}\n"
                )
            )
        )

    def test_clean_stderr_records_no_degradation(self) -> None:
        self.assertIsNone(
            harness_failures.classify_harness_degradation(
                self._result(returncode=0, stderr="OpenAI Codex v0.147.0\n")
            )
        )


class HarnessAuthenticationClassificationTests(unittest.TestCase):
    """Credential failures reach the category no reply can clear."""

    def _result(self, **overrides: Any) -> harnesses.HarnessResult:
        fields: dict[str, Any] = {
            "name": "claude",
            "argv": ["claude"],
            "returncode": 1,
            "stdout": "",
            "stderr": "",
            "last_message": "",
        }
        fields.update(overrides)
        return harnesses.HarnessResult(**fields)

    def test_the_observed_oauth_expiry_is_auth_failed_not_a_nonzero_exit(self) -> None:
        classification = runner.classify_harness_failure(
            self._result(last_message=OBSERVED_AUTH_EXPIRY)
        )

        self.assertEqual(events.PAUSE_REASON_AUTH_FAILED, classification.reason)
        self.assertEqual("authentication", classification.marker)
        self.assertFalse(classification.retryable)

    def test_an_expired_credential_asks_for_inspection_not_a_reply(self) -> None:
        # The point of the category: an operator cannot refresh a harness's
        # OAuth session by answering a mailbox question, so the pause has to
        # escalate rather than hold the turn open waiting for one.
        classification = runner.classify_harness_failure(
            self._result(last_message=OBSERVED_AUTH_EXPIRY)
        )

        policy = pause_policy.classify_pause(classification.reason)

        self.assertFalse(policy["recoverable"])
        self.assertTrue(policy["requires_user"])
        self.assertEqual(pause_policy.USER_ACTION_INSPECT, policy["user_action"])

    def test_every_authenticate_word_form_reaches_the_same_category(self) -> None:
        for text in (
            "Failed to authenticate",
            "the request was not authenticated",
            "authenticating with the provider failed",
            "authentication failed",
            "user is unauthenticated",
        ):
            with self.subTest(text=text):
                classification = runner.classify_harness_failure(
                    self._result(stderr=text)
                )
                self.assertEqual(events.PAUSE_REASON_AUTH_FAILED, classification.reason)

    def test_oauth_and_an_expired_session_join_the_expired_token(self) -> None:
        for text in (
            "oauth token could not be refreshed",
            "your session expired",
            "token expired",
        ):
            with self.subTest(text=text):
                classification = runner.classify_harness_failure(
                    self._result(stderr=text)
                )
                self.assertEqual(events.PAUSE_REASON_AUTH_FAILED, classification.reason)

    def test_the_rules_that_outrank_authentication_still_win(self) -> None:
        # Widening the pattern must not reorder the table: the oversized-request
        # rule and the quota rule both precede it, and both describe requests an
        # auth message may legitimately mention.
        overflow = runner.classify_harness_failure(
            self._result(last_message="Prompt is too long; failed to authenticate")
        )
        self.assertEqual(harness_failures.CONTEXT_OVERFLOW_MARKER, overflow.marker)

        quota = runner.classify_harness_failure(
            self._result(stderr="usage limit reached; please authenticate again")
        )
        self.assertEqual(events.PAUSE_REASON_QUOTA_EXHAUSTED, quota.reason)


if __name__ == "__main__":
    unittest.main()
