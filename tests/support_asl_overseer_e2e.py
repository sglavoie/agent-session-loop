"""Shared fixtures for test_asl_overseer_e2e."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    clock,
    plans_snapshot,
    process_tree,
    state,
)
from tests import support


class _WorldProcesses:
    """Process and repository operations for the isolated test world."""

    repos: dict[str, Path]
    state_root: Path
    workspace_root: str
    children: list[subprocess.Popen[bytes]]

    def change(self, alias: str, name: str = "work.txt", text: str = "wip\n") -> None:
        path = self.repos[alias] / name
        path.write_text(text, encoding="utf-8")

    def publish_turn(
        self, plan_dir: Path, aliases: tuple[str, ...], *, turn_index: int = 1
    ) -> None:
        """Publish the write-guard evidence a checkpoint may commit from."""
        record = {
            "turn_index": turn_index,
            "session_id": "1",
            "workspace_write_guard": {
                "changed": {
                    alias: {
                        "head_before": support.git_stdout(
                            self.repos[alias], "rev-parse", "HEAD"
                        ),
                        "head_after": support.git_stdout(
                            self.repos[alias], "rev-parse", "HEAD"
                        ),
                        "qualified_paths": [f"{alias}:work.txt"],
                    }
                    for alias in aliases
                }
            },
        }
        run_dir = (
            state.plan_state_dir(plan_dir, self.state_root)
            / state.RUNS_DIR_NAME
            / f"{plan_dir.name}-run-1"
            / f"turn-{turn_index:04d}"
        )
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / state.RESULT_FILE_NAME).write_text(
            json.dumps(record), encoding="utf-8"
        )

    def start_fake_harness(self) -> subprocess.Popen[bytes]:
        """A real process standing in for the harness a lock names.

        The overseer never talks to it. What it needs to be is a pid this host's
        process table reports as alive, because that -- not any field in the
        lock file -- is what makes a lock active and a run owned.
        """
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(600)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.children.append(child)
        return child

    def _stop_children(self) -> None:
        for child in self.children:
            with contextlib.suppress(Exception):
                child.kill()
            with contextlib.suppress(Exception):
                child.wait(timeout=10)
        self.children.clear()

    def hold_lock(self, plan_dir: Path, pid: int) -> Path:
        """Publish a lock naming ``pid`` as the run's owner."""
        snapshot = plans_snapshot.inspect_plan(plan_dir)
        lock_path = state.lock_path_for_plan(snapshot.plan_dir, self.state_root)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(
            json.dumps(
                {
                    "version": 3,
                    "owner": "agent-session-loop",
                    "pid": pid,
                    "hostname": socket.gethostname(),
                    "process_start": process_tree.process_birth(pid),
                    "wakeup_endpoint": None,
                    "plan_dir": str(snapshot.plan_dir),
                    "plan_path_hash": snapshot.path_hash,
                    "plan_hash": snapshot.plan_hash,
                    "repositories": [self.workspace_root],
                    "created_at": clock.utc_iso_now(),
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return lock_path

    def release_lock(self, plan_dir: Path) -> None:
        """Drop the lock the way a runner that paused and exited does.

        A paused run is not a running one: the worker releases the plan before
        it exits, which is the state any later resume actually meets.
        """
        snapshot = plans_snapshot.inspect_plan(plan_dir)
        state.lock_path_for_plan(snapshot.plan_dir, self.state_root).unlink(
            missing_ok=True
        )


INTEGRATION_ENV = "AMX_ASL_INTEGRATION"


SOURCE_ENV = "AMX_SOURCE_ROOT"


BUILD_TIMEOUT_SECONDS = 300.0


WATCHER = "overseer-e2e"


SECOND_WATCHER = "overseer-e2e-other"


HARNESS = "claude"


HARNESS_EMOJI = "\N{FOX FACE}"


PROFILE = "overseer-e2e"


AUTHORIZED = "tt"


EXCLUDED = "vendor"


SECOND = "tt-docs"


SECRET = "vendor-secret-8f3a1c-never-disclosed"


STANDING_INSTRUCTION = (
    "go ahead and checkpoint work in progress and answer routine tooling "
    "questions on my runs without waiting for me"
)


LATER_HOLD = "actually hold off on committing anything in tt until I have looked at it"


_BUILD: dict[str, Any] = {}


def _required() -> bool:
    value = os.environ.get(INTEGRATION_ENV, "").strip().lower()
    return value not in ("", "0", "false", "no", "off")


def _candidate_sources() -> tuple[Path, ...]:
    named = os.environ.get(SOURCE_ENV, "").strip()
    if named:
        return (Path(named).expanduser(),)
    beside = support.REPO_ROOT.parent
    return (
        beside / "sglavoie_agentmux",
        beside / "agentmux",
        beside / "1_dev_projects" / "sglavoie_agentmux",
    )


def _locate_source() -> Path | None:
    """Find an AgentMux checkout, or report that there is none to build."""
    for candidate in _candidate_sources():
        overseer = candidate / "cmd" / "overseer"
        if (candidate / "go.mod").is_file() and overseer.is_dir():
            return candidate.resolve()
    return None


def _unavailable(reason: str) -> None:
    """Skip this module, or fail it when the paired gate was asked for."""
    detail = (
        f"{reason}; set {SOURCE_ENV} to an AgentMux checkout to run the paired "
        "end-to-end suite"
    )
    if _required():
        raise AssertionError(f"{INTEGRATION_ENV} is set but {detail}")
    raise unittest.SkipTest(detail)


def setUpModule() -> None:
    """Build the binary under test once, before any fixture needs it."""
    source = _locate_source()
    if source is None:
        _unavailable("no AgentMux source tree was found beside this checkout")
        return
    go = shutil.which("go")
    if not go:
        _unavailable("no `go` toolchain is on PATH")
        return
    workdir = tempfile.TemporaryDirectory(prefix="asl-overseer-e2e-amx-")
    binary = Path(workdir.name) / "amx"
    completed = subprocess.run(
        [go, "build", "-o", str(binary), "."],
        cwd=source,
        text=True,
        capture_output=True,
        timeout=BUILD_TIMEOUT_SECONDS,
        check=False,
    )
    if completed.returncode != 0 or not binary.is_file():
        workdir.cleanup()
        _unavailable(
            f"`go build` in {source} failed: {completed.stderr.strip()[:2000]}"
        )
        return
    _BUILD["workdir"] = workdir
    _BUILD["source"] = source
    _BUILD["binary"] = binary


def tearDownModule() -> None:
    workdir = _BUILD.pop("workdir", None)
    if workdir is not None:
        workdir.cleanup()
