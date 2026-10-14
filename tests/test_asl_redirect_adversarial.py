"""Adversarial integration: the ten ways urgent correction is asked to fail.

Every earlier session proved its own half. This module is the release review's
own evidence, and it is organized by *fault* rather than by module, because the
claims the feature makes are claims about what survives — a stalled disk, a
wedged pipe, two threads arriving at one latch, a pid that now belongs to
something else, a repository that could not be read, a supervisor that died
mid-correction.

Ten groups, one per fault named in the release contract:

* **slow storage** — the deadline is measured on a clock, not on a write, so a
  stalled acceptance append spends only the decision grace and kills nothing;
  when the append finally lands the escalation still happens, against the
  deadline it missed rather than a fresh one. A stalled journal *read* likewise
  never delays a settlement, because the drain holds no latch while reading.
* **blocked pipes** — a real worker wedged writing into a pipe nobody drains,
  behind a protocol interrupt that never returns, is still stopped at the
  deadline and is still proven stopped afterwards.
* **completion races** — settlement and acceptance contend for one latch many
  times over, and the two outcomes are the only two that ever occur.
* **concurrent feedback** — many producers appending to one journal at once
  keep a total order, and a retry under that contention adds no second record.
* **stale process identities** — a pid that now names something else is
  reported reused rather than surviving, and a pid whose identity cannot be
  read at all blocks a successor instead of being assumed gone.
* **partial multi-repository work** — an attempt that could only observe some
  of the workspace never has its evidence merged into a verdict it did not
  earn, and a repository nothing re-observed keeps the earlier observation.
* **review repairs** — a run's own audit is not redirectable, and a correction
  aimed at a session does not follow selection into a repair child or a review.
* **stopped supervisors** — a dead owner's accepted correction is stranded
  rather than re-answered, a live foreign owner is left entirely alone, and a
  queued stop beats an automatic continuation.
* **missing history** — every unusable conversation state stops for a person,
  and the fresh-conversation authorization is spent exactly once.
* **expired submissions** — expiry is durable, and an acceptance racing it
  never produces both answers.

The deterministic groups pin correctness and are safe under contention; the
live-process group is the only one that touches a real pid, and every wait in
it is bounded so a broken controller fails an assertion instead of hanging the
suite.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    clock,
    continuation_guards,
    conversation_resume,
    conversations,
    durability,
    events,
    plans_payloads,
    plans_session_ids,
    process_tree,
    redirect_admission,
    redirect_continuation,
    redirect_control,
    redirect_recovery,
    redirect_store_journal,
    redirect_store_models,
    redirect_store_requests,
    redirect_store_vocab,
)

from .support import (
    _write_lock_file,
    _write_state_payload,
    live_redirect_endpoint,
    write_plan,
)
from .test_asl_continuation_guards import registered_guard
from .test_asl_conversation_resume import ContinuationTestBase

#: Every cross-thread wait is bounded by this, so a fault the code fails to
#: handle shows up as a failed assertion rather than as a hung shard.
PATIENCE = 10.0

RUN_ID = "run-adversarial-1"
SESSION_ID = "7"
FEEDBACK = "the column you are parsing is the wrong one; re-read the fixture"


def target(
    turn_index: int = 1,
    *,
    run_id: str = RUN_ID,
    session_id: str = SESSION_ID,
    harness: str | None = "codex",
) -> redirect_store_models.RedirectTarget:
    return redirect_store_models.RedirectTarget(
        run_id=run_id,
        session_id=session_id,
        turn_index=turn_index,
        harness=harness,
    )


class _Clock:
    """A monotonic clock the test advances by hand."""

    def __init__(self, start: float = 1000.0) -> None:
        self._lock = threading.Lock()
        self._value = start

    def __call__(self) -> float:
        with self._lock:
            return self._value

    def advance(self, delta: float) -> None:
        with self._lock:
            self._value += delta


class _Cancellation:
    """Structurally :class:`harnesses.AbortSignal`, with its waits observable."""

    def __init__(self, *, completed: bool = False) -> None:
        self.armed: list[tuple[str, float]] = []
        self._done = threading.Event()
        if completed:
            self._done.set()

    def arm(self, reason: str, *, grace_seconds: float = 0.0) -> None:
        self.armed.append((reason, grace_seconds))

    @property
    def completed(self) -> bool:
        return self._done.is_set()

    def wait_completed(self, timeout: float | None = None) -> bool:
        return self._done.wait(timeout)

    def collect(self) -> None:
        self._done.set()


class _Tools:
    """Recording stand-ins for the four process operations."""

    def __init__(self, *, confirmation: dict | None = None) -> None:
        self._lock = threading.Lock()
        self.force_stopped: list[int] = []
        self._confirmation = confirmation or {
            "pgid": 4242,
            "group": process_tree.GROUP_STOPPED,
            "remaining_pids": [],
            "pid_reused": [],
            "unverified_pids": [],
            "ownership_unknown": False,
            "complete": True,
            "incomplete_reason": None,
        }

    def collect_evidence(self, root_pid: int, **kwargs) -> dict:
        return {
            "root_pid": root_pid,
            "root_pgid": root_pid,
            "entries": [],
            "escaped_pids": [],
            "collected": True,
            "disabled": None,
            "sample_error": None,
        }

    def force_stop(self, pgid: int, **kwargs) -> dict:
        with self._lock:
            self.force_stopped.append(pgid)
        return {
            "pgid": pgid,
            "signal": "SIGKILL",
            "signalled": True,
            "already_gone": False,
            "refused": None,
            "error": None,
        }

    def terminate_escapees(self, evidence, **kwargs) -> dict:
        return {
            "policy": "identity_checked_sigkill",
            "candidate_pids": [],
            "killed": [],
        }

    def confirm(self, *, pgid: int, evidence, **kwargs) -> dict:
        return dict(self._confirmation)

    def bundle(self) -> redirect_control.TerminationTools:
        return redirect_control.TerminationTools(
            collect_evidence=self.collect_evidence,
            force_stop=self.force_stop,
            terminate_escapees=self.terminate_escapees,
            confirm=self.confirm,
        )


class JournalTestCase(unittest.TestCase):
    """A plan directory with a real redirect journal on a real filesystem."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.plan_dir = root / "plan"
        self.state_dir = root / "state"
        write_plan(self.plan_dir, ("pending",))

    def submit(
        self,
        operation_id: str | None = "op-1",
        *,
        turn_index: int = 1,
        feedback: str = FEEDBACK,
        ttl_seconds: int | None = None,
        now=None,
        session_id: str = SESSION_ID,
    ) -> redirect_store_models.RedirectRequest:
        return redirect_store_requests.submit_request(
            self.plan_dir,
            target=target(turn_index, session_id=session_id),
            feedback=feedback,
            state_dir=self.state_dir,
            operation_id=operation_id,
            ttl_seconds=ttl_seconds,
            now=now,
        )

    def journal(self) -> redirect_store_models.RedirectJournalView:
        return redirect_store_journal.read_journal(
            self.plan_dir, state_dir=self.state_dir
        )

    def raw_records(self) -> list[dict]:
        path = redirect_store_journal.journal_path_for_plan(
            self.plan_dir, self.state_dir
        )
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]


# ---------------------------------------------------------------------------
# 1. Slow storage
# ---------------------------------------------------------------------------


class SlowStorageTests(JournalTestCase):
    """A disk that will not answer must not move a deadline or lose a record."""

    def _controller(
        self, clock_source: _Clock, tools: _Tools, **kwargs
    ) -> redirect_control.UrgentController:
        controller = redirect_control.UrgentController(
            worker=redirect_control.OwnedWorker.session_leader(4242),
            cancellation=kwargs.pop("cancellation", None),
            window_seconds=2.0,
            poll_seconds=0.005,
            decision_grace_seconds=kwargs.pop("decision_grace_seconds", 0.05),
            evidence_wait_seconds=0.5,
            collection_wait_seconds=0.05,
            monotonic=clock_source,
            tools=tools.bundle(),
            environ={},
            **kwargs,
        )
        self.addCleanup(controller.join, PATIENCE)
        self.addCleanup(controller.release)
        return controller

    def test_an_acceptance_stalled_in_the_journal_kills_nothing_and_then_does(
        self,
    ) -> None:
        # Session 10 left ``DECISION_GRACE_SECONDS`` as a judgement it could not
        # reproduce against a real filesystem. This reproduces it: the append is
        # a real journal write held open inside the store, so the watchdog
        # genuinely reaches its deadline with the acceptance undecided.
        self.submit("op-slow")
        released = threading.Event()
        entered = threading.Event()
        real_accept = redirect_store_requests.accept_request

        def stalled_accept(*args, **kwargs):
            entered.set()
            released.wait(PATIENCE)
            return real_accept(*args, **kwargs)

        clock_source = _Clock()
        tools = _Tools()
        controller = self._controller(clock_source, tools)
        accepted: list[redirect_store_models.RedirectRequest] = []

        def accept() -> None:
            with mock.patch.object(
                redirect_store_requests, "accept_request", side_effect=stalled_accept
            ):
                accepted.append(
                    controller.accept(
                        self.plan_dir,
                        "op-slow",
                        target=target(),
                        state_dir=self.state_dir,
                    )
                )

        worker = threading.Thread(target=accept, daemon=True)
        worker.start()
        self.addCleanup(worker.join, PATIENCE)
        self.assertTrue(entered.wait(PATIENCE), "the append never started")

        clock_source.advance(2.0)
        self.assertTrue(controller.wait_escalated(PATIENCE))
        # Nothing was promised, so nothing was stopped.
        self.assertEqual([], tools.force_stopped)
        self.assertIsNone(controller.termination)
        self.assertEqual(
            redirect_control.SKIP_UNDECIDED, controller.to_payload()["skipped"]
        )

        released.set()
        worker.join(PATIENCE)
        self.assertEqual(1, len(accepted), "the stalled append never completed")
        self.assertTrue(controller.wait_escalated(PATIENCE))
        report = controller.termination
        self.assertIsNotNone(report)
        self.assertTrue(report.forced)
        self.assertEqual([4242], tools.force_stopped)
        # The window is not refunded for the time the disk spent: the deadline on
        # the report is the one the escalation missed.
        self.assertEqual(1002.0, report.deadline)
        self.assertGreaterEqual(report.escalated_at, report.deadline)
        # And the record that made it binding is in the journal exactly once.
        self.assertEqual(
            1,
            sum(
                1
                for record in self.raw_records()
                if record.get("kind") == redirect_store_vocab.KIND_ACCEPTED
            ),
        )

    def test_a_stalled_journal_read_never_delays_the_turn_settling(self) -> None:
        # The drain reads outside the latch on purpose. A journal on a wedged
        # filesystem would otherwise hold every turn's completion hostage.
        self.submit("op-read")
        reading = threading.Event()
        release = threading.Event()

        def stalled_pending(plan_dir, *, target, state_dir=None):
            reading.set()
            release.wait(PATIENCE)
            return redirect_admission.pending_redirects(
                plan_dir, target=target, state_dir=state_dir
            )

        wakeup = threading.Event()
        gate = redirect_continuation.TurnRedirect(
            plan_dir=self.plan_dir,
            target=target(),
            state_dir=self.state_dir,
            wakeup=wakeup,
            build_controller=lambda _worker: _StubController(),
            read_pending=stalled_pending,
            poll_seconds=0.01,
            bind_wait_seconds=0.05,
            report_wait_seconds=0.2,
        )
        self.addCleanup(release.set)
        self.addCleanup(gate.close)
        gate.bind_worker(4242)
        gate.start()
        self.assertTrue(reading.wait(PATIENCE), "the drain never read the journal")

        started = time.monotonic()
        clean = gate.settle()
        elapsed = time.monotonic() - started

        self.assertTrue(clean, "a turn nothing redirected settled dirty")
        self.assertLess(elapsed, 1.0, "the settlement waited for the stalled read")

        release.set()
        # When the read finally answers, the drain finds the turn settled and
        # stops before offering anything, so the late answer neither accepts the
        # correction against a finished turn nor settles it: it stays submitted,
        # which is what leaves it to its own deadline or to the next run's
        # reconciliation rather than to a race.
        gate.close()
        self.assertEqual([], list(gate.accepted))
        self.assertFalse(gate.redirected)
        self.assertEqual(
            redirect_store_vocab.STATE_SUBMITTED,
            self.journal().request("op-read").state,
        )

    def test_a_slow_fsync_still_orders_concurrent_submissions(self) -> None:
        # Durability is what makes the journal the authority, and it is also the
        # slowest thing on the submission path. Under a deliberately slow
        # ``fullsync`` the records still come out totally ordered and distinct.
        real_fullsync = durability.fullsync

        def slow_fullsync(*args, **kwargs):
            time.sleep(0.02)
            return real_fullsync(*args, **kwargs)

        ids = tuple(f"op-sync-{index}" for index in range(8))
        with (
            mock.patch.object(
                redirect_store_journal.durability, "fullsync", side_effect=slow_fullsync
            ),
            concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool,
        ):
            results = list(
                pool.map(
                    lambda operation_id: self.submit(
                        operation_id, feedback=f"correction {operation_id}"
                    ),
                    ids,
                )
            )

        view = self.journal()
        self.assertEqual(set(ids), {request.operation_id for request in view.requests})
        sequences = sorted(request.submitted_seq for request in results)
        self.assertEqual(sequences, sorted(set(sequences)), "two records shared a seq")
        self.assertEqual(len(ids), len(self.raw_records()))


class _StubController:
    """A controller that records nothing and stops nothing."""

    def __init__(self) -> None:
        self.released = 0

    def accept(self, plan_dir, operation_id, *, target, state_dir=None):
        return redirect_admission.accept_redirect(
            plan_dir, operation_id, target=target, state_dir=state_dir
        )

    def release(self) -> None:
        self.released += 1

    def wait_escalated(self, timeout: float | None = None) -> bool:
        return True

    def require_successor_allowed(self):
        return None

    def to_payload(self) -> dict:
        return {"phase": "idle", "operation_id": None}


# ---------------------------------------------------------------------------
# 2. Blocked pipes
# ---------------------------------------------------------------------------


# A worker that wedges writing into a pipe nobody drains. Once the pipe buffer
# fills the process is blocked in ``write`` and will answer no protocol call,
# read no stdin, and notice no cooperative cancellation -- which is the exact
# condition forced termination exists for.
_WEDGED_WORKER_SOURCE = """
import sys, time
chunk = "x" * 65536
while True:
    sys.stdout.write(chunk)
    sys.stdout.flush()
    time.sleep(0.001)
"""


@unittest.skipUnless(sys.platform in ("darwin", "linux"), "needs POSIX process groups")
class BlockedPipeTests(unittest.TestCase):
    """A worker nothing can talk to is still stopped, and still proven stopped."""

    def setUp(self) -> None:
        self.worker = subprocess.Popen(
            [sys.executable, "-c", _WEDGED_WORKER_SOURCE],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
        self.addCleanup(self._reap)
        # Never read stdout: the pipe fills and the child blocks in write.
        self._wait_until_blocked()

    def _wait_until_blocked(self) -> None:
        # A 64 KiB pipe buffer fills in well under a second; the assertion below
        # is that the worker is still there, not that it is provably in ``write``.
        deadline = time.monotonic() + PATIENCE
        while time.monotonic() < deadline:
            if self.worker.poll() is None:
                time.sleep(0.2)
                break
            self.fail("the wedged worker exited before the test began")

    def _reap(self) -> None:
        with contextlib.suppress(OSError):
            os.killpg(self.worker.pid, signal.SIGKILL)
        if self.worker.stdout is not None:
            self.worker.stdout.close()
        try:
            self.worker.wait(timeout=PATIENCE)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            self.worker.kill()
            self.worker.wait(timeout=PATIENCE)

    def test_the_deadline_lands_through_a_wedged_pipe_and_a_wedged_interrupt(
        self,
    ) -> None:
        wedged = threading.Event()
        self.addCleanup(wedged.set)

        class WedgedTransport:
            def interrupt(self) -> bool:
                wedged.wait(PATIENCE * 2)
                return False

        cancellation = _Cancellation()
        controller = redirect_control.UrgentController(
            worker=redirect_control.OwnedWorker.session_leader(self.worker.pid),
            cancellation=cancellation,
            transport=WedgedTransport(),
            window_seconds=0.3,
            poll_seconds=0.01,
            evidence_wait_seconds=PATIENCE,
            collection_wait_seconds=0.1,
        )
        self.addCleanup(controller.join, PATIENCE)
        self.addCleanup(controller.release)

        controller.arm(operation_id="op-wedged")
        controller.confirm()
        # The controller is still readable while the interrupt is wedged: the
        # protocol call runs on its own thread and holds no lock.
        self.assertEqual("op-wedged", controller.to_payload()["operation_id"])

        self.assertTrue(controller.wait_escalated(PATIENCE))
        report = controller.termination
        self.assertIsNotNone(report)
        self.assertTrue(report.forced, report.to_payload())
        self.assertTrue(report.group_stop["signalled"], report.group_stop)
        self.assertTrue(report.evidence["collected"], report.evidence)

        # The kill really landed on a process that could not cooperate.
        if self.worker.stdout is not None:
            self.worker.stdout.close()
        self.assertEqual(-signal.SIGKILL, self.worker.wait(timeout=PATIENCE))

        # A successor is refused until the turn's owner reports the worker
        # reaped, and permitted once it does -- the kill alone is not the proof.
        with self.assertRaises(redirect_control.SuccessorBlockedError) as caught:
            controller.require_successor_allowed()
        self.assertEqual("predecessor-uncollected", caught.exception.reason)

        cancellation.collect()
        cleared = controller.require_successor_allowed()
        self.assertIsNotNone(cleared)
        self.assertTrue(cleared.permits_continuation, cleared.to_payload())


# ---------------------------------------------------------------------------
# 3. Completion races
# ---------------------------------------------------------------------------


class CompletionRaceTests(JournalTestCase):
    """Settlement and acceptance contend, repeatedly, and only two outcomes occur."""

    #: Enough repetitions that an unserialized latch loses at least once, few
    #: enough that the group stays under a second on a loaded machine.
    ROUNDS = 40

    def _race_one_round(self, turn_index: int) -> str:
        """Settle and accept one turn from two threads, and name who won."""
        operation_id = f"op-race-{turn_index}"
        request = self.submit(operation_id, turn_index=turn_index)
        gate = redirect_continuation.TurnRedirect(
            plan_dir=self.plan_dir,
            target=target(turn_index),
            state_dir=self.state_dir,
            build_controller=lambda _worker: _StubController(),
            poll_seconds=0.01,
            bind_wait_seconds=PATIENCE,
            report_wait_seconds=0.1,
        )
        gate.bind_worker(4242)
        start = threading.Barrier(2)
        accepted: list[object] = []
        clean: list[bool] = []

        def do_accept() -> None:
            start.wait(PATIENCE)
            accepted.append(gate.accept(request))

        def do_settle() -> None:
            start.wait(PATIENCE)
            clean.append(gate.settle())

        threads = [
            threading.Thread(target=do_accept, daemon=True),
            threading.Thread(target=do_settle, daemon=True),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(PATIENCE)
            self.assertFalse(thread.is_alive(), "the latch deadlocked")
        gate.close()

        journal_state = self.journal().request(operation_id).state
        if accepted[0] is None:
            # The settlement won: the turn is clean and the request is left
            # submitted, so it answers to its own deadline instead.
            self.assertTrue(clean[0])
            self.assertEqual(redirect_store_vocab.STATE_SUBMITTED, journal_state)
            self.assertEqual(
                [redirect_continuation.REFUSE_TURN_SETTLED],
                [record["reason"] for record in gate.to_payload()["refused"]],
            )
            return "settled-first"
        # The acceptance won: the record is durable and the attempt has
        # forfeited its completion claim.
        self.assertFalse(clean[0])
        self.assertEqual(redirect_store_vocab.STATE_ACCEPTED, journal_state)
        self.assertTrue(gate.redirected)
        return "accepted-first"

    def test_exactly_one_of_the_settlement_and_the_acceptance_wins(self) -> None:
        outcomes = [self._race_one_round(index + 1) for index in range(self.ROUNDS)]

        self.assertEqual(self.ROUNDS, len(outcomes))
        self.assertLessEqual(
            set(outcomes),
            {"settled-first", "accepted-first"},
            "a round reached a third outcome",
        )

    def test_a_correction_is_never_accepted_twice_by_racing_drains(self) -> None:
        # Two drains and a settlement all reading one journal: the store's
        # acceptance is idempotent, so the worst case is one record, never two.
        request = self.submit("op-double")
        gate = redirect_continuation.TurnRedirect(
            plan_dir=self.plan_dir,
            target=target(),
            state_dir=self.state_dir,
            build_controller=lambda _worker: _StubController(),
            poll_seconds=0.01,
            bind_wait_seconds=PATIENCE,
            report_wait_seconds=0.1,
        )
        self.addCleanup(gate.close)
        gate.bind_worker(4242)
        start = threading.Barrier(3)

        def accept() -> None:
            start.wait(PATIENCE)
            gate.accept(request)

        threads = [threading.Thread(target=accept, daemon=True) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(PATIENCE)

        accepted_records = [
            record
            for record in self.raw_records()
            if record.get("kind") == redirect_store_vocab.KIND_ACCEPTED
        ]
        self.assertEqual(1, len(accepted_records), accepted_records)
        self.assertEqual(
            ["op-double"], [record.operation_id for record in gate.accepted][:1]
        )


# ---------------------------------------------------------------------------
# 4. Concurrent feedback
# ---------------------------------------------------------------------------


class ConcurrentFeedbackTests(JournalTestCase):
    """Many producers, one journal, and the operator's order preserved."""

    def test_concurrent_submissions_keep_a_total_order_and_every_record(self) -> None:
        ids = tuple(f"op-concurrent-{index:02d}" for index in range(12))
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            requests = list(
                pool.map(
                    lambda operation_id: self.submit(
                        operation_id, feedback=f"correction {operation_id}"
                    ),
                    ids,
                )
            )

        view = self.journal()
        self.assertEqual(set(ids), {request.operation_id for request in view.requests})
        sequences = [request.submitted_seq for request in requests]
        self.assertEqual(len(set(sequences)), len(sequences))
        # Sequence order and journal order are the same order.
        by_sequence = sorted(requests, key=lambda request: request.submitted_seq)
        self.assertEqual(
            [request.operation_id for request in by_sequence],
            [
                record["id"]
                for record in self.raw_records()
                if record.get("kind") == redirect_store_vocab.KIND_SUBMITTED
            ],
        )

    def test_a_retry_under_contention_adds_no_second_record(self) -> None:
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results = list(
                pool.map(lambda _index: self.submit("op-retry"), range(6)),
            )

        self.assertEqual({"op-retry"}, {request.operation_id for request in results})
        self.assertEqual(1, len(self.raw_records()))
        self.assertEqual(1, len({request.submitted_seq for request in results}))

    def test_coalescing_presents_concurrent_corrections_in_journal_order(self) -> None:
        # The operator wrote the second knowing what the first said, so the
        # prompt must show them in the order the journal recorded, whatever
        # order the threads happened to finish in.
        ids = tuple(f"op-order-{index}" for index in range(6))
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda operation_id: self.submit(operation_id), ids))

        view = self.journal()
        expected = [
            request.operation_id
            for request in sorted(
                view.requests, key=lambda request: request.submitted_seq
            )
        ]
        from agent_session_loop import redirect_ack

        candidates = redirect_ack.coalesced_candidates(
            view, continuation=target(turn_index=2)
        )

        self.assertEqual(tuple(expected), candidates)


# ---------------------------------------------------------------------------
# 5. Stale process identities
# ---------------------------------------------------------------------------


@unittest.skipUnless(sys.platform in ("darwin", "linux"), "needs a real process table")
class StaleProcessIdentityTests(unittest.TestCase):
    """A pid is not an identity, and a probe that cannot tell is not a clearance."""

    def setUp(self) -> None:
        self.child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(self._reap)

    def _reap(self) -> None:
        with contextlib.suppress(OSError):
            self.child.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.child.wait(timeout=PATIENCE)

    def test_a_pid_that_now_names_something_else_is_reused_not_surviving(self) -> None:
        # The recorded birth belongs to a process that is gone; the pid is live
        # but is somebody else's. That must not block a successor.
        evidence = {
            "root_pid": os.getpid(),
            "collected": True,
            "entries": [
                {
                    "pid": self.child.pid,
                    "birth": "Mon Jan  1 00:00:00 1990",
                    "command": "something that no longer exists",
                }
            ],
            "escaped_pids": [],
        }

        confirmation = process_tree.confirm_termination(
            pgid=self.child.pid,
            evidence=evidence,
            killpg=lambda pgid, sig: (_ for _ in ()).throw(ProcessLookupError()),
        )

        self.assertEqual([self.child.pid], confirmation["pid_reused"])
        self.assertEqual([], confirmation["remaining_pids"])
        self.assertTrue(confirmation["complete"], confirmation)

    def test_a_pid_whose_identity_cannot_be_read_blocks_rather_than_clears(
        self,
    ) -> None:
        # No recorded birth means no way to authenticate the live pid. "We could
        # not tell" is reported as unverified and counted against completion.
        evidence = {
            "root_pid": os.getpid(),
            "collected": True,
            "entries": [{"pid": self.child.pid, "birth": None, "command": "unknown"}],
            "escaped_pids": [],
        }

        confirmation = process_tree.confirm_termination(
            pgid=self.child.pid,
            evidence=evidence,
            killpg=lambda pgid, sig: (_ for _ in ()).throw(ProcessLookupError()),
        )

        self.assertEqual([self.child.pid], confirmation["unverified_pids"])
        self.assertEqual([self.child.pid], confirmation["remaining_pids"])
        self.assertFalse(confirmation["complete"])
        self.assertEqual(
            process_tree.INCOMPLETE_DESCENDANTS_RUNNING,
            confirmation["incomplete_reason"],
        )

    def test_a_live_descendant_under_its_recorded_identity_blocks(self) -> None:
        birth = process_tree.process_birth(self.child.pid)
        self.assertIsNotNone(birth, "this machine's ps did not report a start time")
        evidence = {
            "root_pid": os.getpid(),
            "collected": True,
            "entries": [
                {"pid": self.child.pid, "birth": birth, "command": "sleep"},
            ],
            "escaped_pids": [],
        }

        confirmation = process_tree.confirm_termination(
            pgid=self.child.pid,
            evidence=evidence,
            killpg=lambda pgid, sig: (_ for _ in ()).throw(ProcessLookupError()),
        )

        self.assertEqual([], confirmation["pid_reused"])
        self.assertEqual([], confirmation["unverified_pids"])
        self.assertEqual([self.child.pid], confirmation["remaining_pids"])
        self.assertFalse(confirmation["complete"])

    def test_an_uncollected_sample_is_never_a_clearance(self) -> None:
        confirmation = process_tree.confirm_termination(
            pgid=self.child.pid,
            evidence={"root_pid": os.getpid(), "collected": False, "entries": []},
            killpg=lambda pgid, sig: (_ for _ in ()).throw(ProcessLookupError()),
        )

        self.assertFalse(confirmation["complete"])
        self.assertEqual(
            process_tree.INCOMPLETE_EVIDENCE_UNAVAILABLE,
            confirmation["incomplete_reason"],
        )


# ---------------------------------------------------------------------------
# 6. Partial multi-repository work
# ---------------------------------------------------------------------------


class PartialMultiRepositoryTests(unittest.TestCase):
    """An attempt that saw only part of the workspace earns only part of a claim.

    The guard-record fixture is ``tests.test_asl_continuation_guards``'s, reused
    rather than rebuilt: a second fixture for the same artifact is a second
    chance to get its shape wrong, and a merge test that merges a record no
    producer emits proves nothing about any run.
    """

    def test_a_repository_nothing_re_observed_keeps_the_earlier_observation(
        self,
    ) -> None:
        prior = registered_guard(changes={"SVC": ("one.py",), "OAK": ("two.py",)})
        current = registered_guard(changes={"SVC": ("three.py",)})

        merged = continuation_guards.merge_session_write_evidence(prior, current)

        self.assertIsNotNone(merged)
        self.assertEqual(["OAK", "SVC"], sorted(merged["changed"]))
        # Nothing re-observed OAK, so its earlier observation stands unamended.
        self.assertEqual(["two.py"], merged["changed"]["OAK"]["untracked_added"])
        # SVC was re-observed, so the two attempts' paths accumulate.
        self.assertEqual(
            ["one.py", "three.py"],
            merged["changed"]["SVC"]["untracked_added"],
        )
        note = merged[continuation_guards.CONTINUATION_EVIDENCE_KEY]
        self.assertTrue(note["merged"])
        self.assertEqual(["OAK"], note["carried_repositories"])
        self.assertEqual(["SVC"], note["extended_repositories"])

    def test_an_unavailable_repository_refuses_the_merge_in_both_directions(
        self,
    ) -> None:
        clean = registered_guard(changes={"SVC": ("one.py",)})
        partial = registered_guard(
            changes={"SVC": ("two.py",)},
            unavailable={"OAK": "the checkout could not be read"},
        )

        self.assertIsNone(
            continuation_guards.merge_session_write_evidence(partial, clean),
            "an earlier attempt that could not read the workspace was carried",
        )
        self.assertIsNone(
            continuation_guards.merge_session_write_evidence(clean, partial),
            "a current attempt that could not read the workspace took a merge",
        )

    def test_a_failed_verdict_is_never_upgraded_by_a_merge(self) -> None:
        failed = registered_guard(
            changes={"OAK": ("forbidden.py",)},
            passed=False,
            violations=({"alias": "OAK", "path": "forbidden.py"},),
        )
        clean = registered_guard(changes={"SVC": ("one.py",)})

        self.assertIsNone(
            continuation_guards.merge_session_write_evidence(failed, clean),
            "a refused earlier attempt's paths were carried into a clean record",
        )
        self.assertIsNone(
            continuation_guards.merge_session_write_evidence(clean, failed),
            "a merge blurred the verdict an operator has to act on",
        )

    def test_the_authorized_aliases_are_recomputed_in_registration_order(self) -> None:
        # Each attempt touched a different repository and each claimed only its
        # own alias, so the merged claim is both -- and must be in the order the
        # workspace registers them, because that is the only order
        # ``commit_settlement`` accepts.
        prior = registered_guard(changes={"OAK": ("one.py",)})
        current = registered_guard(changes={"SVC": ("one.py",)})

        merged = continuation_guards.merge_session_write_evidence(prior, current)

        self.assertIsNotNone(merged)
        self.assertEqual(["SVC", "OAK"], merged["authorized_aliases"])

    def test_an_attempt_that_observed_nothing_new_leaves_the_record_alone(self) -> None:
        # "No earlier attempt", "not eligible" and "identical" are one answer on
        # purpose: the caller keeps the record it has in all three cases.
        same = registered_guard(changes={"SVC": ("one.py",)})

        self.assertIsNone(continuation_guards.merge_session_write_evidence(same, same))
        self.assertIsNone(continuation_guards.merge_session_write_evidence(None, same))


# ---------------------------------------------------------------------------
# 7. Review repairs
# ---------------------------------------------------------------------------


def _accepted(
    operation_id: str,
    *,
    turn_index: int = 1,
    feedback_bytes: int = 12,
    digest: str = "d",
) -> redirect_continuation.AcceptedRedirect:
    return redirect_continuation.AcceptedRedirect(
        operation_id=operation_id,
        target=target(turn_index),
        accepted_at=clock.utc_iso_now(),
        feedback_digest=digest * 64,
        feedback_bytes=feedback_bytes,
    )


class ReviewRepairTests(JournalTestCase):
    """A correction belongs to a session, and an audit belongs to nobody."""

    def _write_live_state(self, *, session_id: str, session_kind: str) -> None:
        _write_state_payload(
            self.plan_dir,
            self.state_dir,
            {
                "version": 3,
                "plan_dir": str(self.plan_dir),
                "paused": False,
                "run_id": RUN_ID,
                "active_turn": {
                    "run_id": RUN_ID,
                    "session_id": session_id,
                    "turn_index": 1,
                    "session_kind": session_kind,
                    "harness": "codex",
                },
            },
        )

    def test_a_runs_own_audit_is_not_redirectable(self) -> None:
        self._write_live_state(
            session_id="review", session_kind=plans_payloads.SESSION_KIND_REVIEW
        )
        endpoint, _event = self.enterContext(
            live_redirect_endpoint(self.plan_dir, self.state_dir)
        )
        _write_lock_file(
            self.plan_dir,
            self.state_dir,
            pid=os.getpid(),
            redirect_endpoint=endpoint,
        )

        facts = redirect_admission.inspect_target(
            self.plan_dir, state_dir=self.state_dir
        )

        self.assertFalse(facts.admissible)
        self.assertEqual(redirect_admission.REFUSE_TERMINAL_AUDIT, facts.refusal)
        self.assertEqual((), self.journal().requests, "a refusal persisted a record")

    def test_a_correction_does_not_follow_selection_into_a_repair_child(self) -> None:
        pending = redirect_continuation.PendingRedirect.after_turn(
            [_accepted("op-repair")],
            session_id=SESSION_ID,
            harness="codex",
            conversation_id="codex:thread-1",
            turn_index=1,
        )
        self.assertIsNotNone(pending)
        self.assertEqual(SESSION_ID, pending.session_id)

        # A repair child of 7 is the session id "7.1", which is a different
        # session: the correction was written about the parent's turn.
        self.assertTrue(plans_session_ids.is_valid_session_id(f"{SESSION_ID}.1"))
        self.assertNotEqual(f"{SESSION_ID}.1", pending.session_id)

        # And a second acceptance for the repair child starts its own lineage
        # rather than extending the parent's.
        child = redirect_continuation.PendingRedirect.after_turn(
            [_accepted("op-child", turn_index=2, feedback_bytes=9, digest="e")],
            session_id=f"{SESSION_ID}.1",
            harness="codex",
            conversation_id="codex:thread-2",
            turn_index=2,
            previous=pending,
        )
        self.assertEqual(1, child.attempt)
        self.assertEqual(("op-child",), child.operation_ids)

    def test_a_second_correction_on_a_corrected_turn_extends_one_lineage(self) -> None:
        first = redirect_continuation.PendingRedirect.after_turn(
            [_accepted("op-one", feedback_bytes=5, digest="a")],
            session_id=SESSION_ID,
            harness="codex",
            conversation_id="codex:thread-1",
            turn_index=1,
        )
        second = redirect_continuation.PendingRedirect.after_turn(
            [_accepted("op-two", turn_index=2, feedback_bytes=7, digest="b")],
            session_id=SESSION_ID,
            harness="codex",
            conversation_id="codex:thread-1",
            turn_index=2,
            previous=first,
        )

        self.assertEqual(2, second.attempt)
        self.assertEqual(("op-one", "op-two"), second.operation_ids)
        self.assertEqual(12, second.feedback_bytes)


# ---------------------------------------------------------------------------
# 8. Stopped supervisors
# ---------------------------------------------------------------------------


class StoppedSupervisorTests(JournalTestCase):
    """What happens to a correction whose owner stopped holding it."""

    def _write_dead_owner_lock(self) -> None:
        _write_state_payload(
            self.plan_dir,
            self.state_dir,
            {
                "version": 3,
                "plan_dir": str(self.plan_dir),
                "paused": False,
                "run_id": RUN_ID,
            },
        )
        # 2**30 is above every real pid on both supported platforms, so the
        # owner reads as gone without this test having to kill anything.
        _write_lock_file(self.plan_dir, self.state_dir, pid=2**30)

    def test_an_accepted_correction_of_a_dead_run_is_stranded_not_re_answered(
        self,
    ) -> None:
        request = self.submit("op-stranded")
        redirect_store_requests.accept_request(
            self.plan_dir,
            request.operation_id,
            target=target(),
            state_dir=self.state_dir,
        )
        self._write_dead_owner_lock()

        report = redirect_recovery.reconcile_startup(
            self.plan_dir,
            run_id="run-successor",
            pending_session_ids=(),
            state_dir=self.state_dir,
        )

        self.assertFalse(report.blocked)
        self.assertEqual(
            ["op-stranded"],
            [record.operation_id for record in report.stranded],
        )
        # First terminal wins: the dead run's own decision still stands.
        self.assertEqual(
            redirect_store_vocab.STATE_ACCEPTED,
            self.journal().request("op-stranded").state,
        )

    def test_an_unanswered_correction_of_a_dead_run_is_settled_run_ended(self) -> None:
        self.submit("op-unanswered")
        self._write_dead_owner_lock()

        report = redirect_recovery.reconcile_startup(
            self.plan_dir,
            run_id="run-successor",
            pending_session_ids=(),
            state_dir=self.state_dir,
        )

        self.assertFalse(report.blocked)
        settled = self.journal().request("op-unanswered")
        self.assertEqual(redirect_store_vocab.STATE_REJECTED, settled.state)
        self.assertEqual(redirect_store_vocab.REASON_RUN_ENDED, settled.reason)

    def test_a_live_foreign_owner_is_left_entirely_alone(self) -> None:
        self.submit("op-foreign")
        _write_state_payload(
            self.plan_dir,
            self.state_dir,
            {
                "version": 3,
                "plan_dir": str(self.plan_dir),
                "paused": False,
                "run_id": RUN_ID,
            },
        )
        _write_lock_file(
            self.plan_dir,
            self.state_dir,
            pid=1,
            hostname="some-other-machine",
        )

        report = redirect_recovery.reconcile_startup(
            self.plan_dir,
            run_id="run-successor",
            pending_session_ids=(SESSION_ID,),
            state_dir=self.state_dir,
        )

        self.assertTrue(report.blocked)
        self.assertEqual(redirect_recovery.BLOCKED_FOREIGN_OWNER, report.blocked_reason)
        # Nothing was settled: a second reconciler is how one correction
        # acquires two outcomes.
        self.assertEqual(
            redirect_store_vocab.STATE_SUBMITTED,
            self.journal().request("op-foreign").state,
        )

    def test_a_pending_session_keeps_its_correction_recoverable(self) -> None:
        self.submit("op-recoverable")
        self._write_dead_owner_lock()

        report = redirect_recovery.reconcile_startup(
            self.plan_dir,
            run_id="run-successor",
            pending_session_ids=(SESSION_ID,),
            state_dir=self.state_dir,
        )

        self.assertEqual(("op-recoverable",), report.recoverable_for(SESSION_ID))
        self.assertEqual((), report.recoverable_for("other"))
        self.assertEqual(
            redirect_store_vocab.STATE_SUBMITTED,
            self.journal().request("op-recoverable").state,
        )


# ---------------------------------------------------------------------------
# 9. Missing history
# ---------------------------------------------------------------------------


class MissingHistoryTests(ContinuationTestBase):
    """What happens to an accepted correction when the history it is about is gone.

    ``tests/test_asl_conversation_resume.py`` already pins the continuation
    decision itself for every selection state. These are the cases that only
    exist once a correction has been accepted: the operator wrote it *about* a
    conversation, so losing that conversation is losing the thing the correction
    refers to, and the three ways forward -- stop, authorized fresh start,
    harness with no continuity at all -- must each be explicit about whether the
    correction travels.
    """

    def plan(self, *, authorization=None, operations=("op-1",), **kwargs):
        return conversation_resume.plan_turn_continuation(
            self.plan_state,
            binding=self.binding(**kwargs),
            authorization=authorization or self.withheld(),
            interrupted_operations=operations,
        )

    def test_a_correction_is_never_delivered_into_a_different_conversation(
        self,
    ) -> None:
        # The conversation the correction was written about cannot be continued
        # and no fresh start was authorized. Delivering the correction anyway --
        # into a conversation that never saw the work being corrected -- is the
        # failure this pause exists to prevent.
        conversations.open_conversation(self.plan_state, self.reference("thread-7"))

        continuation = self.plan()

        self.assertEqual(
            conversation_resume.CONTINUATION_RECOVERY_REQUIRED,
            continuation.disposition,
        )
        self.assertTrue(continuation.paused)
        self.assertIsNone(continuation.resume_conversation_id)
        self.assertIsNone(continuation.intent, "a refused turn carried an intent")
        self.assertEqual(
            events.PAUSE_REASON_CONVERSATION_RECOVERY_REQUIRED,
            conversation_resume.RECOVERY_PAUSE_REASON,
        )

    def test_an_unreadable_ledger_stops_rather_than_losing_the_correction(self) -> None:
        self.confirmed("thread-7")
        with self.ledger_path.open("ab") as handle:
            handle.write(b"{not json\n")

        continuation = self.plan(authorization=self.withheld())

        self.assertTrue(continuation.paused)
        self.assertIsNone(continuation.intent)
        # The pause says what was refused and both ways forward, because the
        # operator's correction is still pending on the other side of it.
        message = conversation_resume.recovery_message(continuation)
        self.assertIn("--fresh-conversation", message)
        self.assertIn("outstanding feedback are kept", message)

    def test_an_authorized_fresh_start_still_carries_the_correction_forward(
        self,
    ) -> None:
        # Starting over discards the agent's accumulated context, which is the
        # cost the operator accepted. It must not also discard the correction:
        # the ids travel into the fresh intent so the new conversation is told
        # what it is picking up.
        conversations.open_conversation(self.plan_state, self.reference("thread-7"))
        authorization = self.granted()

        continuation = self.plan(
            authorization=authorization, operations=("op-1", "op-2")
        )

        self.assertEqual(
            conversation_resume.CONTINUATION_FRESH, continuation.disposition
        )
        self.assertTrue(authorization.consumed)
        self.assertIsNotNone(continuation.intent)
        self.assertEqual(
            ("op-1", "op-2"), tuple(continuation.intent.interrupted_operations)
        )

    def test_a_resumed_conversation_is_told_which_operations_were_cut_short(
        self,
    ) -> None:
        self.confirmed("thread-7")

        continuation = self.plan(operations=("op-1",))

        self.assertTrue(continuation.resumes)
        self.assertEqual("thread-7", continuation.resume_conversation_id)
        self.assertEqual(("op-1",), tuple(continuation.intent.interrupted_operations))

    def test_a_harness_without_continuity_carries_no_correction_through_here(
        self,
    ) -> None:
        # A harness that cannot resume is not a refusal, but it also cannot be
        # told what was interrupted, so the intent is absent rather than empty:
        # the correction reaches that worker through the prompt the run builds,
        # and a caller that read an empty intent as "nothing pending" would be
        # reading the wrong surface.
        with mock.patch.object(
            conversation_resume,
            "_harness_capability",
            return_value=conversations.HarnessCapability(
                harness="codex",
                state="unsupported",
                detail="this harness carries no conversation across turns",
            ),
        ):
            continuation = self.plan()

        self.assertEqual(
            conversation_resume.CONTINUATION_UNAVAILABLE, continuation.disposition
        )
        self.assertFalse(continuation.paused)
        self.assertFalse(continuation.captures)
        self.assertIsNone(continuation.intent)

    def test_the_ids_a_resumed_conversation_accepts_are_narrowed_not_raised(
        self,
    ) -> None:
        # The journal's id charset is wider than the ledger's and the ledger caps
        # the count, so a correction with an id the ledger would refuse must be
        # dropped from the resume hint rather than failing the turn -- the worker
        # still reads its words out of the journal.
        pending = redirect_continuation.PendingRedirect(
            session_id=SESSION_ID,
            harness="codex",
            conversation_id="codex:thread-1",
            predecessor_turn_index=1,
            attempt=1,
            operation_ids=("ok-1", "not ok: spaces and colons", "ok-2"),
            feedback_digests=("a" * 64, "b" * 64, "c" * 64),
            feedback_bytes=30,
        )

        self.assertEqual(("ok-1", "ok-2"), pending.resumable_operation_ids())
        # The full list stays on the record, so what was narrowed is visible.
        self.assertEqual(3, len(pending.to_payload()["operation_ids"]))

    def test_the_resume_hint_is_capped_at_the_ledgers_limit(self) -> None:
        count = conversations.MAX_INTERRUPTED_OPERATIONS + 5
        pending = redirect_continuation.PendingRedirect(
            session_id=SESSION_ID,
            harness="codex",
            conversation_id="codex:thread-1",
            predecessor_turn_index=1,
            attempt=1,
            operation_ids=tuple(f"op-{index}" for index in range(count)),
            feedback_digests=tuple("a" * 64 for _ in range(count)),
            feedback_bytes=count,
        )

        self.assertEqual(
            conversations.MAX_INTERRUPTED_OPERATIONS,
            len(pending.resumable_operation_ids()),
        )
        self.assertEqual(count, len(pending.operation_ids))


# ---------------------------------------------------------------------------
# 10. Expired submissions
# ---------------------------------------------------------------------------


class ExpiredSubmissionTests(JournalTestCase):
    """Expiry is a written refusal, and nothing may accept past it."""

    def test_an_expired_request_cannot_be_accepted_by_any_later_caller(self) -> None:
        created = clock.utc_now()
        request = self.submit("op-expired", ttl_seconds=1, now=created)
        later = created + timedelta(seconds=5)

        expired = redirect_store_requests.expire_due_requests(
            self.plan_dir, state_dir=self.state_dir, now=later
        )

        self.assertEqual([request.operation_id], [r.operation_id for r in expired])
        self.assertEqual(
            redirect_store_vocab.STATE_EXPIRED,
            self.journal().request("op-expired").state,
        )
        with self.assertRaises(redirect_store_models.RedirectStoreError) as caught:
            redirect_store_requests.accept_request(
                self.plan_dir,
                "op-expired",
                target=target(),
                state_dir=self.state_dir,
                now=later,
            )
        # Expiry answers before settlement does, because "its deadline passed" is
        # the more concrete thing to tell an operator than "it was already
        # settled" -- and either way the acceptance is refused.
        self.assertEqual(
            redirect_store_vocab.ERROR_REQUEST_EXPIRED, caught.exception.reason
        )

    def test_an_acceptance_past_the_deadline_expires_the_request_durably(self) -> None:
        created = clock.utc_now()
        self.submit("op-late", ttl_seconds=1, now=created)
        later = created + timedelta(seconds=5)

        with self.assertRaises(redirect_store_models.RedirectStoreError) as caught:
            redirect_store_requests.accept_request(
                self.plan_dir,
                "op-late",
                target=target(),
                state_dir=self.state_dir,
                now=later,
            )

        self.assertEqual(
            redirect_store_vocab.ERROR_REQUEST_EXPIRED, caught.exception.reason
        )
        # The refusal outlives the process that reached it.
        self.assertEqual(
            redirect_store_vocab.STATE_EXPIRED, self.journal().request("op-late").state
        )
        self.assertEqual(
            redirect_store_vocab.REASON_EXPIRED,
            self.journal().request("op-late").reason,
        )

    #: Enough rounds that both orders occur on a normal machine, few enough that
    #: the test stays well under a second.
    ROUNDS = 20

    def _expiry_race_round(self, turn_index: int, created, later) -> str:
        """Accept and expire one request at once, and name the answer it got."""
        operation_id = f"op-race-expiry-{turn_index}"
        self.submit(operation_id, turn_index=turn_index, ttl_seconds=1, now=created)
        start = threading.Barrier(2)

        def do_accept() -> None:
            start.wait(PATIENCE)
            with contextlib.suppress(redirect_store_models.RedirectStoreError):
                redirect_store_requests.accept_request(
                    self.plan_dir,
                    operation_id,
                    target=target(turn_index),
                    state_dir=self.state_dir,
                    now=created,
                )

        def do_expire() -> None:
            start.wait(PATIENCE)
            redirect_store_requests.expire_due_requests(
                self.plan_dir, state_dir=self.state_dir, now=later
            )

        threads = [
            threading.Thread(target=do_accept, daemon=True),
            threading.Thread(target=do_expire, daemon=True),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(PATIENCE)
            self.assertFalse(thread.is_alive(), "the journal lock deadlocked")

        # Exactly one terminal record, whichever answer won the lock.
        terminal = [
            record
            for record in self.raw_records()
            if record.get("id") == operation_id
            and record.get("kind")
            in (redirect_store_vocab.KIND_ACCEPTED, redirect_store_vocab.KIND_EXPIRED)
        ]
        self.assertEqual(1, len(terminal), terminal)
        return self.journal().request(operation_id).state

    def test_an_acceptance_racing_expiry_produces_one_answer_not_two(self) -> None:
        created = clock.utc_now()
        later = created + timedelta(seconds=5)

        states = [
            self._expiry_race_round(index + 1, created, later)
            for index in range(self.ROUNDS)
        ]

        self.assertEqual(self.ROUNDS, len(states))
        self.assertLessEqual(
            set(states),
            {redirect_store_vocab.STATE_ACCEPTED, redirect_store_vocab.STATE_EXPIRED},
            "a racing request reached a state neither caller wrote",
        )


if __name__ == "__main__":  # pragma: no cover - module is run by the suite driver
    unittest.main()
