"""Live guidance delivery: the endpoint, the harness seam, and the poller.

Everything here runs in-process. The harness tests drive the real
``HarnessAdapter.run`` boundary with a fake process object rather than a fake
executable, so the module spawns nothing and belongs in the portable shard
while still exercising the code that owns the live process.
"""

from __future__ import annotations

import io
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    events,
    guidance_delivery,
    harnesses,
    mailbox,
    run_control,
    runner,
)
from tests.support import read_jsonl

#: Every bounded wait in this module. Long enough that a loaded machine does
#: not fail a correct implementation, short enough that a wrong one fails
#: rather than hangs.
WAIT_SECONDS = 10.0


def turn_identity(**overrides: Any) -> guidance_delivery.TurnIdentity:
    fields: dict[str, Any] = {
        "run_id": "run-1",
        "session_id": "7",
        "turn_index": 2,
        "harness": "codex",
    }
    fields.update(overrides)
    return guidance_delivery.TurnIdentity(**fields)


def request_for(
    turn: guidance_delivery.TurnIdentity,
    *,
    guidance_id: str = "11111111-1111-4111-8111-111111111111",
    seq: int = 1,
    text: str = "prefer the smaller change",
) -> guidance_delivery.GuidanceRequest:
    return guidance_delivery.GuidanceRequest(
        guidance_id=guidance_id,
        seq=seq,
        text=text,
        turn=turn,
    )


def recording_transport(
    calls: list[guidance_delivery.GuidanceRequest],
) -> guidance_delivery.GuidanceTransport:
    """A transport that always succeeds and remembers what it was handed."""

    def transport(
        request: guidance_delivery.GuidanceRequest,
    ) -> guidance_delivery.GuidanceOutcome:
        calls.append(request)
        return guidance_delivery.DELIVERED

    return transport


class DeliveryVocabularyTests(unittest.TestCase):
    """The spine restates the ledger's vocabulary; it may not drift from it.

    ``guidance_delivery`` cannot import ``mailbox`` -- the mailbox reaches
    ``harnesses`` through ``state``, and ``harnesses`` imports the spine -- so
    these assertions are what keeps the two copies identical.
    """

    def test_states_and_channel_match_the_ledger(self) -> None:
        self.assertEqual(
            mailbox.GUIDANCE_STATE_DELIVERED_LIVE,
            guidance_delivery.STATE_DELIVERED_LIVE,
        )
        self.assertEqual(
            mailbox.GUIDANCE_STATE_DEFERRED,
            guidance_delivery.STATE_DEFERRED,
        )
        self.assertEqual(
            mailbox.GUIDANCE_CHANNEL_LIVE,
            guidance_delivery.CHANNEL_LIVE,
        )

    def test_reasons_match_the_ledger(self) -> None:
        self.assertEqual(
            mailbox.GUIDANCE_REASON_DELIVERED,
            guidance_delivery.REASON_DELIVERED,
        )
        self.assertEqual(
            set(mailbox.GUIDANCE_DEFERRAL_REASONS),
            set(guidance_delivery.DEFERRAL_REASONS),
        )

    def test_every_outcome_is_a_record_the_mailbox_accepts(self) -> None:
        """The states and reasons this module produces are appendable as-is."""
        outcomes = [
            guidance_delivery.DELIVERED,
            *(
                guidance_delivery.deferred(reason)
                for reason in guidance_delivery.DEFERRAL_REASONS
            ),
        ]
        for outcome in outcomes:
            with self.subTest(reason=outcome.reason):
                self.assertIn(
                    outcome.delivery_state,
                    mailbox.GUIDANCE_DELIVERY_STATES,
                )
                self.assertIn(outcome.channel, mailbox.GUIDANCE_DELIVERY_CHANNELS)
                self.assertIn(outcome.reason, mailbox.GUIDANCE_DELIVERY_REASONS)


class GuidanceOutcomeTests(unittest.TestCase):
    def test_a_delivered_outcome_is_a_live_delivery(self) -> None:
        outcome = guidance_delivery.DELIVERED

        self.assertTrue(outcome.delivered)
        self.assertEqual(guidance_delivery.STATE_DELIVERED_LIVE, outcome.delivery_state)
        self.assertEqual(guidance_delivery.CHANNEL_LIVE, outcome.channel)
        self.assertIsNone(outcome.detail)

    def test_a_deferral_names_a_reason_and_stays_deferred(self) -> None:
        outcome = guidance_delivery.deferred(
            guidance_delivery.REASON_STALE_TURN,
            detail="turn 2 already finished",
        )

        self.assertFalse(outcome.delivered)
        self.assertEqual(guidance_delivery.STATE_DEFERRED, outcome.delivery_state)
        self.assertEqual(guidance_delivery.REASON_STALE_TURN, outcome.reason)

    def test_success_cannot_borrow_a_deferral_reason(self) -> None:
        with self.assertRaises(ValueError):
            guidance_delivery.GuidanceOutcome(
                delivered=True,
                reason=guidance_delivery.REASON_HARNESS_UNSUPPORTED,
            )

    def test_a_deferral_cannot_invent_a_reason(self) -> None:
        with self.assertRaises(ValueError):
            guidance_delivery.deferred("because")


class LiveDeliveryEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.turn = turn_identity()
        self.endpoint = guidance_delivery.LiveDeliveryEndpoint(self.turn)
        self.request = request_for(self.turn)

    def test_an_unopened_endpoint_has_no_turn_to_deliver_into(self) -> None:
        outcome = self.endpoint.submit(self.request)

        self.assertFalse(outcome.delivered)
        self.assertEqual(guidance_delivery.REASON_NO_ACTIVE_TURN, outcome.reason)

    def test_an_open_endpoint_without_a_transport_is_unsupported(self) -> None:
        self.endpoint.open(None)

        outcome = self.endpoint.submit(self.request)

        self.assertFalse(outcome.delivered)
        self.assertEqual(guidance_delivery.REASON_HARNESS_UNSUPPORTED, outcome.reason)

    def test_a_transport_receives_the_body_and_the_turn(self) -> None:
        seen: list[guidance_delivery.GuidanceRequest] = []
        self.endpoint.open(recording_transport(seen))

        outcome = self.endpoint.submit(self.request)

        self.assertTrue(outcome.delivered)
        self.assertEqual([self.request], seen)
        self.assertEqual("prefer the smaller change", seen[0].text)
        self.assertEqual(self.turn, seen[0].turn)

    def test_a_closed_endpoint_is_a_stale_turn(self) -> None:
        calls: list[guidance_delivery.GuidanceRequest] = []
        self.endpoint.open(recording_transport(calls))
        self.endpoint.close()

        outcome = self.endpoint.submit(self.request)

        self.assertFalse(outcome.delivered)
        self.assertEqual(guidance_delivery.REASON_STALE_TURN, outcome.reason)
        self.assertEqual([], calls)

    def test_reopening_a_closed_endpoint_does_nothing(self) -> None:
        """A late open must not resurrect a channel into a finished turn."""
        self.endpoint.close()
        self.endpoint.open(lambda request: guidance_delivery.DELIVERED)

        self.assertTrue(self.endpoint.closed)
        self.assertEqual(
            guidance_delivery.REASON_STALE_TURN,
            self.endpoint.submit(self.request).reason,
        )

    def test_a_request_for_another_turn_never_reaches_the_transport(self) -> None:
        calls: list[guidance_delivery.GuidanceRequest] = []
        self.endpoint.open(recording_transport(calls))

        for label, other in (
            ("turn_index", turn_identity(turn_index=3)),
            ("session_id", turn_identity(session_id="8")),
            ("run_id", turn_identity(run_id="run-2")),
            ("harness", turn_identity(harness="claude")),
        ):
            with self.subTest(field=label):
                outcome = self.endpoint.submit(request_for(other))

                self.assertFalse(outcome.delivered)
                self.assertEqual(guidance_delivery.REASON_STALE_TURN, outcome.reason)
        self.assertEqual([], calls)

    def test_a_failing_transport_defers_instead_of_raising(self) -> None:
        def explode(request: guidance_delivery.GuidanceRequest) -> Any:
            raise RuntimeError("pipe is gone")

        self.endpoint.open(explode)

        outcome = self.endpoint.submit(self.request)

        self.assertFalse(outcome.delivered)
        self.assertEqual(
            guidance_delivery.REASON_TRANSPORT_UNAVAILABLE,
            outcome.reason,
        )
        self.assertEqual("RuntimeError: pipe is gone", outcome.detail)

    def test_a_transport_that_returns_junk_defers(self) -> None:
        self.endpoint.open(lambda request: "sent")  # type: ignore[arg-type,return-value]

        outcome = self.endpoint.submit(self.request)

        self.assertFalse(outcome.delivered)
        self.assertEqual(
            guidance_delivery.REASON_TRANSPORT_UNAVAILABLE,
            outcome.reason,
        )

    def test_close_waits_for_a_delivery_already_in_flight(self) -> None:
        """The endpoint's quiescence guarantee, mirroring the reader join.

        ``close`` returning has to mean no transport is running, otherwise a
        turn could be torn down underneath one.
        """
        entered = threading.Event()
        release = threading.Event()
        left = threading.Event()

        def slow(request: guidance_delivery.GuidanceRequest) -> Any:
            entered.set()
            release.wait(WAIT_SECONDS)
            left.set()
            return guidance_delivery.DELIVERED

        self.endpoint.open(slow)
        submitted: list[guidance_delivery.GuidanceOutcome] = []
        submitter = threading.Thread(
            target=lambda: submitted.append(self.endpoint.submit(self.request))
        )
        submitter.start()
        self.assertTrue(entered.wait(WAIT_SECONDS))

        closer = threading.Thread(target=self.endpoint.close)
        closer.start()
        # The transport is still running, so the close cannot have completed.
        closer.join(0.2)
        self.assertTrue(closer.is_alive())
        self.assertFalse(left.is_set())

        release.set()
        closer.join(WAIT_SECONDS)
        submitter.join(WAIT_SECONDS)

        self.assertFalse(closer.is_alive())
        self.assertTrue(left.is_set())
        self.assertTrue(submitted[0].delivered)
        # Nothing starts after the close: the next attempt never reaches the
        # transport at all.
        self.assertEqual(
            guidance_delivery.REASON_STALE_TURN,
            self.endpoint.submit(self.request).reason,
        )

    def test_activated_closes_even_when_the_body_raises(self) -> None:
        with (
            self.assertRaises(RuntimeError),
            self.endpoint.activated(lambda request: guidance_delivery.DELIVERED),
        ):
            self.assertTrue(self.endpoint.opened)
            raise RuntimeError("turn blew up")

        self.assertTrue(self.endpoint.closed)


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

        def transport(
            request: guidance_delivery.GuidanceRequest,
        ) -> guidance_delivery.GuidanceOutcome:
            self.delivered_while_running.append(process.poll() is None)
            process.receive(request.text)
            return guidance_delivery.DELIVERED

        return transport


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
        sweep = mock.patch.object(harnesses, "sweep_enabled", return_value=False)
        sweep.start()
        self.addCleanup(sweep.stop)

    def run_turn(self, adapter: _RecordingAdapter, process: _FakeProcess) -> Any:
        adapter.process = process
        with mock.patch.object(
            harnesses.subprocess, "Popen", return_value=process
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
            mock.patch.object(harnesses.subprocess, "Popen", return_value=process),
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
                harnesses.subprocess,
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

        with mock.patch.object(harnesses.subprocess, "Popen", return_value=process):
            result = adapter.run(
                cwd=self.tmp,
                plan_dir=self.tmp,
                prompt="do the work",
                last_message_path=self.tmp / "last.txt",
            )

        self.assertEqual(0, result.returncode)


class ProductionAdapterDeferralTests(unittest.TestCase):
    """Without an open protocol conversation, no adapter claims live delivery.

    Codex gained one over the app-server; claude and copilot are still one-shot
    processes. What all three share is this floor: an adapter handed no
    transport has no live channel and must say so, so a turn whose conversation
    never opened defers instead of reporting a delivery it did not make. The
    codex transport itself is covered in tests/test_asl_codex_app_server.py.
    """

    def test_every_adapter_reports_no_live_transport(self) -> None:
        for name in harnesses.HARNESS_NAMES:
            with self.subTest(harness=name):
                adapter = harnesses.get_adapter(name)
                invocation = harnesses.HarnessInvocation(
                    name=name,
                    argv=[adapter.executable],
                    cwd=Path("/tmp"),
                )

                self.assertIsNone(
                    adapter.live_delivery_transport(
                        process=_FakeProcess(),
                        invocation=invocation,
                    )
                )

    def test_an_endpoint_opened_by_a_shipped_adapter_defers(self) -> None:
        turn = turn_identity()
        endpoint = guidance_delivery.LiveDeliveryEndpoint(turn)
        adapter = harnesses.get_adapter("codex")
        invocation = harnesses.HarnessInvocation(
            name="codex", argv=[adapter.executable], cwd=Path("/tmp")
        )

        endpoint.open(
            adapter.live_delivery_transport(
                process=_FakeProcess(), invocation=invocation
            )
        )

        outcome = endpoint.submit(request_for(turn))
        self.assertFalse(outcome.delivered)
        self.assertEqual(guidance_delivery.REASON_HARNESS_UNSUPPORTED, outcome.reason)


class MailboxPollDeliveryTests(unittest.TestCase):
    """What the live poller records, and what it refuses to record."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.plan_dir = self.tmp / "plan"
        self.state_dir = self.tmp / "state"
        self.plan_dir.mkdir()
        (self.plan_dir / "manifest.md").write_text("# m\n", encoding="utf-8")
        (self.plan_dir / "context.md").write_text("# c\n", encoding="utf-8")
        self.event_path = self.tmp / "events.jsonl"
        self.payload: dict[str, Any] = {
            "run_id": "run-1",
            "control": runner._initial_control_payload(),
        }
        self.persistence = runner._RunPersistence()
        self.poll = runner._MailboxPoll(
            plan_dir=self.plan_dir,
            state_dir_arg=str(self.state_dir),
            control_state=run_control.RunControlState(current_harness="codex"),
            seen_control_ids=set(),
            payload=self.payload,
            event_path=self.event_path,
            control_payload=dict(self.payload["control"]),
            persistence=self.persistence,
            run_control_lock=threading.Lock(),
        )
        self.turn = turn_identity()
        self.poll.set_context(
            {"session": 7, "session_id": "7", "turn_index": 2, "total_sessions": 13}
        )
        self.received: list[str] = []

    def endpoint(
        self,
        *,
        turn: guidance_delivery.TurnIdentity | None = None,
        live: bool = True,
    ) -> guidance_delivery.LiveDeliveryEndpoint:
        endpoint = guidance_delivery.LiveDeliveryEndpoint(turn or self.turn)
        endpoint.open(self.transport if live else None)
        return endpoint

    def transport(
        self, request: guidance_delivery.GuidanceRequest
    ) -> guidance_delivery.GuidanceOutcome:
        self.received.append(request.text)
        return guidance_delivery.DELIVERED

    def add_guidance(
        self, text: str = "use the smaller diff"
    ) -> mailbox.MailboxMessage:
        return mailbox.append_guidance(self.plan_dir, text, state_dir=self.state_dir)

    def view(self) -> mailbox.MailboxView:
        return mailbox.read_mailbox(self.plan_dir, state_dir=self.state_dir)

    def deliveries(self) -> list[mailbox.MailboxMessage]:
        return [
            message
            for message in self.view().messages
            if message.kind == mailbox.KIND_GUIDANCE_DELIVERY
        ]

    def mailbox_events(self, action: str) -> list[dict[str, Any]]:
        if not self.event_path.exists():
            return []
        return [
            record["payload"]
            for record in read_jsonl(self.event_path)
            if record["name"] == events.MAILBOX_EVENT
            and record["payload"].get("action") == action
        ]

    def test_live_delivery_records_one_attempt_and_events_it(self) -> None:
        guidance = self.add_guidance("rebase before you push")
        self.poll.set_live_delivery(self.endpoint())

        self.poll()

        self.assertEqual(["rebase before you push"], self.received)
        [record] = self.deliveries()
        self.assertEqual(guidance.id, record.to)
        self.assertEqual(mailbox.GUIDANCE_STATE_DELIVERED_LIVE, record.delivery_state)
        self.assertEqual(mailbox.GUIDANCE_REASON_DELIVERED, record.delivery_reason)
        self.assertEqual(mailbox.GUIDANCE_CHANNEL_LIVE, record.channel)
        self.assertEqual("run-1", record.run_id)
        self.assertEqual("7", record.session_id)
        self.assertEqual(2, record.turn_index)
        self.assertEqual("codex", record.harness)
        self.assertEqual(
            mailbox.GUIDANCE_STATE_DELIVERED_LIVE,
            self.view().guidance_delivery_state(guidance.id),
        )
        [event_payload] = self.mailbox_events(events.MAILBOX_ACTION_GUIDANCE_DELIVERED)
        self.assertEqual(guidance.id, event_payload["guidance_id"])
        self.assertEqual(record.id, event_payload["delivery_id"])
        self.assertEqual(2, event_payload["turn_index"])

    def test_delivery_events_and_records_carry_no_body(self) -> None:
        secret = "rotate the token s3cr3t-hunter2 tonight"
        self.add_guidance(secret)
        self.poll.set_live_delivery(self.endpoint())

        self.poll()

        [event_payload] = self.mailbox_events(events.MAILBOX_ACTION_GUIDANCE_DELIVERED)
        self.assertNotIn("text", event_payload)
        self.assertNotIn("digest", event_payload)
        self.assertNotIn("length", event_payload)
        [record] = self.deliveries()
        self.assertIsNone(record.text)
        mailbox_text = mailbox.mailbox_path_for_plan(
            self.plan_dir, str(self.state_dir)
        ).read_text(encoding="utf-8")
        # The guidance record itself holds the body once; the delivery record
        # that reports on it holds no copy.
        self.assertEqual(1, mailbox_text.count(secret))

    def test_repeated_polling_submits_once_per_turn(self) -> None:
        self.add_guidance()
        endpoint = guidance_delivery.LiveDeliveryEndpoint(self.turn)
        submissions: list[guidance_delivery.GuidanceRequest] = []

        def counting(
            request: guidance_delivery.GuidanceRequest,
        ) -> guidance_delivery.GuidanceOutcome:
            submissions.append(request)
            return guidance_delivery.deferred(
                guidance_delivery.REASON_TRANSPORT_UNAVAILABLE
            )

        endpoint.open(counting)
        self.poll.set_live_delivery(endpoint)

        self.poll()
        self.poll()
        self.poll()

        self.assertEqual(1, len(submissions))
        self.assertEqual(1, len(self.deliveries()))

    def test_a_deferral_leaves_the_guidance_deliverable_for_the_next_turn(
        self,
    ) -> None:
        guidance = self.add_guidance("read the handoff first")
        self.poll.set_live_delivery(self.endpoint(live=False))

        self.poll()

        [deferral] = self.deliveries()
        self.assertEqual(mailbox.GUIDANCE_STATE_DEFERRED, deferral.delivery_state)
        self.assertEqual(
            mailbox.GUIDANCE_REASON_HARNESS_UNSUPPORTED,
            deferral.delivery_reason,
        )
        self.assertEqual(
            [guidance.id],
            [message.id for message in self.view().deliverable_guidance],
        )
        self.assertEqual([], self.received)
        self.assertEqual(
            1,
            len(self.mailbox_events(events.MAILBOX_ACTION_GUIDANCE_DELIVERY_DEFERRED)),
        )

        # The next turn is a new endpoint and a new attempt.
        self.poll.set_live_delivery(self.endpoint(turn=turn_identity(turn_index=3)))
        self.poll()

        self.assertEqual(["read the handoff first"], self.received)
        self.assertEqual(
            mailbox.GUIDANCE_STATE_DELIVERED_LIVE,
            self.view().guidance_delivery_state(guidance.id),
        )

    def test_a_new_turn_rereads_an_unchanged_mailbox(self) -> None:
        """The unchanged-mailbox short circuit must not swallow a retry.

        Nothing writes the mailbox between the deferral and the next turn, so
        the poll's change token is identical -- yet the new turn still has to
        look, or the guidance would never be delivered at all.
        """
        self.add_guidance("try again next turn")
        self.poll.set_live_delivery(self.endpoint(live=False))
        self.poll()
        # Settle the token so the next tick would otherwise short circuit.
        self.poll()

        self.poll.set_live_delivery(self.endpoint(turn=turn_identity(turn_index=3)))
        self.poll()

        self.assertEqual(["try again next turn"], self.received)

    def test_a_delivered_record_stops_later_turns_from_redelivering(self) -> None:
        self.add_guidance()
        self.poll.set_live_delivery(self.endpoint())
        self.poll()

        self.poll.set_live_delivery(self.endpoint(turn=turn_identity(turn_index=3)))
        self.poll()

        self.assertEqual(1, len(self.received))
        self.assertEqual(1, len(self.deliveries()))

    def test_a_tick_after_the_turn_ended_defers_as_stale(self) -> None:
        self.add_guidance()
        endpoint = self.endpoint()
        endpoint.close()
        self.poll.set_live_delivery(endpoint)

        self.poll()

        [record] = self.deliveries()
        self.assertEqual(mailbox.GUIDANCE_STATE_DEFERRED, record.delivery_state)
        self.assertEqual(mailbox.GUIDANCE_REASON_STALE_TURN, record.delivery_reason)
        self.assertEqual([], self.received)

    def test_no_endpoint_means_no_delivery_history_at_all(self) -> None:
        """Between turns there is nothing to deliver into, so nothing is said."""
        self.add_guidance()

        self.poll()
        self.poll()

        self.assertEqual([], self.deliveries())
        self.assertEqual(
            [],
            self.mailbox_events(events.MAILBOX_ACTION_GUIDANCE_DELIVERY_DEFERRED),
        )

    def test_detaching_the_endpoint_stops_delivery(self) -> None:
        self.add_guidance()
        self.poll.set_live_delivery(self.endpoint())
        self.poll.set_live_delivery(None)

        self.poll()

        self.assertEqual([], self.deliveries())
        self.assertEqual([], self.received)

    def test_a_failed_receipt_keeps_the_guidance_pending(self) -> None:
        """Transport succeeded, the ledger could not say so: prefer a duplicate."""
        guidance = self.add_guidance("prefer at-least-once")
        self.poll.set_live_delivery(self.endpoint())

        for error in (
            OSError("read-only mailbox"),
            mailbox.MailboxError(mailbox.ERROR_MAILBOX_FULL, "no room"),
        ):
            with self.subTest(error=type(error).__name__):
                self.received.clear()
                self.poll.set_live_delivery(self.endpoint())
                with mock.patch.object(
                    mailbox, "append_guidance_delivery", side_effect=error
                ):
                    self.poll()

                self.assertEqual(["prefer at-least-once"], self.received)
                self.assertEqual([], self.deliveries())
                self.assertEqual(
                    mailbox.GUIDANCE_STATE_QUEUED,
                    self.view().guidance_delivery_state(guidance.id),
                )
                self.assertEqual(
                    [guidance.id],
                    [message.id for message in self.view().deliverable_guidance],
                )
                self.assertEqual(
                    [], self.mailbox_events(events.MAILBOX_ACTION_GUIDANCE_DELIVERED)
                )

    def test_a_mailbox_read_failure_skips_delivery_without_raising(self) -> None:
        self.add_guidance()
        self.poll.set_live_delivery(self.endpoint())

        with mock.patch.object(
            mailbox,
            "read_mailbox",
            side_effect=mailbox.MailboxError(mailbox.ERROR_MAILBOX_READ, "unreadable"),
        ):
            self.poll()

        self.assertEqual([], self.received)
        self.assertEqual([], self.deliveries())

    def test_a_failing_transport_is_recorded_not_raised(self) -> None:
        self.add_guidance()
        endpoint = guidance_delivery.LiveDeliveryEndpoint(self.turn)

        def explode(request: guidance_delivery.GuidanceRequest) -> Any:
            raise OSError("broken pipe")

        endpoint.open(explode)
        self.poll.set_live_delivery(endpoint)

        self.poll()

        [record] = self.deliveries()
        self.assertEqual(mailbox.GUIDANCE_STATE_DEFERRED, record.delivery_state)
        self.assertEqual(
            mailbox.GUIDANCE_REASON_TRANSPORT_UNAVAILABLE,
            record.delivery_reason,
        )

    def test_guidance_read_by_the_worker_is_never_delivered_again(self) -> None:
        guidance = self.add_guidance()
        mailbox.append_acknowledgement(
            self.plan_dir, guidance.seq, state_dir=self.state_dir
        )
        self.poll.set_live_delivery(self.endpoint())

        self.poll()

        self.assertEqual([], self.received)
        self.assertEqual([], self.deliveries())


if __name__ == "__main__":
    unittest.main()
