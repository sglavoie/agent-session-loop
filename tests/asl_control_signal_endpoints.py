"""ControlSignalTests: endpoints."""

from __future__ import annotations

from .asl_control_signal_support import (
    LIVE_WAKEUP,
    ControlSignalTestsBase,
    control_signal,
    errno,
    mock,
    os,
    socket,
    state,
    state_locks,
    threading,
)


class ControlSignalTests(ControlSignalTestsBase):
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
        # A live local pid with its exact birth is this host renamed, so the
        # other host's run is given a start time no local process has.
        self.write_lock(
            pid=os.getpid(),
            hostname="some-other-host",
            process_start="Thu Jan  1 00:00:00 1970",
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

    def test_a_refused_stop_pipe_leaks_neither_descriptors_nor_the_node(
        self,
    ) -> None:
        real_open = os.open
        opened: list[int] = []

        def record_open(path, *args, **kwargs):
            fd = real_open(path, *args, **kwargs)
            opened.append(fd)
            return fd

        with (
            mock.patch.object(control_signal.os, "open", side_effect=record_open),
            mock.patch.object(
                control_signal.os,
                "pipe",
                side_effect=OSError(errno.EMFILE, "Too many open files"),
            ),
            self.assertRaises(OSError),
            control_signal.open_wakeup_endpoint(self.state_dir, threading.Event()),
        ):
            self.fail("the endpoint must not be served without its stop pipe")

        if not opened:
            self.skipTest("this filesystem cannot host a wakeup FIFO")
        # Both the read end and the keepalive writer were open when the pipe
        # was refused; neither may outlive the failure, nor may the node.
        self.assertEqual(2, len(opened))
        for descriptor in opened:
            with self.subTest(descriptor=descriptor), self.assertRaises(OSError):
                os.fstat(descriptor)
        self.assertEqual([], list(self.state_dir.rglob("*.fifo")))

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

    def test_a_refused_second_run_leaves_the_live_runs_endpoints_serving(
        self,
    ) -> None:
        # `asl run` opens its endpoints before it tries the lock, so a second
        # starter has its own FIFOs by the time it is refused. With one shared
        # name it replaced the owner's nodes, then unlinked them on exit, and
        # the owner's lock went on naming paths nobody read.
        plan_state = state.plan_state_dir(self.plan_dir, self.state_dir)
        woken = threading.Event()
        urgent = threading.Event()
        with (
            control_signal.open_wakeup_endpoint(plan_state, woken) as wakeup,
            control_signal.open_redirect_endpoint(plan_state, urgent) as redirect,
        ):
            if wakeup is None or redirect is None:
                self.skipTest("this filesystem cannot host a wakeup FIFO")
            state_locks.acquire_lock(
                self.plan_dir,
                self.state_dir,
                wakeup_endpoint=wakeup,
                redirect_endpoint=redirect,
            )
            self.addCleanup(state_locks.release_lock, self.plan_dir, self.state_dir)

            with (
                control_signal.open_wakeup_endpoint(
                    plan_state, threading.Event()
                ) as rival_wakeup,
                control_signal.open_redirect_endpoint(
                    plan_state, threading.Event()
                ) as rival_redirect,
            ):
                self.assertNotIn(rival_wakeup, (None, wakeup))
                self.assertNotIn(rival_redirect, (None, redirect))
                with self.assertRaises(state.StateError) as refused:
                    state_locks.acquire_lock(
                        self.plan_dir,
                        self.state_dir,
                        wakeup_endpoint=rival_wakeup,
                        redirect_endpoint=rival_redirect,
                    )
                self.assertIn("plan is locked by pid", str(refused.exception))

            self.assertEqual(LIVE_WAKEUP, control_signal._deliver_wakeup(wakeup))
            self.assertTrue(woken.wait(timeout=5.0))
            self.assertEqual(LIVE_WAKEUP, self.notify())
            self.assertEqual(
                LIVE_WAKEUP,
                control_signal.notify_redirect(self.plan_dir, state_dir=self.state_dir),
            )
            self.assertTrue(urgent.wait(timeout=5.0))

    def test_an_abandoned_endpoint_is_swept_and_a_live_one_kept(self) -> None:
        plan_state = state.plan_state_dir(self.plan_dir, self.state_dir)
        plan_state.mkdir(parents=True, exist_ok=True)
        # What a run killed hard leaves behind: a published name, no reader.
        abandoned = plan_state / "control-wakeup.1-deadbeef.fifo"
        os.mkfifo(abandoned, 0o600)
        live_event = threading.Event()
        with control_signal.open_wakeup_endpoint(plan_state, live_event) as live:
            if live is None:
                self.skipTest("this filesystem cannot host a wakeup FIFO")
            self.assertFalse(abandoned.exists())
            with control_signal.open_wakeup_endpoint(
                plan_state, threading.Event()
            ) as other:
                self.assertIsNotNone(other)
                self.assertTrue(live.exists())
            self.assertEqual(LIVE_WAKEUP, control_signal._deliver_wakeup(live))
            self.assertTrue(live_event.wait(timeout=5.0))
