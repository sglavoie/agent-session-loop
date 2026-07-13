"""Fake-harness end-to-end runner integration tests.

Split from tests/test_agent_session_loop.py."""

from __future__ import annotations

import argparse
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import dataclasses
from datetime import datetime, timezone
import errno
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import pty
import select
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from unittest import mock

from agent_session_loop import (
    artifacts,
    authoring,
    baseline,
    baseline_cache,
    cli,
    compact_state,
    config,
    contracts,
    control_queue,
    events,
    harnesses,
    install_verify,
    plans,
    preflight,
    prepare,
    progress_cli,
    progress_repair,
    qna,
    review,
    review_repair,
    run_control,
    runner,
    state,
    tail,
    verification,
    workbench,
    workbench_server,
)
from agent_session_loop.formatting import (
    format_duration_seconds,
    format_turn_finished_message,
    format_turn_started_message,
)
from tests.support import (
    REPO_ROOT,
    patched_env,
    write_plan,
    write_plan_with_ids,
    install_fake_codex,
    install_fake_harness,
    install_fake_amx,
    install_fake_spec,
    init_git_repo,
    git_stdout,
    read_jsonl,
    wait_until,
    FakeRunControlListener,
)


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

    def setup_fake_spec(
        self,
        tmp_dir: Path,
        *,
        mode: str = "success",
    ) -> tuple[Path, Path, dict[str, str]]:
        fake_spec = install_fake_spec(tmp_dir)
        log_path = tmp_dir / "fake-spec.jsonl"
        env = {
            "PATH": str(tmp_dir) + os.pathsep + os.environ.get("PATH", ""),
            "FAKE_SPEC_LOG": str(log_path),
        }
        if mode != "success":
            env["FAKE_SPEC_MODE"] = mode
        return fake_spec, log_path, env

    def event_index(
        self,
        records: list[dict[str, object]],
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
    ) -> tuple[int, dict[str, object], Path, Path, Path]:
        plan_dir, state_dir, config_home, fake_codex = self.setup_loop(
            statuses=statuses,
            harness_config_overrides=harness_config_overrides,
        )
        real_state_dir = state.plan_state_dir(plan_dir, state_dir)
        if listener.state_path is None:
            listener.state_path = real_state_dir / events.STATE_FILE_NAME
        if listener.events_path is None:
            listener.events_path = real_state_dir / events.EVENTS_FILE_NAME
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
            "PYTHONPATH": str(REPO_ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""),
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
    ) -> tuple[int, dict[str, object], list[dict[str, object]], Path, Path]:
        listener = FakeRunControlListener()
        calls: list[dict[str, object]] = []

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
        effort: str | None = None,
        note: str | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
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
            timeout=15,
            check=False,
        )
        return completed, json.loads(completed.stdout)

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
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, object], Path, Path, Path]:
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

        def read_state_payload(payload: dict[str, object]) -> dict[str, object]:
            return json.loads(Path(str(payload["state_path"])).read_text(encoding="utf-8"))

        def notify_started(
            payload: dict[str, object],
            persistence: object = None,
        ) -> None:
            state_payload = read_state_payload(payload)
            observed.append(("started", state_payload["active_turn"]))
            self.assertEqual(payload["run_id"], state_payload["run_id"])
            self.assertEqual("codex", state_payload["harness"])
            self.assertIsNone(state_payload["active_turn"])

        def notify_turn_started(
            payload: dict[str, object],
            active_turn: dict[str, object],
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
            payload: dict[str, object],
            persistence: object = None,
        ) -> None:
            state_payload = read_state_payload(payload)
            observed.append(("finished", state_payload["run_finished_at"]))
            self.assertEqual(payload["run_finished_at"], state_payload["run_finished_at"])
            self.assertIsNone(state_payload["active_turn"])

        with (
            mock.patch.object(
                runner,
                "_notify_agent_mux_loop_started",
                side_effect=notify_started,
            ),
            mock.patch.object(
                runner,
                "_notify_agent_mux_loop_turn_started",
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
            project_dir.mkdir()
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
                    "PATH": str(tmp_dir)
                    + os.pathsep
                    + os.environ.get("PATH", ""),
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
                timeout=15,
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

            hook_payloads = [
                json.loads(str(record["stdin"])) for record in amx_records
            ]
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
                [
                    hook_payload["source_sequence"]
                    for hook_payload in hook_payloads
                ],
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
        hook_payloads: list[dict[str, object]] = []
        finish_payloads: list[dict[str, object]] = []

        def notify_turn_started(
            payload: dict[str, object],
            active_turn: dict[str, object],
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
            payload: dict[str, object],
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
                runner,
                "_notify_agent_mux_loop_turn_started",
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
            try:
                os.close(master_fd)
            except OSError:
                pass

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
        timeout: float = 5.0,
    ) -> str:
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
        timeout: float = 15.0,
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
    ) -> dict[str, object]:
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

        self.assertIn(
            "status: verifying session 1/2 turn 1 check 2/2 compile", text
        )
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

        self.assertIn(
            "status: verifying session 1/1 turn 1 check -/3 pending", text
        )
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
                "tail_enabled": False,
                "next_harness": None,
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
                "tail_enabled": False,
                "next_harness": None,
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
        process, master_fd = self.start_run_pty(
            plan_dir=plan_dir,
            state_dir=state_dir,
            config_home=config_home,
            mode="streaming-success",
            note=note,
            fake_step_sleep="5.0",
        )

        output = self.read_run_pty_until(
            master_fd,
            "",
            lambda text: "session 1/1 started" in text,
            process=process,
        )

        def harness_started():
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

        wait_until(harness_started, timeout=3)
        process.send_signal(signal.SIGINT)
        output = self.finish_run_pty(
            process=process,
            master_fd=master_fd,
            output=output,
            timeout=10,
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
            timeout=8,
        )
        output = self.finish_run_pty(
            process=process,
            master_fd=master_fd,
            output=output,
            timeout=20,
        )

        self.assertEqual(0, process.returncode, output)
        payload = self.read_run_payload(plan_dir=plan_dir, state_dir=state_dir)
        self.assertEqual("all-complete", payload["reason"])
        self.assertEqual(
            {
                "stop_after_current": False,
                "tail_enabled": False,
                "next_harness": "copilot",
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
        self.assertTrue(
            codex_prompt.startswith(runner.TASK_PACKET_HEADING_PREFIX)
        )
        self.assertNotIn("session-run", codex_prompt)
        self.assertIn("-p", turns[1]["argv"])
        copilot_prompt = turns[1]["argv"][turns[1]["argv"].index("-p") + 1]
        self.assertTrue(
            copilot_prompt.startswith(runner.TASK_PACKET_HEADING_PREFIX)
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
            if active_turn.get("stdout_chars", 0) <= 0:
                return None
            if active_turn.get("stderr_chars", 0) <= 0:
                return None
            stdout_path = Path(active_turn["stdout_path"])
            stderr_path = Path(active_turn["stderr_path"])
            if not stdout_path.exists() or not stderr_path.exists():
                return None
            if (
                "session 1 stdout before completion"
                in stdout_path.read_text(encoding="utf-8")
                and "session 1 stderr before completion"
                in stderr_path.read_text(encoding="utf-8")
            ):
                return active_turn
            return None

        wait_until(first_turn_logs_ready, timeout=10)
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
            timeout=5,
        )
        output = self.read_run_pty_until(
            master_fd,
            output,
            lambda text: (
                "stderr session=1 turn=1 | session 1 stderr before completion" in text
            ),
            process=process,
            timeout=5,
        )
        release_session("1")
        output = self.read_run_pty_until(
            master_fd,
            output,
            lambda text: (
                "stdout session=1 turn=1 | session 1 stdout after completion" in text
            ),
            process=process,
            timeout=5,
        )

        output = self.read_run_pty_until(
            master_fd,
            output,
            lambda text: (
                "stdout session=2 turn=2 | session 2 stdout before completion" in text
            ),
            process=process,
            timeout=5,
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
            timeout=20,
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
                "tail_enabled": False,
                "next_harness": None,
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
            _fake_spec, spec_log, spec_env = self.setup_fake_spec(tmp_dir)
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
                extra_env=spec_env,
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
            spec_records = read_jsonl(spec_log)

        self.assertEqual(0, process.returncode, output)
        self.assertIn("commit 1 finished (session 1, status success,", output)
        self.assertEqual("stopped", payload["reason"])
        self.assertEqual([1], payload["completed_sessions"])
        self.assertEqual(1, len(payload["commits"]))
        self.assertTrue(payload["commits"][0]["success"])
        self.assertEqual(1, len(spec_records))
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
        listener = FakeRunControlListener(consume_on_call=1)
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
            consume_on_call=1,
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
                "tail_enabled": False,
                "next_harness": None,
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
        calls: list[dict[str, object]] = []
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
                    PYTHONPATH=str(REPO_ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""),
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
        calls: list[dict[str, object]] = []
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
                    PYTHONPATH=str(REPO_ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""),
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

    def test_external_queue_does_not_interrupt_active_harness_subprocess(self) -> None:
        """A queue command written mid-turn must not be applied until the turn ends."""

        listener = FakeRunControlListener(current_harness="codex")
        calls: list[dict[str, object]] = []
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
                    PYTHONPATH=str(REPO_ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""),
                ),
                mock.patch.object(
                    runner.run_control.RunControlListener,
                    "for_run",
                    side_effect=fake_for_run,
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
        # While the first harness call ran, the stop flag had not yet been
        # propagated from the queue to the in-memory control state. Drain
        # only happens between checkpoints.
        self.assertFalse(stop_state_during_call[0])
        # Exactly one harness call happened because the queued stop was
        # applied at the post-session checkpoint and ended the run.
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
            str(calls[0]["prompt"]).startswith(runner.TASK_PACKET_HEADING_PREFIX)
        )
        self.assertTrue(
            str(calls[1]["prompt"]).startswith(runner.TASK_PACKET_HEADING_PREFIX)
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
                "tail_enabled": False,
                "next_harness": "claude",
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
            self.run_with_mocked_harness_switch(
                harness_model="gpt-5", effort="high"
            )
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

    def test_missing_switched_harness_pauses_before_next_turn(self) -> None:
        listener = FakeRunControlListener()
        calls: list[dict[str, object]] = []

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
                [
                    record
                    for record in event_records
                    if record["name"] == "turn_started"
                ]
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
        listener = FakeRunControlListener(consume_on_call=None)

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
                "tail_enabled": False,
                "next_harness": None,
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
        listener = FakeRunControlListener(consume_on_call=1)

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
        self.assertEqual(1, listener.consume_calls)

    def test_run_control_stop_after_current_waits_for_per_session_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            repo_dir = tmp_dir / "repo"
            init_git_repo(repo_dir)
            _fake_spec, spec_log, spec_env = self.setup_fake_spec(tmp_dir)
            listener = FakeRunControlListener(consume_on_call=1)

            exit_code, payload, _plan_dir, _state_dir, _fake_codex = (
                self.run_locked_with_listener(
                    mode="success",
                    statuses=("pending", "pending"),
                    listener=listener,
                    cwd=repo_dir,
                    commit_mode=runner.COMMIT_MODE_PER_SESSION,
                    extra_env=spec_env,
                )
            )
            spec_records = read_jsonl(spec_log)

        self.assertEqual(0, exit_code)
        self.assertEqual("stopped", payload["reason"])
        self.assertEqual([1], payload["completed_sessions"])
        self.assertEqual(1, len(payload["commits"]))
        self.assertTrue(payload["commits"][0]["success"])
        self.assertEqual(1, len(spec_records))
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
        listener = FakeRunControlListener(consume_on_call=1)

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
            f"$session-run <plan-dir>/ --session 2.1 --runner-managed",
            f"$session-run <plan-dir>/session-2.1.md --runner-managed",
            f"$session-run <plan-dir>/session-3.md --runner-managed",
        )
        for prompt_template in prompts:
            with self.subTest(prompt=prompt_template):
                with tempfile.TemporaryDirectory() as tmp:
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
            _fake_spec, spec_log, spec_env = self.setup_fake_spec(tmp_dir)
            (repo_dir / "dirty.txt").write_text("dirty\n", encoding="utf-8")

            completed, payload, plan_dir, _state_dir, _fake_codex = self.run_loop(
                mode="success",
                statuses=("pending",),
                cwd=repo_dir,
                commit_mode=runner.COMMIT_MODE_PER_SESSION,
                extra_env=spec_env,
            )

            self.assertEqual(1, completed.returncode)
            self.assertFalse(payload["ok"])
            self.assertTrue(payload["paused"])
            self.assertEqual("commit-preflight", payload["pause_reason"])
            self.assertEqual("commit-preflight", payload["reason"])
            self.assertIn("git status --porcelain", payload["pause_message"])
            self.assertEqual([], payload["turns"])
            self.assertEqual([], payload["commits"])
            self.assertFalse(spec_log.exists())
            self.assertIn(
                "- **Status**: pending",
                (plan_dir / "session-1.md").read_text(encoding="utf-8"),
            )

    def test_commit_per_session_commits_after_each_completed_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            repo_dir = tmp_dir / "repo"
            init_git_repo(repo_dir)
            _fake_spec, spec_log, spec_env = self.setup_fake_spec(tmp_dir)
            initial_commit_count = int(
                git_stdout(repo_dir, "rev-list", "--count", "HEAD")
            )

            completed, payload, _plan_dir, _state_dir, _fake_codex = self.run_loop(
                mode="success",
                statuses=("pending", "pending"),
                cwd=repo_dir,
                commit_mode=runner.COMMIT_MODE_PER_SESSION,
                extra_env=spec_env,
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
            spec_records = read_jsonl(spec_log)
            self.assertEqual(2, len(spec_records))
            for record in spec_records:
                self.assertEqual(["commit", "--yes", "--no-verify"], record["argv"][1:])
                self.assertEqual(expected_cwd, record["cwd"])

            self.assertEqual(2, len(payload["commits"]))
            for expected_session, commit in enumerate(payload["commits"], start=1):
                self.assertTrue(commit["success"])
                self.assertEqual("success", commit["status_result"])
                self.assertEqual("session", commit["scope"])
                self.assertEqual(expected_session, commit["session"])
                self.assertEqual(expected_cwd, commit["cwd"])
                self.assertEqual(
                    ["spec", "commit", "--yes", "--no-verify"],
                    commit["argv"],
                )
                self.assertTrue(Path(commit["result_path"]).exists())

            event_records = read_jsonl(Path(payload["events_path"]))
            turn_1_finished = self.event_index(
                event_records, "turn_finished", session=1
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
            self.assertLess(turn_1_finished, commit_1_started)
            self.assertLess(commit_1_started, commit_1_finished)
            self.assertLess(commit_1_finished, turn_2_started)

    def test_commit_at_end_commits_once_after_all_sessions_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            repo_dir = tmp_dir / "repo"
            init_git_repo(repo_dir)
            _fake_spec, spec_log, spec_env = self.setup_fake_spec(tmp_dir)
            initial_commit_count = int(
                git_stdout(repo_dir, "rev-list", "--count", "HEAD")
            )

            completed, payload, _plan_dir, _state_dir, _fake_codex = self.run_loop(
                mode="success",
                statuses=("pending", "pending"),
                cwd=repo_dir,
                commit_mode=runner.COMMIT_MODE_END,
                extra_env=spec_env,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertTrue(payload["ok"])
            self.assertEqual("all-complete", payload["reason"])
            self.assertEqual(runner.COMMIT_MODE_END, payload["commit_mode"])
            self.assertEqual([1, 2], payload["completed_sessions"])
            self.assertEqual(1, len(payload["commits"]))
            self.assertEqual(1, len(read_jsonl(spec_log)))
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

    def test_commit_failure_pauses_with_failed_commit_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            repo_dir = tmp_dir / "repo"
            init_git_repo(repo_dir)
            _fake_spec, spec_log, spec_env = self.setup_fake_spec(
                tmp_dir,
                mode="failure",
            )
            initial_head = git_stdout(repo_dir, "rev-parse", "HEAD")

            completed, payload, plan_dir, _state_dir, _fake_codex = self.run_loop(
                mode="success",
                statuses=("pending", "pending"),
                cwd=repo_dir,
                commit_mode=runner.COMMIT_MODE_PER_SESSION,
                extra_env=spec_env,
            )

            self.assertEqual(1, completed.returncode)
            self.assertFalse(payload["ok"])
            self.assertTrue(payload["paused"])
            self.assertEqual("commit-failed", payload["pause_reason"])
            self.assertEqual([], payload["completed_sessions"])
            self.assertEqual(1, len(payload["turns"]))
            self.assertEqual(1, len(payload["commits"]))
            self.assertEqual(1, len(read_jsonl(spec_log)))
            self.assertEqual(initial_head, git_stdout(repo_dir, "rev-parse", "HEAD"))

            commit = payload["commits"][0]
            self.assertFalse(commit["success"])
            self.assertEqual("nonzero-exit", commit["status_result"])
            self.assertEqual(23, commit["returncode"])
            self.assertEqual(commit, payload["pause_details"]["commit"])
            self.assertIn(
                "- **Status**: complete",
                (plan_dir / "session-1.md").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "- **Status**: pending",
                (plan_dir / "session-2.md").read_text(encoding="utf-8"),
            )

    def test_unchanged_head_commit_pauses_with_commit_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            repo_dir = tmp_dir / "repo"
            init_git_repo(repo_dir)
            _fake_spec, spec_log, spec_env = self.setup_fake_spec(
                tmp_dir,
                mode="unchanged-head",
            )
            initial_head = git_stdout(repo_dir, "rev-parse", "HEAD")

            completed, payload, _plan_dir, _state_dir, _fake_codex = self.run_loop(
                mode="success",
                statuses=("pending",),
                cwd=repo_dir,
                commit_mode=runner.COMMIT_MODE_PER_SESSION,
                extra_env=spec_env,
            )

            self.assertEqual(1, completed.returncode)
            self.assertFalse(payload["ok"])
            self.assertTrue(payload["paused"])
            self.assertEqual("commit-failed", payload["pause_reason"])
            self.assertEqual([], payload["completed_sessions"])
            self.assertEqual(1, len(payload["commits"]))
            self.assertEqual(1, len(read_jsonl(spec_log)))

            commit = payload["commits"][0]
            self.assertFalse(commit["success"])
            self.assertEqual("unchanged-head", commit["status_result"])
            self.assertEqual(0, commit["returncode"])
            self.assertEqual(initial_head, commit["head_before"])
            self.assertEqual(initial_head, commit["head_after"])
            self.assertIsNone(commit["commit_sha"])

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
            timeout=10,
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
            "session 1/1 finished "
            "(status complete, return code 0, duration ",
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
            timeout=10,
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
            timeout=10,
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

        run_stdout, run_stderr = run_process.communicate(timeout=10)
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

        run_stdout, run_stderr = run_process.communicate(timeout=10)
        tail_stdout, tail_stderr = tail_process.communicate(timeout=10)
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
            self.assertEqual({"codex"}, {payload["harness"] for payload in heartbeat_payloads})
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
                note=None,
                allow_not_loop_ready=False,
            )
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
            )
            persistence = runner._RunPersistence()

            persistence.write_state(payload)
            initial_state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertIn("active_verification", initial_state)
            self.assertIsNone(initial_state["active_verification"])

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
                check_count=1,
                check_names=("pytest",),
                check_kinds=("command",),
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
                    status="pass",
                    duration_seconds=1.25,
                    failure_reason=None,
                    returncode=0,
                )
                live_verification.finish(
                    passed=True, passed_count=1, reason="ok"
                )

            state_payload = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertIsNone(state_payload["active_verification"])

            event_records = read_jsonl(event_path)
            event_names = [record["name"] for record in event_records]
            self.assertIn(events.VERIFICATION_STARTED_EVENT, event_names)
            self.assertIn(events.VERIFICATION_CHECK_STARTED_EVENT, event_names)
            self.assertIn(events.VERIFICATION_HEARTBEAT_EVENT, event_names)
            self.assertIn(events.VERIFICATION_CHECK_FINISHED_EVENT, event_names)
            self.assertIn(events.VERIFICATION_FINISHED_EVENT, event_names)

            started_payload = next(
                record["payload"]
                for record in event_records
                if record["name"] == events.VERIFICATION_STARTED_EVENT
            )
            self.assertEqual(1, started_payload["check_count"])
            self.assertEqual(["pytest"], started_payload["check_names"])
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

            finished_payload = next(
                record["payload"]
                for record in event_records
                if record["name"] == events.VERIFICATION_FINISHED_EVENT
            )
            self.assertTrue(finished_payload["passed"])
            self.assertEqual("ok", finished_payload["reason"])
            self.assertEqual(1, finished_payload["passed_count"])

            stderr_text = stderr.getvalue()
            timestamp = r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] "
            self.assertRegex(
                stderr_text,
                timestamp + r"verification started for session 1/2 turn 1 \(1 check\)",
            )
            self.assertRegex(
                stderr_text,
                timestamp + r"verification check 1/1 started: pytest \(command\)",
            )
            self.assertRegex(
                stderr_text,
                timestamp
                + r"verification heartbeat for session 1/2 check 1/1 pytest \(elapsed",
            )
            self.assertRegex(
                stderr_text,
                timestamp
                + r"verification check 1/1 finished: pytest \(command\) status pass",
            )
            self.assertRegex(
                stderr_text,
                timestamp
                + r"verification passed for session 1/2 \(1/1 checks passed",
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
                heartbeat_interval=60,
            )

            live_verification.start()
            after_start_state = json.loads(
                state_path.read_text(encoding="utf-8")
            )["active_verification"]
            self.assertEqual(2, after_start_state["check_count"])
            self.assertEqual(
                ["file_exists", "regex_in_file"], after_start_state["check_kinds"]
            )
            self.assertIsNone(after_start_state["current_check_index"])

            live_verification.check_started(
                check_index=0,
                name="readme",
                kind="file_exists",
                argv=None,
                cwd=None,
            )
            mid_state = json.loads(
                state_path.read_text(encoding="utf-8")
            )["active_verification"]
            self.assertEqual(0, mid_state["current_check_index"])
            self.assertEqual("readme", mid_state["current_check_name"])
            self.assertEqual("file_exists", mid_state["current_check_kind"])
            self.assertIsNotNone(mid_state["current_check_started_at"])

            live_verification.check_finished(
                check_index=0,
                name="readme",
                kind="file_exists",
                status="fail",
                duration_seconds=0.0,
                failure_reason="file-missing",
                returncode=None,
            )
            after_check_state = json.loads(
                state_path.read_text(encoding="utf-8")
            )["active_verification"]
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
            live_turn.stop()
            live_turn.finish(runner._utc_now())

            state_payload = json.loads(state_path.read_text(encoding="utf-8"))
            event_records = read_jsonl(event_path)
            action_events = [
                record["payload"]
                for record in event_records
                if record["name"] == events.ACTION_REQUIRED_EVENT
            ]
            self.assertEqual(1, len(action_events))
            self.assertEqual("codex", action_events[0]["harness"])
            self.assertEqual("codex", state_payload["action_required"]["harness"])

    def test_run_persistence_serializes_live_turn_and_control_state_writes(
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
            activity_write_started = threading.Event()
            release_activity_write = threading.Event()
            delayed_once = False
            thread_errors: list[BaseException] = []
            original_write_json = runner._write_json_file

            def delayed_write_json(path: Path, payload_arg: dict[str, object]) -> None:
                nonlocal delayed_once
                path = Path(path)
                active = payload_arg.get("active_turn")
                control = payload_arg.get("control")
                if (
                    path == state_path
                    and not delayed_once
                    and isinstance(active, dict)
                    and active.get("last_activity_stream") == "stdout"
                    and isinstance(control, dict)
                    and control.get("next_harness") is None
                ):
                    delayed_once = True
                    serialized = json.dumps(payload_arg, sort_keys=True, indent=2) + "\n"
                    activity_write_started.set()
                    release_activity_write.wait(timeout=2)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    temporary_path = path.with_name(f".{path.name}.blocked.tmp")
                    temporary_path.write_text(serialized, encoding="utf-8")
                    os.replace(temporary_path, path)
                    return
                original_write_json(path, payload_arg)

            def record_activity() -> None:
                try:
                    live_turn.record_activity("stdout", "new output\n")
                except BaseException as error:
                    thread_errors.append(error)

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

            with mock.patch.object(
                runner,
                "_write_json_file",
                side_effect=delayed_write_json,
            ):
                activity_thread = threading.Thread(target=record_activity)
                activity_thread.start()
                self.assertTrue(activity_write_started.wait(timeout=2))

                control_thread = threading.Thread(target=record_control)
                control_thread.start()
                time.sleep(0.1)
                self.assertTrue(control_thread.is_alive())

                release_activity_write.set()
                activity_thread.join(timeout=2)
                control_thread.join(timeout=2)

            self.assertFalse(activity_thread.is_alive())
            self.assertFalse(control_thread.is_alive())
            if thread_errors:
                raise thread_errors[0]

            state_payload = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual("stdout", state_payload["active_turn"]["last_activity_stream"])
            self.assertEqual("claude", state_payload["control"]["next_harness"])
            self.assertEqual("harness-selected", state_payload["control"]["last_action"])
            control_events = [
                record["payload"]
                for record in read_jsonl(event_path)
                if record["name"] == events.RUN_CONTROL_EVENT
            ]
            self.assertEqual(["harness-selected"], [event["action"] for event in control_events])

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


if __name__ == "__main__":
    unittest.main()
