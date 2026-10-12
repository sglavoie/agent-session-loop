"""AgentMuxSuiteIsolationTests, StaleLockReclamationTests.

Split from tests/test_asl_control.py."""

from __future__ import annotations

import ast
import inspect
import io
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    agent_mux_delivery,
    plans,
    run_persistence,
    state,
)
from tests.support import (
    write_plan,
)


class AgentMuxSuiteIsolationTests(unittest.TestCase):
    """Lock in the suite-wide default: no live AgentMux delivery."""

    def test_suite_environment_forces_notification_off(self) -> None:
        # tests/support.py exports the kill switch at import time; every
        # nested ASL subprocess inherits it, so the full suite produces zero
        # live lifecycle events even with a real amx on PATH.
        self.assertEqual(
            agent_mux_delivery.NOTIFY_OFF,
            os.environ.get(agent_mux_delivery.NOTIFY_ENV_VAR),
        )
        self.assertFalse(run_persistence._agent_mux_notify_enabled())

    def test_notify_helpers_short_circuit_under_suite_default(self) -> None:
        with (
            mock.patch.object(agent_mux_delivery.shutil, "which") as which,
            mock.patch.object(agent_mux_delivery.subprocess, "run") as run,
        ):
            run_persistence._notify_agent_mux_loop_started({"run_id": "run-x"})
            run_persistence._notify_agent_mux_loop_finished({"run_id": "run-x"})

        which.assert_not_called()
        run.assert_not_called()


class StaleLockReclamationTests(unittest.TestCase):
    """Reading a lock clears it only when this host can prove its owner died."""

    DEAD_PID = 2**30
    # Long enough that a competitor blocked on the mutex is unambiguous, short
    # enough to keep the suite fast. Both are bounds on correct behaviour, not
    # on scheduling: a serialized competitor never finishes sooner.
    RECLAIM_WINDOW_SECONDS = 0.5
    MUTEX_HOLD_SECONDS = 0.3
    # The real 5s bound only makes a contention test slow; the holder keeps the
    # claim until the call under test returns, so the wait always expires.
    CONTENDED_BOUND_SECONDS = 0.05

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.plan_dir = self.tmp / "plan"
        self.state_dir = self.tmp / "state"
        self.plan_dir.mkdir()
        write_plan(self.plan_dir, ("pending",))
        plan_state = state.plan_state_dir(self.plan_dir, self.state_dir)
        plan_state.mkdir(parents=True, exist_ok=True)
        self.lock_path = plan_state / state.LOCK_FILE_NAME

    def write_lock(self, *, pid: int, hostname: str | None) -> None:
        payload: dict[str, object] = {
            "version": 1,
            "owner": "agent-session-loop",
            "pid": pid,
            "plan_dir": str(plans.canonicalize_path(self.plan_dir)),
            "created_at": "2026-07-30T22:50:34.651504+00:00",
        }
        if hostname is not None:
            payload["hostname"] = hostname
        self.lock_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

    def read_lock_info(self) -> tuple[state.LockInfo, str]:
        buffer = io.StringIO()
        with redirect_stderr(buffer):
            lock_info = state.read_lock_info(self.plan_dir, self.state_dir)
        assert lock_info is not None
        return lock_info, buffer.getvalue()

    def test_dead_pid_on_this_host_is_reclaimed_and_logged(self) -> None:
        self.write_lock(pid=self.DEAD_PID, hostname=socket.gethostname())

        lock_info, stderr = self.read_lock_info()

        # The caller still learns what was found; only the file is gone.
        self.assertTrue(lock_info.stale)
        self.assertEqual(f"pid {self.DEAD_PID} is not running", lock_info.reason)
        self.assertFalse(self.lock_path.exists())
        self.assertIn("reclaimed stale lock", stderr)
        self.assertIn(str(self.lock_path), stderr)

    def test_dead_pid_from_another_host_is_refused(self) -> None:
        self.write_lock(pid=self.DEAD_PID, hostname="some-other-host")
        before = self.lock_path.read_bytes()

        lock_info, stderr = self.read_lock_info()

        # A remote pid is unknowable here, so `asl unlock` stays the only path.
        self.assertEqual("lock was recorded on host some-other-host", lock_info.reason)
        self.assertEqual(before, self.lock_path.read_bytes())
        self.assertEqual("", stderr)

    def test_legacy_lock_without_hostname_is_refused(self) -> None:
        # Predates the hostname field, so the pid names no process table.
        self.write_lock(pid=self.DEAD_PID, hostname=None)
        before = self.lock_path.read_bytes()

        lock_info, stderr = self.read_lock_info()

        self.assertEqual(f"pid {self.DEAD_PID} is not running", lock_info.reason)
        self.assertEqual(before, self.lock_path.read_bytes())
        self.assertEqual("", stderr)

    def test_live_pid_is_never_reclaimed(self) -> None:
        self.write_lock(pid=os.getpid(), hostname=socket.gethostname())
        before = self.lock_path.read_bytes()

        lock_info, stderr = self.read_lock_info()

        self.assertTrue(lock_info.active)
        self.assertEqual(before, self.lock_path.read_bytes())
        self.assertEqual("", stderr)

    def test_lock_taken_over_between_the_proof_and_the_unlink_survives(self) -> None:
        self.write_lock(pid=self.DEAD_PID, hostname=socket.gethostname())
        real_is_pid_running = state.is_pid_running

        def takeover(pid: int) -> bool:
            # The window a bare unlink would lose: a new runner reclaims the
            # dead lock while this reader is still proving the old owner gone.
            self.write_lock(pid=os.getpid(), hostname=socket.gethostname())
            return real_is_pid_running(pid)

        with mock.patch.object(state, "is_pid_running", takeover):
            lock_info, stderr = self.read_lock_info()

        self.assertTrue(lock_info.stale)
        self.assertTrue(self.lock_path.exists())
        self.assertEqual("", stderr)
        # Re-reading judges the lock that is actually there now.
        self.assertTrue(self.read_lock_info()[0].active)

    def test_delayed_reclaim_cannot_delete_a_replacement_live_lock(self) -> None:
        # One reclaimer is stalled between validating the dead payload and
        # unlinking it while a second reclaims the same lock and a runner
        # publishes its own over the freed path. Unserialized, the stalled
        # unlink lands on that replacement and two runners drive one plan.
        self.write_lock(pid=self.DEAD_PID, hostname=socket.gethostname())
        real_read_json = state._read_json_object_safe
        window_open = threading.Event()
        competitor_done = threading.Event()
        failures: list[BaseException] = []
        stalled_threads: list[threading.Thread] = []

        def stall_after_the_equality_read(path: Path) -> dict[str, object] | None:
            raw = real_read_json(path)
            if path == self.lock_path and threading.current_thread() in stalled_threads:
                window_open.set()
                # Under a correct mutex the competitor cannot publish inside
                # this window, so the event never arrives and the wait always
                # runs to its bound.
                competitor_done.wait(self.RECLAIM_WINDOW_SECONDS)
            return raw

        def stalled_reclaimer() -> None:
            try:
                state.read_lock_info(self.plan_dir, self.state_dir)
            except BaseException as error:  # pragma: no cover - reported below
                failures.append(error)

        def competitor() -> None:
            try:
                state.read_lock_info(self.plan_dir, self.state_dir)
                state.acquire_lock(self.plan_dir, self.state_dir)
            except BaseException as error:  # pragma: no cover - reported below
                failures.append(error)
            finally:
                competitor_done.set()

        reclaimer = threading.Thread(target=stalled_reclaimer)
        stalled_threads.append(reclaimer)
        rival = threading.Thread(target=competitor)
        buffer = io.StringIO()
        with (
            mock.patch.object(
                state, "_read_json_object_safe", stall_after_the_equality_read
            ),
            redirect_stderr(buffer),
        ):
            reclaimer.start()
            self.assertTrue(window_open.wait(10.0))
            rival.start()
            reclaimer.join(30.0)
            rival.join(30.0)

        self.assertEqual([], failures)
        self.assertFalse(reclaimer.is_alive())
        self.assertFalse(rival.is_alive())

        surviving = state.read_lock_info(self.plan_dir, self.state_dir)
        self.assertIsNotNone(surviving)
        assert surviving is not None
        self.assertTrue(surviving.active)
        self.assertEqual(os.getpid(), surviving.pid)
        with self.assertRaises(state.StateError) as raised:
            state.acquire_lock(self.plan_dir, self.state_dir)
        self.assertIn("plan is locked by pid", str(raised.exception))
        # The dead lock is removed exactly once, by whichever reclaimer held
        # the mutex; the other finds nothing left to prove stale.
        self.assertEqual(1, buffer.getvalue().count("reclaimed stale lock"))

    def test_lock_mutations_queue_behind_a_sidecar_that_outlives_the_lock(
        self,
    ) -> None:
        mutex_path = self.lock_path.parent / state.LOCK_MUTEX_FILE_NAME
        taken = threading.Event()
        held_results: list[bool] = []

        def hold_the_mutex() -> None:
            with state._plan_lock_mutex(self.lock_path) as held:
                held_results.append(held)
                taken.set()
                time.sleep(self.MUTEX_HOLD_SECONDS)

        def elapsed_while_blocked(call):
            taken.clear()
            holder = threading.Thread(target=hold_the_mutex)
            holder.start()
            self.assertTrue(taken.wait(10.0))
            started = time.monotonic()
            result = call()
            waited = time.monotonic() - started
            holder.join(10.0)
            return result, waited

        lock_info, acquire_waited = elapsed_while_blocked(
            lambda: state.acquire_lock(self.plan_dir, self.state_dir)
        )
        self.assertEqual(os.getpid(), lock_info.pid)
        self.assertGreaterEqual(acquire_waited, self.MUTEX_HOLD_SECONDS / 2)

        unlocked, unlock_waited = elapsed_while_blocked(
            lambda: state.unlock_plan(self.plan_dir, self.state_dir, force=True)
        )
        self.assertTrue(unlocked.removed)
        self.assertGreaterEqual(unlock_waited, self.MUTEX_HOLD_SECONDS / 2)

        self.assertEqual([True, True], held_results)
        # The ownership record comes and goes; the thing that orders its
        # replacement has to be a different, stable file.
        self.assertFalse(self.lock_path.exists())
        self.assertTrue(mutex_path.exists())
        self.assertNotEqual(self.lock_path, mutex_path)

    def test_release_leaves_a_replacement_lock_when_the_mutex_is_contended(
        self,
    ) -> None:
        # The run that owned the lock was taken over: a live runner published
        # its own record while this one was ending. A release that unlinks on a
        # timed-out claim deletes that record and puts two runners on one plan.
        self.write_lock(pid=os.getpid(), hostname=socket.gethostname())
        replacement = self.lock_path.read_bytes()
        holding = threading.Event()
        release_returned = threading.Event()
        held_results: list[bool] = []

        def hold_the_mutex() -> None:
            with state._plan_lock_mutex(self.lock_path) as held:
                held_results.append(held)
                holding.set()
                # Held until the release under test has returned, so the claim
                # outlives the bound by construction rather than by timing.
                release_returned.wait(30.0)

        holder = threading.Thread(target=hold_the_mutex)
        holder.start()
        buffer = io.StringIO()
        try:
            self.assertTrue(holding.wait(10.0))
            started = time.monotonic()
            with (
                mock.patch.object(
                    state, "LOCK_MUTEX_WAIT_SECONDS", self.CONTENDED_BOUND_SECONDS
                ),
                redirect_stderr(buffer),
            ):
                state.release_lock(self.plan_dir, self.state_dir)
            waited = time.monotonic() - started
        finally:
            release_returned.set()
            holder.join(10.0)

        self.assertEqual([True], held_results)
        self.assertGreaterEqual(waited, self.CONTENDED_BOUND_SECONDS)
        self.assertEqual(replacement, self.lock_path.read_bytes())
        self.assertIn("left the lock", buffer.getvalue())
        self.assertIn(str(self.lock_path), buffer.getvalue())
        # The surviving owner is live, so the plan stays single-runner.
        with self.assertRaises(state.StateError) as raised:
            state.acquire_lock(self.plan_dir, self.state_dir)
        self.assertIn("plan is locked by pid", str(raised.exception))
        # Fail-closed, not fail-always: an uncontended release still removes it.
        state.release_lock(self.plan_dir, self.state_dir)
        self.assertFalse(self.lock_path.exists())

    def test_every_lock_mutation_path_branches_on_the_mutex_claim(self) -> None:
        tree = ast.parse(inspect.getsource(state))
        mutators = {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and any(
                isinstance(child, ast.Name) and child.id == "_plan_lock_mutex"
                for child in ast.walk(node)
            )
        }
        self.assertEqual(
            {
                "_reclaim_dead_local_lock",
                "acquire_lock",
                "release_lock",
                "unlock_plan",
            },
            set(mutators) - {"_plan_lock_mutex"},
        )

        for name, node in mutators.items():
            if name == "_plan_lock_mutex":
                continue
            with self.subTest(mutator=name):
                claims = [
                    item
                    for statement in ast.walk(node)
                    if isinstance(statement, ast.With)
                    for item in statement.items
                    if isinstance(item.context_expr, ast.Call)
                    and isinstance(item.context_expr.func, ast.Name)
                    and item.context_expr.func.id == "_plan_lock_mutex"
                ]
                self.assertEqual(1, len(claims))
                bound = claims[0].optional_vars
                # Referencing the contextmanager proves nothing: the repaired
                # defect was a mutator that entered it and then mutated
                # regardless of what it yielded. The claim has to be read.
                self.assertIsInstance(bound, ast.Name)
                assert isinstance(bound, ast.Name)
                self.assertTrue(
                    any(
                        isinstance(child, ast.Name)
                        and child.id == bound.id
                        and isinstance(child.ctx, ast.Load)
                        for child in ast.walk(node)
                    ),
                    f"{name} binds the mutex claim without ever reading it",
                )


if __name__ == "__main__":
    unittest.main()
