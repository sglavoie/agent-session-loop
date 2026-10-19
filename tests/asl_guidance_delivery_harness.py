# ruff: noqa: F403, F405
from .asl_guidance_delivery_support import *


class _FakePipe(io.StringIO):
    """A stream that is at EOF at once, like a harness that printed nothing."""


class _FakeProcess:
    """An in-process stand-in for the harness child.

    It stays "running" -- blocked in :meth:`wait` -- until something delivers
    guidance to it, which is how the tests below prove that a delivery landed
    inside the same live process rather than after it exited.
    """

    def __init__(self, *, exit_on_delivery: bool = True) -> None:
        self.pid = 424242
        self.returncode: int | None = None
        self.stdout = _FakePipe("")
        self.stderr = _FakePipe("")
        self.delivered: list[str] = []
        self.timed_out = False
        self._exit_on_delivery = exit_on_delivery
        self._exit = threading.Event()

    def receive(self, text: str) -> None:
        """What a real transport would do: hand the body to the live child."""
        if self.returncode is not None:
            raise AssertionError("guidance reached an exited process")
        self.delivered.append(text)
        if self._exit_on_delivery:
            self._exit.set()

    def finish(self) -> None:
        self._exit.set()

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if not self._exit.wait(WAIT_SECONDS):
            self.timed_out = True
        self.returncode = 0
        return 0

    def kill(self) -> None:
        self.finish()

    def terminate(self) -> None:
        self.finish()


class _RecordingAdapter(harnesses.HarnessAdapter):
    """A harness whose live transport hands guidance to its own process."""

    name = "codex"
    skill_prefix = "$"

    def __init__(self, *, supports_live: bool) -> None:
        super().__init__(executable="fake-harness")
        self.supports_live = supports_live
        self.process: _FakeProcess | None = None
        self.delivered_while_running: list[bool] = []

    def build_argv(self, **kwargs: Any) -> list[str]:
        return [self.executable]

    def live_delivery_transport(
        self,
        *,
        process: Any,
        invocation: harnesses.HarnessInvocation,
        transport: harnesses.HarnessTransport | None = None,
    ) -> guidance_delivery.GuidanceTransport | None:
        if not self.supports_live:
            return super().live_delivery_transport(
                process=process, invocation=invocation, transport=transport
            )

        def deliver(
            request: guidance_delivery.GuidanceRequest,
        ) -> guidance_delivery.GuidanceOutcome:
            self.delivered_while_running.append(process.poll() is None)
            process.receive(request.text)
            return guidance_delivery.DELIVERED

        return deliver


class HarnessLiveDeliverySeamTests(unittest.TestCase):
    """The endpoint is opened and closed by whoever owns the process."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.turn = turn_identity()
        self.endpoint = guidance_delivery.LiveDeliveryEndpoint(self.turn)
        # The post-turn sweep shells out to `ps`; nothing here is about
        # descendants, and the module stays spawn-free without it.
        sweep = mock.patch.object(harness_process, "sweep_enabled", return_value=False)
        sweep.start()
        self.addCleanup(sweep.stop)

    def run_turn(self, adapter: _RecordingAdapter, process: _FakeProcess) -> Any:
        adapter.process = process
        with mock.patch.object(
            harness_execution.subprocess, "Popen", return_value=process
        ) as popen:
            result = adapter.run(
                cwd=self.tmp,
                plan_dir=self.tmp,
                prompt="do the work",
                last_message_path=self.tmp / "last.txt",
                live_delivery=self.endpoint,
            )
        self.assertEqual(1, popen.call_count)
        return result

    def deliver_when_open(
        self,
        outcomes: list[guidance_delivery.GuidanceOutcome],
        *,
        text: str = "stop rewriting the tests",
    ) -> threading.Thread:
        """Submit exactly like the poller does, once the turn is live."""

        def poll() -> None:
            deadline = time.monotonic() + WAIT_SECONDS
            while not self.endpoint.opened and time.monotonic() < deadline:
                time.sleep(0.005)
            outcomes.append(self.endpoint.submit(request_for(self.turn, text=text)))

        thread = threading.Thread(target=poll)
        thread.start()
        return thread

    def test_guidance_reaches_the_live_process_of_the_exact_turn(self) -> None:
        adapter = _RecordingAdapter(supports_live=True)
        process = _FakeProcess()
        outcomes: list[guidance_delivery.GuidanceOutcome] = []
        poller = self.deliver_when_open(outcomes)

        result = self.run_turn(adapter, process)
        poller.join(WAIT_SECONDS)

        self.assertFalse(process.timed_out)
        self.assertEqual(0, result.returncode)
        self.assertEqual(["stop rewriting the tests"], process.delivered)
        # The transport ran while the child was still running, which is the
        # whole claim `delivered-live` makes.
        self.assertEqual([True], adapter.delivered_while_running)
        self.assertTrue(outcomes[0].delivered)

    def test_the_endpoint_is_closed_once_the_process_is_gone(self) -> None:
        adapter = _RecordingAdapter(supports_live=True)
        process = _FakeProcess(exit_on_delivery=False)
        process.finish()

        self.run_turn(adapter, process)

        self.assertTrue(self.endpoint.closed)
        outcome = self.endpoint.submit(request_for(self.turn))
        self.assertEqual(guidance_delivery.REASON_STALE_TURN, outcome.reason)
        self.assertEqual([], process.delivered)

    def test_the_endpoint_is_closed_when_the_turn_fails(self) -> None:
        adapter = _RecordingAdapter(supports_live=True)
        process = _FakeProcess(exit_on_delivery=False)
        process.finish()

        with (
            mock.patch.object(
                harness_execution.subprocess, "Popen", return_value=process
            ),
            mock.patch.object(
                harnesses.HarnessAdapter,
                "output_processor",
                side_effect=RuntimeError("processor exploded"),
            ),
            self.assertRaises(RuntimeError),
        ):
            adapter.run(
                cwd=self.tmp,
                plan_dir=self.tmp,
                prompt="do the work",
                last_message_path=self.tmp / "last.txt",
                live_delivery=self.endpoint,
            )

        self.assertTrue(self.endpoint.closed)

    def test_a_harness_that_never_starts_leaves_the_endpoint_unopened(self) -> None:
        adapter = _RecordingAdapter(supports_live=True)

        with (
            mock.patch.object(
                harness_execution.subprocess,
                "Popen",
                side_effect=FileNotFoundError("no such executable"),
            ),
            self.assertRaises(harnesses.HarnessError),
        ):
            adapter.run(
                cwd=self.tmp,
                plan_dir=self.tmp,
                prompt="do the work",
                last_message_path=self.tmp / "last.txt",
                live_delivery=self.endpoint,
            )

        self.assertFalse(self.endpoint.opened)
        self.assertEqual(
            guidance_delivery.REASON_NO_ACTIVE_TURN,
            self.endpoint.submit(request_for(self.turn)).reason,
        )

    def test_an_adapter_without_a_transport_defers_honestly(self) -> None:
        adapter = _RecordingAdapter(supports_live=False)
        process = _FakeProcess(exit_on_delivery=False)
        outcomes: list[guidance_delivery.GuidanceOutcome] = []
        poller = self.deliver_when_open(outcomes)
        threading.Timer(0.05, process.finish).start()

        self.run_turn(adapter, process)
        poller.join(WAIT_SECONDS)

        self.assertEqual([], process.delivered)
        self.assertFalse(outcomes[0].delivered)
        self.assertEqual(
            guidance_delivery.REASON_HARNESS_UNSUPPORTED,
            outcomes[0].reason,
        )

    def test_a_turn_without_an_endpoint_still_runs(self) -> None:
        adapter = _RecordingAdapter(supports_live=True)
        process = _FakeProcess(exit_on_delivery=False)
        process.finish()

        with mock.patch.object(
            harness_execution.subprocess, "Popen", return_value=process
        ):
            result = adapter.run(
                cwd=self.tmp,
                plan_dir=self.tmp,
                prompt="do the work",
                last_message_path=self.tmp / "last.txt",
            )

        self.assertEqual(0, result.returncode)


__all__ = tuple(name for name in globals() if not name.startswith("__"))
