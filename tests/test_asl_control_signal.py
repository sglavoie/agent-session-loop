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
    plans,
    process_tree,
    state,
    state_locks,
    turn_execution,
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


class ControlSignalTests(unittest.TestCase):
    """`control_signal`: same-host wakeup, reported but never enforced."""

    DEAD_PID = 2**30
    # Sentinel for "record whatever birth time this pid really has", which is
    # what a lock published by the runner itself carries.
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
            "plan_dir": str(plans.canonicalize_path(self.plan_dir)),
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

    def test_no_lock_reports_no_runner(self) -> None:
        result = self.notify()

        self.assertEqual(control_signal.NotifyResult(False, "no-runner"), result)

    def test_dead_pid_reports_stale_lock(self) -> None:
        lock_path = self.write_lock(
            pid=self.DEAD_PID,
            hostname=socket.gethostname(),
            wakeup_endpoint=self.abandoned_endpoint(),
        )
        before = lock_path.read_bytes()

        result = self.notify()

        self.assertEqual(control_signal.NotifyResult(False, "stale-lock"), result)
        # A producer reads the lock through the non-reclaiming reader, so even
        # a provably dead owner leaves the file untouched.
        self.assertEqual(before, lock_path.read_bytes())

    def test_other_host_reports_remote_host(self) -> None:
        self.write_lock(
            pid=os.getpid(),
            hostname="some-other-host",
            wakeup_endpoint=self.abandoned_endpoint(),
        )

        result = self.notify()

        self.assertEqual(control_signal.NotifyResult(False, "remote-host"), result)

    def test_lock_without_an_endpoint_reports_no_endpoint(self) -> None:
        # Two runners publish this: one from before the endpoint existed, one
        # whose filesystem could not host a FIFO. Both fall back to the poll.
        self.write_lock(pid=os.getpid(), hostname=socket.gethostname())

        self.assertEqual(
            control_signal.NotifyResult(False, "no-endpoint"), self.notify()
        )

    def test_live_endpoint_receives_every_wakeup(self) -> None:
        endpoint, woken = self.serve_endpoint()
        self.write_lock(
            pid=os.getpid(),
            hostname=socket.gethostname(),
            wakeup_endpoint=endpoint,
        )

        self.assertEqual(LIVE_WAKEUP, self.notify())
        self.assertTrue(woken.wait(timeout=5.0))

        # The reader keeps serving: a wakeup is not a one-shot arming.
        woken.clear()
        self.assertEqual(LIVE_WAKEUP, self.notify())
        self.assertTrue(woken.wait(timeout=5.0))

    def test_abandoned_endpoint_reports_endpoint_closed(self) -> None:
        # The FIFO is real and the mode is right; nobody holds the read end, so
        # the open fails with ENXIO instead of writing into nowhere.
        self.write_lock(
            pid=os.getpid(),
            hostname=socket.gethostname(),
            wakeup_endpoint=self.abandoned_endpoint(),
        )

        self.assertEqual(
            control_signal.NotifyResult(False, "endpoint-closed"),
            self.notify(),
        )

    def test_missing_endpoint_reports_endpoint_closed(self) -> None:
        self.write_lock(
            pid=os.getpid(),
            hostname=socket.gethostname(),
            wakeup_endpoint=self.tmp / "gone.fifo",
        )

        self.assertEqual(
            control_signal.NotifyResult(False, "endpoint-closed"),
            self.notify(),
        )

    def test_endpoint_that_is_not_a_fifo_reports_endpoint_failed(self) -> None:
        # A path the runner never created, or one something else replaced. It
        # is not the transport, so it is a failure rather than a closed reader.
        regular = self.tmp / "not-a-fifo"
        regular.write_text("", encoding="utf-8")
        self.write_lock(
            pid=os.getpid(),
            hostname=socket.gethostname(),
            wakeup_endpoint=regular,
        )

        self.assertEqual(
            control_signal.NotifyResult(False, "endpoint-failed"),
            self.notify(),
        )

    def test_a_full_pipe_still_counts_as_delivered(self) -> None:
        """Unread bytes in the pipe are a wakeup already waiting to be seen."""
        endpoint = self.tmp / "full.fifo"
        os.mkfifo(endpoint, 0o600)
        read_fd = os.open(endpoint, os.O_RDONLY | os.O_NONBLOCK)
        self.addCleanup(os.close, read_fd)
        write_fd = os.open(endpoint, os.O_WRONLY | os.O_NONBLOCK)
        self.addCleanup(os.close, write_fd)
        while True:
            try:
                os.write(write_fd, b"\0" * 4096)
            except OSError as error:
                self.assertIn(error.errno, (errno.EAGAIN, errno.EWOULDBLOCK))
                break

        self.assertEqual(LIVE_WAKEUP, control_signal._deliver_wakeup(endpoint))

    def test_endpoint_stops_answering_once_the_runner_leaves(self) -> None:
        event = threading.Event()
        with control_signal.open_wakeup_endpoint(self.state_dir, event) as endpoint:
            if endpoint is None:
                self.skipTest("this filesystem cannot host a wakeup FIFO")
            self.assertEqual(LIVE_WAKEUP, control_signal._deliver_wakeup(endpoint))
            self.assertTrue(event.wait(timeout=5.0))

        # The node is unlinked on the way out, so a producer that resolved the
        # path earlier finds nothing rather than a pipe nobody drains.
        self.assertFalse(endpoint.exists())
        self.assertEqual(
            control_signal.NotifyResult(False, "endpoint-closed"),
            control_signal._deliver_wakeup(endpoint),
        )

    def test_a_filesystem_that_cannot_host_a_fifo_yields_no_endpoint(
        self,
    ) -> None:
        # Losing the endpoint costs latency, never correctness, so the run has
        # to start anyway — with a lock that names nothing to write to.
        with (
            mock.patch.object(control_signal.os, "mkfifo", side_effect=OSError("nope")),
            control_signal.open_wakeup_endpoint(
                self.state_dir, threading.Event()
            ) as endpoint,
        ):
            self.assertIsNone(endpoint)
            state_locks.acquire_lock(
                self.plan_dir, self.state_dir, wakeup_endpoint=endpoint
            )
            self.addCleanup(state_locks.release_lock, self.plan_dir, self.state_dir)

            self.assertEqual(
                control_signal.NotifyResult(False, "no-endpoint"),
                self.notify(),
            )

    def test_the_production_publisher_records_the_endpoint_it_serves(
        self,
    ) -> None:
        # No fixture: the lock `acquire_lock` writes is the one a producer has
        # to be able to wake, and the endpoint in it is the one being read.
        endpoint, woken = self.serve_endpoint()
        state_locks.acquire_lock(
            self.plan_dir, self.state_dir, wakeup_endpoint=endpoint
        )
        self.addCleanup(state_locks.release_lock, self.plan_dir, self.state_dir)

        lock = state_locks.inspect_lock_info(self.plan_dir, self.state_dir)

        self.assertIsNotNone(lock)
        assert lock is not None
        self.assertEqual(str(endpoint), lock.wakeup_endpoint)
        self.assertEqual(LIVE_WAKEUP, self.notify())
        self.assertTrue(woken.wait(timeout=5.0))

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

    def test_wakeup_round_trip_reaches_another_process(self) -> None:
        child, endpoint = self.start_endpoint_child()
        self.write_lock(
            pid=child.pid,
            hostname=socket.gethostname(),
            wakeup_endpoint=endpoint,
        )
        started = time.monotonic()

        self.assertEqual(LIVE_WAKEUP, self.notify())

        self.assertEqual(0, child.wait(timeout=10))
        self.assertLess(
            time.monotonic() - started,
            turn_execution.LIVE_CONTROL_POLL_INTERVAL_SECONDS,
        )

    def test_a_process_wearing_the_recorded_identity_is_never_woken(
        self,
    ) -> None:
        """Pid reuse, with an identity token that matches rather than differs.

        The lock names a pid, a host, and a birth time that all describe this
        live child exactly — the collision no pid-plus-birth-token check can
        detect, because there is nothing left to compare. What the lock also
        names is the dead runner's endpoint, and that is what delivery is
        addressed to: no reader holds it, so the write is refused and the
        process wearing the pid is untouched.
        """
        child, own_endpoint = self.start_endpoint_child()
        self.write_lock(
            pid=child.pid,
            hostname=socket.gethostname(),
            wakeup_endpoint=self.abandoned_endpoint(),
        )

        # Every field a producer used to authenticate on agrees with the live
        # process, so this is the case that used to pass the check and signal.
        lock = state_locks.inspect_lock_info(self.plan_dir, self.state_dir)
        self.assertIsNotNone(lock)
        assert lock is not None
        self.assertFalse(lock.stale)
        self.assertEqual(child.pid, lock.pid)
        self.assertEqual(socket.gethostname(), lock.owner_hostname)
        self.assertEqual(
            process_tree.process_birth(child.pid), lock.owner_process_start
        )

        self.assertEqual(
            control_signal.NotifyResult(False, "endpoint-closed"),
            self.notify(),
        )

        # The child exits on a wakeup and on SIGUSR1, so still running is proof
        # that neither channel reached it.
        time.sleep(0.25)
        self.assertIsNone(child.poll(), "a recycled pid was woken anyway")

        # Same process, same recorded identity: pointing the lock at the
        # endpoint it really reads is what makes it reachable, and nothing else.
        self.write_lock(
            pid=child.pid,
            hostname=socket.gethostname(),
            wakeup_endpoint=own_endpoint,
        )

        self.assertEqual(LIVE_WAKEUP, self.notify())
        self.assertEqual(0, child.wait(timeout=10))

    def test_serving_an_endpoint_installs_no_signal_handler(self) -> None:
        """The obligation the signal design created, removed at the source.

        A runner that never changes a disposition has none to restore, so no
        ordering between publishing the lock, running the loop, and releasing
        can leave the process holding a handler it did not start with.
        """
        watched = (
            signal.SIGUSR1,
            signal.SIGUSR2,
            signal.SIGINT,
            signal.SIGTERM,
        )
        before = {number: signal.getsignal(number) for number in watched}
        event = threading.Event()

        with control_signal.open_wakeup_endpoint(self.state_dir, event) as endpoint:
            if endpoint is None:
                self.skipTest("this filesystem cannot host a wakeup FIFO")
            self.assertTrue(control_signal._deliver_wakeup(endpoint).delivered)
            self.assertTrue(event.wait(timeout=5.0))
            self.assertEqual(
                before, {number: signal.getsignal(number) for number in watched}
            )

        self.assertEqual(
            before, {number: signal.getsignal(number) for number in watched}
        )

    # Runs the production `asl run` entry point with two hooks, each widening a
    # window a producer has to land a wakeup in: the lock publication is
    # stretched, so a command can arrive between "a producer can find this
    # endpoint" and "the run loop is reading it", and the terminal payload
    # emission can be held open, which is the far side — the lock is released
    # and the endpoint unlinked. Every signal disposition is captured before
    # `cli.main` and compared after, so a run that installed a handler and left
    # it behind reports itself as exit 97.
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
window = float(sys.argv[3])
emitting = pathlib.Path(sys.argv[4])
emit_delay = float(sys.argv[5])
real_acquire = state_locks.acquire_lock
real_emit = run_outcome._emit_run_payload


def widened_acquire(*args, **kwargs):
    info = real_acquire(*args, **kwargs)
    locked.write_text("locked", encoding="utf-8")
    time.sleep(window)
    return info


def delayed_emit(*args, **kwargs):
    emitting.write_text("emitting", encoding="utf-8")
    time.sleep(emit_delay)
    return real_emit(*args, **kwargs)


state_locks.acquire_lock = widened_acquire
if emit_delay:
    run_outcome._emit_run_payload = delayed_emit
code = cli.main(sys.argv[6:])
if {number: signal.getsignal(number) for number in WATCHED} != before:
    sys.exit(97)
sys.exit(code)
"""
    # Wide enough that the parent's marker poll, queue append, and wakeup all
    # land inside it even on a loaded machine.
    LOCK_WINDOW_SECONDS = 5.0

    def start_run_child(
        self, *, emit_delay: float = 0.0
    ) -> tuple[subprocess.Popen[str], Path, Path]:
        """Spawn a real `asl run` whose lock publication is held open."""
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
                str(self.LOCK_WINDOW_SECONDS),
                str(emitting),
                str(emit_delay),
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

    def awaitMarker(
        self, child: subprocess.Popen[str], marker: Path, message: str
    ) -> None:
        deadline = time.monotonic() + 60.0
        while not marker.exists() and time.monotonic() < deadline:
            self.assertIsNone(child.poll(), message)
            time.sleep(0.01)
        self.assertTrue(marker.exists(), message)

    def test_control_at_lock_publication_never_kills_the_run(self) -> None:
        child, _locked, _emitting = self.start_run_child()

        command = control_queue.append_command(
            self.plan_dir,
            control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT,
            state_dir=self.state_dir,
        )
        self.assertEqual(LIVE_WAKEUP, self.notify())

        stdout, stderr = child.communicate(timeout=180)
        # A negative return code is death by signal, which is what this
        # ordering exists to prevent; 97 is the child reporting that the run
        # left a signal disposition it did not start with.
        self.assertEqual(0, child.returncode, stderr)
        payload = json.loads(stdout)

        # The wakeup was harmless *and* the command it announced was applied.
        self.assertEqual("stopped", payload["reason"])
        self.assertEqual([1], payload["completed_sessions"])
        receipts = [
            record["payload"]
            for record in read_jsonl(
                state.plan_state_dir(self.plan_dir, self.state_dir)
                / events.EVENTS_FILE_NAME
            )
            if record["name"] == events.RUN_CONTROL_EVENT
            and record["payload"]["action"]
            == events.RUN_CONTROL_ACTION_COMMAND_OBSERVED
        ]
        self.assertEqual([command.id], [r["command_id"] for r in receipts])

    # Long enough that the parent's lock read, marker poll and send all land
    # before the run finishes reporting, even on a loaded machine.
    EMIT_DELAY_SECONDS = 10.0

    def test_a_wakeup_resolved_before_release_lands_on_nothing(self) -> None:
        """A wakeup aimed while the lock existed and sent after it is gone.

        Resolving the endpoint and writing to it are two steps, so a producer
        can read the path while the run holds the lock and write once it no
        longer does. The endpoint outlives the lock by design — it is unlinked
        after the release — and the write is then refused rather than made
        harmless, which is what removes the disposition question entirely: the
        run reaches its terminal payload without anything having to absorb a
        late delivery.
        """
        child, _locked, emitting = self.start_run_child(
            emit_delay=self.EMIT_DELAY_SECONDS
        )
        lock_path = (
            state.plan_state_dir(self.plan_dir, self.state_dir) / state.LOCK_FILE_NAME
        )

        lock = state_locks.inspect_lock_info(self.plan_dir, self.state_dir)
        self.assertIsNotNone(lock)
        assert lock is not None
        self.assertEqual(child.pid, lock.pid)
        # The whole of the producer's aim, taken while the lock is published.
        endpoint = lock.wakeup_endpoint
        self.assertIsNotNone(endpoint)
        assert endpoint is not None

        self.awaitMarker(child, emitting, "the run never reached its terminal payload")
        self.assertFalse(
            lock_path.exists(), "the run was woken before releasing its lock"
        )
        self.assertFalse(Path(endpoint).exists(), "the endpoint outlived its reader")

        self.assertEqual(
            control_signal.NotifyResult(False, "endpoint-closed"),
            control_signal._deliver_wakeup(Path(endpoint)),
        )

        stdout, stderr = child.communicate(timeout=180)
        self.assertEqual(0, child.returncode, stderr)
        payload = json.loads(stdout)
        self.assertEqual("all-complete", payload["reason"])
        self.assertEqual([1, 2], payload["completed_sessions"])


if __name__ == "__main__":
    unittest.main()
