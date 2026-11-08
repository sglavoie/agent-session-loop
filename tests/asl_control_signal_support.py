"""ControlSignalTests.

Split from tests/test_asl_control.py."""

from __future__ import annotations

import errno
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    config,
    control_queue,
    control_signal,
    events,
    plans_files,
    process_tree,
    state,
    state_locks,
    turn_execution_constants,
)
from tests.asl_control_support import LIVE_WAKEUP
from tests.support import (
    REPO_ROOT,
    init_git_repo,
    install_fake_harness,
    live_wakeup_endpoint,
    read_jsonl,
    write_plan,
)


class ControlSignalTestsBase(unittest.TestCase):
    """`control_signal`: same-host wakeup, reported but never enforced."""

    DEAD_PID = 2**30

    REAL_BIRTH = object()

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.plan_dir = self.tmp / "plan"
        self.state_dir = self.tmp / "state"
        write_plan(self.plan_dir, ("pending",))
        if process_tree.process_birth(os.getpid()) is None:
            self.skipTest("this platform's ps reports no process start time")

    def write_lock(
        self,
        *,
        pid: int,
        hostname: str | None,
        process_start: object = REAL_BIRTH,
        wakeup_endpoint: Path | str | None = None,
    ) -> Path:
        plan_state = state.plan_state_dir(self.plan_dir, self.state_dir)
        plan_state.mkdir(parents=True, exist_ok=True)
        lock_path = plan_state / state.LOCK_FILE_NAME
        payload: dict[str, object] = {
            "version": 3,
            "owner": "agent-session-loop",
            "pid": pid,
            "plan_dir": str(plans_files.canonicalize_path(self.plan_dir)),
            "created_at": "2026-08-13T00:00:00+00:00",
        }
        if hostname is not None:
            payload["hostname"] = hostname
        if process_start is self.REAL_BIRTH:
            process_start = process_tree.process_birth(pid)
        if process_start is not None:
            payload["process_start"] = process_start
        if wakeup_endpoint is not None:
            payload["wakeup_endpoint"] = str(wakeup_endpoint)
        lock_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        return lock_path

    def notify(self) -> control_signal.NotifyResult:
        return control_signal.notify_runner(self.plan_dir, state_dir=self.state_dir)

    def serve_endpoint(self) -> tuple[Path, threading.Event]:
        """Hold this plan's endpoint open for the rest of the test."""
        return self.enterContext(live_wakeup_endpoint(self.plan_dir, self.state_dir))

    def abandoned_endpoint(self) -> Path:
        """A FIFO whose creator is gone: the node survives, the reader does not.

        This is what a runner killed hard leaves on disk, and the case every
        pid-based scheme had to guess about.
        """
        path = self.tmp / f"abandoned-{uuid.uuid4().hex}.fifo"
        os.mkfifo(path, 0o600)
        return path

    ENDPOINT_CHILD_SCRIPT = """
import os
import pathlib
import signal
import sys
import threading

sys.path.insert(0, sys.argv[1])
from agent_session_loop import control_signal

# A signal is the only other channel that could reach this process, so exiting
# on one turns "the producer fell back to acting on a pid" into a visible
# failure instead of a silent success.
signal.signal(signal.SIGUSR1, lambda *_: os._exit(70))

ready = pathlib.Path(sys.argv[3])
event = threading.Event()
with control_signal.open_wakeup_endpoint(sys.argv[2], event) as path:
    if path is None:
        sys.exit(2)
    # Published by rename: the parent waits on this path existing, so a
    # truncate-then-write would let it read the empty file the create leaves
    # behind and take Path("") — the current directory — for the endpoint.
    staged = ready.with_name(ready.name + ".staged")
    staged.write_text(str(path), encoding="utf-8")
    os.replace(staged, ready)
    sys.exit(0 if event.wait(30) else 1)
"""

    def start_endpoint_child(self) -> tuple[subprocess.Popen[bytes], Path]:
        """Spawn a process serving its own endpoint, exiting 0 on a wakeup.

        A second process is the only way to observe a wakeup that was *not*
        delivered: in-process every refusal looks the same from the outside,
        while a child that outlives the attempt is proof nothing reached it.
        """
        child_state = self.tmp / f"child-state-{uuid.uuid4().hex}"
        child_state.mkdir(parents=True)
        ready = self.tmp / f"ready-{uuid.uuid4().hex}"
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                self.ENDPOINT_CHILD_SCRIPT,
                str(REPO_ROOT),
                str(child_state),
                str(ready),
            ],
        )
        # LIFO: the kill runs first, then the reap that keeps a child which was
        # deliberately never woken from outliving the test as a zombie.
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        deadline = time.monotonic() + 30.0
        while not ready.exists() and time.monotonic() < deadline:
            self.assertIsNone(child.poll(), "child exited before serving")
            time.sleep(0.05)
        self.assertTrue(ready.exists())
        return child, Path(ready.read_text(encoding="utf-8"))

    RUN_CHILD_SCRIPT = """
import pathlib
import signal
import sys
import time

sys.path.insert(0, sys.argv[1])
from agent_session_loop import cli, run_outcome, state_locks

WATCHED = (signal.SIGUSR1, signal.SIGUSR2, signal.SIGINT, signal.SIGTERM)
before = {number: signal.getsignal(number) for number in WATCHED}

locked = pathlib.Path(sys.argv[2])
emitting = pathlib.Path(sys.argv[3])
hold_emit = sys.argv[4] == "1"
real_acquire = state_locks.acquire_lock
real_emit = run_outcome._emit_run_payload


def hold_open(marker):
    # Announce the window, then keep it open until the parent releases it.
    marker.write_text("open", encoding="utf-8")
    released = marker.with_name(marker.name + ".release")
    # The cap only bounds a parent that died without releasing; the test's
    # cleanup kills this process long before it is reached.
    deadline = time.monotonic() + 120.0
    while not released.exists() and time.monotonic() < deadline:
        time.sleep(0.01)


def widened_acquire(*args, **kwargs):
    info = real_acquire(*args, **kwargs)
    hold_open(locked)
    return info


def delayed_emit(*args, **kwargs):
    hold_open(emitting)
    return real_emit(*args, **kwargs)


state_locks.acquire_lock = widened_acquire
if hold_emit:
    run_outcome._emit_run_payload = delayed_emit
code = cli.main(sys.argv[5:])
if {number: signal.getsignal(number) for number in WATCHED} != before:
    sys.exit(97)
sys.exit(code)
"""

    def start_run_child(
        self, *, hold_emit: bool = False
    ) -> tuple[subprocess.Popen[str], Path, Path]:
        """Spawn a real `asl run` whose lock publication is held open.

        The run stops right after publishing its lock and waits there until
        the test calls ``release(locked)``; with ``hold_emit`` it stops again
        just before emitting its terminal payload, until ``release(emitting)``.
        """
        # Two pending sessions, so honoring the queued stop is visible as a
        # session the run declined to start rather than as the plan running out.
        write_plan(self.plan_dir, ("pending", "pending"))
        repo_root = self.tmp / "repo"
        config_home = self.tmp / "config"
        init_git_repo(repo_root)
        config_path = config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps(
                {"codex": {"executable": str(install_fake_harness(self.tmp, "codex"))}}
            ),
            encoding="utf-8",
        )
        env = os.environ.copy()
        env.update(
            {
                "XDG_CONFIG_HOME": str(config_home),
                "FAKE_CODEX_MODE": "success",
                "PYTHONPATH": str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", ""),
            }
        )
        locked = self.tmp / f"locked-{uuid.uuid4().hex}"
        emitting = self.tmp / f"emitting-{uuid.uuid4().hex}"
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                self.RUN_CHILD_SCRIPT,
                str(REPO_ROOT),
                str(locked),
                str(emitting),
                "1" if hold_emit else "0",
                "run",
                str(self.plan_dir),
                "--cwd",
                str(repo_root),
                "--harness",
                "codex",
                "--state-dir",
                str(self.state_dir),
                "--json",
                "--allow-not-loop-ready",
            ],
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(child.kill)
        self.awaitMarker(child, locked, "the run never published its lock")
        return child, locked, emitting

    @staticmethod
    def release(marker: Path) -> None:
        """Close the window the child announced with ``marker``."""
        marker.with_name(marker.name + ".release").write_text(
            "release", encoding="utf-8"
        )

    def awaitMarker(
        self, child: subprocess.Popen[str], marker: Path, message: str
    ) -> None:
        deadline = time.monotonic() + 60.0
        while not marker.exists() and time.monotonic() < deadline:
            self.assertIsNone(child.poll(), message)
            time.sleep(0.01)
        self.assertTrue(marker.exists(), message)


__all__ = [
    "ControlSignalTestsBase",
    "LIVE_WAKEUP",
    "Path",
    "REPO_ROOT",
    "config",
    "control_queue",
    "control_signal",
    "errno",
    "events",
    "init_git_repo",
    "install_fake_harness",
    "json",
    "live_wakeup_endpoint",
    "mock",
    "os",
    "plans_files",
    "process_tree",
    "read_jsonl",
    "signal",
    "socket",
    "state",
    "state_locks",
    "subprocess",
    "sys",
    "tempfile",
    "threading",
    "time",
    "turn_execution_constants",
    "unittest",
    "uuid",
    "write_plan",
]
