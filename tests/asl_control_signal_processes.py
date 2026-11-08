"""ControlSignalTests: processes."""

from __future__ import annotations

from .asl_control_signal_support import (
    LIVE_WAKEUP,
    ControlSignalTestsBase,
    Path,
    control_queue,
    control_signal,
    events,
    json,
    process_tree,
    read_jsonl,
    signal,
    socket,
    state,
    state_locks,
    threading,
    time,
    turn_execution_constants,
)


class ControlSignalTests(ControlSignalTestsBase):
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
            turn_execution_constants.LIVE_CONTROL_POLL_INTERVAL_SECONDS,
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

    def test_control_at_lock_publication_never_kills_the_run(self) -> None:
        child, locked, _emitting = self.start_run_child()

        command = control_queue.append_command(
            self.plan_dir,
            control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT,
            state_dir=self.state_dir,
        )
        # Sent while the run still holds the lock and has not reached its loop.
        self.assertEqual(LIVE_WAKEUP, self.notify())
        self.assertIsNone(child.poll(), "the run left its lock window early")
        self.release(locked)

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
        child, locked, emitting = self.start_run_child(hold_emit=True)
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
        self.release(locked)

        self.awaitMarker(child, emitting, "the run never reached its terminal payload")
        self.assertFalse(
            lock_path.exists(), "the run was woken before releasing its lock"
        )
        self.assertFalse(Path(endpoint).exists(), "the endpoint outlived its reader")

        self.assertEqual(
            control_signal.NotifyResult(False, "endpoint-closed"),
            control_signal._deliver_wakeup(Path(endpoint)),
        )
        self.release(emitting)

        stdout, stderr = child.communicate(timeout=180)
        self.assertEqual(0, child.returncode, stderr)
        payload = json.loads(stdout)
        self.assertEqual("all-complete", payload["reason"])
        self.assertEqual([1, 2], payload["completed_sessions"])
