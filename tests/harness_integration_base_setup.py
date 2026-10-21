"""Setup, environment and payload helpers for the fake-harness tests."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from agent_session_loop import (
    clock,
    config,
    events,
    run_selection,
    state,
)
from tests.support import (
    REPO_ROOT,
    _contract_block,
    install_fake_codex,
    read_jsonl,
    write_plan,
    write_plan_with_ids,
)


class HarnessSetupMixin(unittest.TestCase):
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
