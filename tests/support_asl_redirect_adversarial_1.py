"""Focused test_asl_redirect_adversarial test group 1."""

from __future__ import annotations

from .support_asl_redirect_adversarial_base import (
    PATIENCE,
    JournalTestCase,
    _Clock,
    _StubController,
    _Tools,
    concurrent,
    durability,
    mock,
    redirect_admission,
    redirect_continuation,
    redirect_control,
    redirect_store_journal,
    redirect_store_models,
    redirect_store_requests,
    redirect_store_vocab,
    target,
    threading,
    time,
)


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
