"""Locking tests extracted from test_asl_run_persistence."""

from __future__ import annotations

import threading
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    agent_mux_delivery,
    run_persistence,
)
from tests.support import (
    assert_no_runner_import,
)

from .asl_run_persistence_support import _identified_payload


class RunPersistenceBoundaryTests(unittest.TestCase):
    """``run_persistence`` is runner-free and reaches for nothing slow."""

    def setUp(self) -> None:
        self.source = Path(run_persistence.__file__ or "").read_text(encoding="utf-8")

    def test_run_persistence_does_not_import_the_runner(self) -> None:
        assert_no_runner_import(self, run_persistence)

    def test_nothing_slow_is_reachable_from_inside_the_lock(self) -> None:
        # The lock contract holds structurally as well as behaviourally: the
        # module cannot hold its lock across a mailbox read, a lease read, a
        # sleep, or a subprocess, because it never performs one anywhere. The
        # words survive only in the prose that states the contract.
        for reach in ("subprocess.", "time.sleep(", "mailbox.", "leases.", "locks."):
            self.assertNotIn(reach, self.source, msg=reach)

    def test_the_lock_contract_is_restated_in_the_module_docstring(self) -> None:
        docstring = run_persistence.__doc__ or ""
        self.assertIn("outermost lock every durable write below takes", docstring)
        self.assertIn(
            "never held\n  across a mailbox read, a lease read, a sleep, or a "
            "subprocess",
            docstring,
        )

    def test_the_handle_satisfies_the_transport_persistence_protocol(self) -> None:
        # `agent_mux_delivery` types this handle structurally so neither side
        # imports the other; if that ever stopped holding, the notify seam
        # would only fail at runtime.
        handle: agent_mux_delivery.StatePersistence = run_persistence._RunPersistence()
        self.assertIsNotNone(handle.lock)


class RunPersistenceLockTests(unittest.TestCase):
    """The lock is reentrant, serializing, and released before any transport."""

    def test_the_lock_is_reentrant_because_a_write_may_reach_another(self) -> None:
        persistence = run_persistence._RunPersistence()
        self.assertIsInstance(persistence.lock, type(threading.RLock()))
        with persistence.lock:
            # Sequence allocation bumps the counter and writes state within a
            # single hold; a plain mutex would self-deadlock right here.
            self.assertTrue(persistence.lock.acquire(blocking=False))
            persistence.lock.release()

    def test_reentrancy_does_not_leak_the_hold_to_another_thread(self) -> None:
        persistence = run_persistence._RunPersistence()
        with persistence.lock:
            persistence.lock.acquire()
            try:
                self.assertTrue(_lock_is_held_elsewhere(persistence.lock))
            finally:
                persistence.lock.release()
            self.assertTrue(_lock_is_held_elsewhere(persistence.lock))
        self.assertFalse(_lock_is_held_elsewhere(persistence.lock))

    def test_concurrent_state_writes_are_serialized(self) -> None:
        persistence = run_persistence._RunPersistence()
        overlapping: list[bool] = []
        inside = threading.Event()
        depth = 0

        def slow_write(payload: dict[str, Any]) -> None:
            nonlocal depth
            depth += 1
            overlapping.append(depth > 1)
            inside.set()
            # Long enough that a second writer would overlap if the lock were
            # not taken around the whole write.
            for _ in range(2000):
                pass
            depth -= 1

        with mock.patch.object(
            run_persistence, "_write_state_payload", side_effect=slow_write
        ):
            threads = [
                threading.Thread(target=persistence.write_state, args=({},))
                for _ in range(4)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertTrue(inside.is_set())
        self.assertEqual([False] * 4, overlapping)

    def test_sequence_allocation_holds_the_lock_across_the_state_write(self) -> None:
        # The allocation and the write it implies are one critical section, so
        # a second producer can never persist a lower sequence behind a higher
        # one already on disk.
        persistence = run_persistence._RunPersistence()
        payload = _identified_payload()
        held: list[bool] = []

        def record_hold(_payload: dict[str, Any]) -> None:
            held.append(_lock_is_held_elsewhere(persistence.lock))

        with mock.patch.object(
            run_persistence, "_write_state_payload", side_effect=record_hold
        ):
            first = run_persistence._allocate_agent_mux_sequence(payload, persistence)
            second = run_persistence._allocate_agent_mux_sequence(payload, persistence)

        self.assertEqual([1, 2], [first, second])
        self.assertEqual(2, payload["agent_mux_sequence"])
        self.assertEqual([True, True], held)

    def test_the_notification_transport_runs_outside_the_lock(self) -> None:
        # `amx` is spawned by the transport. If the lock were still held there,
        # a slow or hung daemon would stall every thread that only wanted to
        # record what already happened.
        persistence = run_persistence._RunPersistence(write_state=False)
        payload = _identified_payload()
        held: list[bool] = []

        def fake_notify(*_args: Any, **kwargs: Any) -> None:
            kwargs["allocate_sequence"]()
            held.append(_lock_is_held_elsewhere(persistence.lock))

        with mock.patch.object(
            agent_mux_delivery, "notify", side_effect=fake_notify
        ) as notify:
            run_persistence._notify_agent_mux_loop_started(payload, persistence)

        self.assertEqual(1, notify.call_count)
        self.assertEqual([False], held)
        self.assertEqual(1, payload["agent_mux_sequence"])


def _lock_is_held_elsewhere(lock: Any) -> bool:
    """Whether ``lock`` is held, judged from a thread that does not own it.

    An ``RLock`` re-acquires for its own owner, so the question can only be
    answered from another thread.
    """
    result: list[bool] = []

    def probe() -> None:
        if lock.acquire(blocking=False):
            lock.release()
            result.append(False)
        else:
            result.append(True)

    thread = threading.Thread(target=probe)
    thread.start()
    thread.join()
    return result[0]
