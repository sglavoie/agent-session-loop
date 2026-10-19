"""Focused test_asl_redirect_adversarial test group 2."""

from __future__ import annotations

from .support_asl_redirect_adversarial_base import (
    _WEDGED_WORKER_SOURCE,
    PATIENCE,
    JournalTestCase,
    _Cancellation,
    _StubController,
    concurrent,
    contextlib,
    os,
    redirect_continuation,
    redirect_control,
    redirect_store_vocab,
    signal,
    subprocess,
    sys,
    target,
    threading,
    time,
    unittest,
)


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
