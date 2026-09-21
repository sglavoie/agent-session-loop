"""Acting on a plan from the far end, with the same refusals as acting locally.

Session 12 gave the link a second direction: the Pi could *read* a plan on this
Mac by asking a question that rode back on an acknowledgement. This session
gives it the third: the Pi may ask this Mac to *act*, and the whole difficulty
is that the two ends can lose each other at any point between the asking and
the doing. The tests are grouped by the claim each holds:

* **a submission is decided before anything is journalled.**
  :class:`SubmissionTests` walks what
  :func:`asl_workbench.remote_actions.parse_submission` refuses -- a method
  this connector will not carry, a reply with no way to fetch its prose, a
  live-run control with no freshness proof, a window that closed in flight --
  and asserts each refusal names a word the transport's *receipt* vocabulary
  contains, which is a narrower set than its error vocabulary;
* **the queue is bounded, and cancellation stops at the attempt.**
  :class:`QueueTests` fills it, withdraws from it, expires it, and abandons it,
  and asserts at every exit that the body reference and both tokens went with
  the command;
* **the mutation lane is not the read lane.** :class:`LaneTests` drives a whole
  connector: a submission to a connector with no performer is refused with the
  word that says which capability is missing, an accepted one is reported as an
  ``operation`` frame, a burst is carried out one at a time, and neither lane
  can bury the other or the heartbeat;
* **a body is redeemed at the moment of use and never before.**
  :class:`BodyTests` asserts the fetch happens inside the attempt, that a
  command that expires while waiting never fetched anything, and that a
  redemption that fails settles ``expired`` rather than being retried;
* **a lost response is reported as lost.** :class:`LostResponseTests` covers the
  two directions a mutation can end without an answer -- the performer raising
  after it may have acted, and the link dropping under a command still waiting;
* **a restart settles and never replays.** :class:`RestartTests` drives
  :func:`~asl_workbench.remote_actions.restart_settlement` and
  :meth:`~agent_session_loop.workbench_remote_actions.AslMutationPerformer.recover`
  against a real journal, including the two rows recovery must not touch;
* **every retained control reaches the same code the browser reaches.**
  :class:`PerformerTests` builds a real corpus and routes launch, reply,
  resume, the three run controls, redirect, unlock and archive through it,
  and :class:`BulkArchiveTests` proves the per-entry bulk result survives the
  decomposition into one operation per plan;
* **and nothing that crosses the wire names this disk.** :class:`PrivacyTests`
  searches every frame the mutation lane produces for the temporary directory
  it was built from.

The Pi's own ``tests/test_companion_transport.py`` holds the mirror image of
the restart cases from the service's side; the scenarios here are the Mac-side
halves of the same two: interrupted before delivery, and interrupted after
dispatch.
"""

from __future__ import annotations

import json
import os
import socket
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

from agent_session_loop import (
    control_queue,
    events,
    intervention,
    mailbox,
    plans,
    state,
    workbench,
    workbench_archive,
    workbench_operations,
    workbench_remote_actions,
)
from asl_workbench import archive as portable_archive
from asl_workbench import catalog, remote_actions
from asl_workbench import connector as portable
from asl_workbench.operations import (
    DETAIL_BRIDGE_UNAVAILABLE,
    DETAIL_CONNECTOR_RESTARTED,
    DETAIL_DEADLINE_PASSED,
    DETAIL_NO_RESULT,
    DETAIL_REPLY_WITNESSED,
    METHOD_ABORT,
    METHOD_LAUNCH,
    METHOD_PLAN_ARCHIVE,
    METHOD_QUESTION_REPLY,
    METHOD_REDIRECT,
    METHOD_REPLY,
    METHOD_SELECT_HARNESS,
    METHOD_STOP_AFTER_CURRENT,
    METHOD_UNLOCK,
    OPERATION_ACCEPTED,
    OPERATION_CONSUMED,
    OPERATION_DISPATCHED,
    OPERATION_EXPIRED,
    OPERATION_PENDING,
    OPERATION_REJECTED,
    OPERATION_UNCONFIRMED,
)
from asl_workbench.transport import (
    CAPABILITY_DETAIL,
    CAPABILITY_MUTATE,
    ERROR_DEADLINE,
    ERROR_FORBIDDEN_METHOD,
    ERROR_UNAUTHORIZED,
    ERROR_UNSUPPORTED_CAPABILITY,
    ERROR_WRONG_TARGET,
    KIND_OPERATION,
    KIND_RECEIPT,
    RECEIPT_DETAIL_CODES,
    RECEIPT_EXPIRED,
    RECEIPT_REFUSED,
    VIEW_HEARTBEAT,
    encode_frame,
    request_envelope,
)
from tests.support import (
    _WORKBENCH_DEAD_PID,
    _write_live_lock_file,
    _write_lock_file,
    _write_state_payload,
    write_plan,
)

#: A fixed moment. Every window in this module is relative to it, so nothing
#: here depends on how long the suite takes to run.
NOW = 1789574400.0

CONNECTOR_ID = "mac-studio"
TARGET_ID = "pi-1"

#: Digest-shaped keys: the transport refuses anything else, so a fixture using
#: ``"plan-1"`` would fail validation rather than the assertion it was written
#: for.
PLAN_KEY = "0123456789abcdef"
RUN_KEY = "abcdefabcdef0123"

RUN_ID = "run-alpha"
SESSION_ID = "1"
BRIDGE_TOKEN = "freshness-token-1"
REPLY_REF = "reply-ref-1"
ACTION_TOKEN = "redeem-token-1"
REPLY_TEXT = "carry on, but check the lock first"


def iso(offset: float) -> str:
    """An ASL timestamp ``offset`` seconds from :data:`NOW`."""
    from datetime import UTC, datetime

    return datetime.fromtimestamp(NOW + offset, tz=UTC).isoformat()


# -- Doubles ----------------------------------------------------------------


class LinkChannel:
    """A channel that can hand a submission back on an acknowledgement.

    The whole downward direction is this return value: there is no inbound
    socket, so a command reaches this process only by riding the response to a
    frame the connector was sending anyway.
    """

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []
        self.inbound: list[bytes] = []
        self.failing = False

    def send(self, frame: bytes) -> bytes | None:
        if self.failing:
            raise portable.ChannelError("the link is down")
        self.frames.append(json.loads(frame.decode("utf-8")))
        return self.inbound.pop(0) if self.inbound else None

    def carry(self, envelope: Any) -> None:
        """Queue one submission for the next acknowledgement to carry."""
        self.inbound.append(encode_frame(envelope))

    def operations(self) -> list[dict[str, Any]]:
        return [frame for frame in self.frames if frame["kind"] == KIND_OPERATION]

    def receipts(self) -> list[dict[str, Any]]:
        return [frame for frame in self.frames if frame["kind"] == KIND_RECEIPT]


class StubSource:
    """An observation source with an empty cycle, and its commits counted."""

    def __init__(self) -> None:
        self.committed: list[portable.ObservationCycle] = []

    def observe(self, now: float) -> portable.ObservationCycle:
        return portable.ObservationCycle(observed_at=now)

    def commit(self, cycle: portable.ObservationCycle) -> None:
        self.committed.append(cycle)


class StubResponder:
    """A detail responder, present only so the read lane exists beside the other."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def answer(self, request: Any, *, now: float) -> dict[str, Any]:
        self.requests.append(dict(request))
        return {
            "detail": "logs",
            "target": {"plan_key": PLAN_KEY},
            "observed_at": now,
            "complete": "complete",
            "content": "the log text",
            "content_bytes": len("the log text"),
            "encoding": "utf-8",
        }


class StubPerformer:
    """A performer whose answer is handed to it, and which records its asks.

    Records the body it was given as well as the request, because "the prose
    reached the action and nothing else" is the claim several tests here make.
    """

    def __init__(
        self,
        result: remote_actions.MutationResult | None = None,
        error: Exception | None = None,
        settlements: list[dict[str, Any]] | None = None,
    ) -> None:
        self.result = result or remote_actions.MutationResult(
            status=OPERATION_ACCEPTED, run_id=RUN_ID
        )
        self.error = error
        self.settlements = settlements or []
        self.calls: list[tuple[remote_actions.MutationRequest, str | None]] = []
        self.recovered = 0

    def perform(
        self, request: remote_actions.MutationRequest, *, body: str | None, now: float
    ) -> remote_actions.MutationResult:
        self.calls.append((request, body))
        if self.error is not None:
            raise self.error
        return self.result

    def recover(self, *, now: float) -> list[dict[str, Any]]:
        self.recovered += 1
        return list(self.settlements)


class StubBodySource:
    """A redemption that counts its fetches and can refuse."""

    def __init__(self, text: str = REPLY_TEXT, error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.fetches: list[str] = []

    def fetch(self, request: remote_actions.MutationRequest, *, now: float) -> str:
        self.fetches.append(request.operation_id)
        if self.error is not None:
            raise self.error
        return self.text


class SettlingPerformer(StubPerformer):
    """An accepted command whose later evidence reaches consumed."""

    def __init__(self) -> None:
        super().__init__()
        self.reconciliations: list[str] = []

    def reconcile(
        self, request: remote_actions.MutationRequest, *, now: float
    ) -> remote_actions.MutationResult:
        self.reconciliations.append(request.operation_id)
        return remote_actions.MutationResult(status=OPERATION_CONSUMED)


def inline(attempt: Any) -> None:
    """Run one worker pass on the calling thread."""
    attempt()
    return None


class HeldWorker:
    """A worker that keeps passes pending until released."""

    def __init__(self) -> None:
        self.pending: list[Any] = []

    def __call__(self, attempt: Any) -> None:
        self.pending.append(attempt)
        return None

    def release(self) -> int:
        released = len(self.pending)
        for attempt in self.pending:
            attempt()
        self.pending.clear()
        return released


# -- Payload fixtures -------------------------------------------------------


def submit_body(**overrides: Any) -> dict[str, Any]:
    """One valid ``operation.submit`` payload, defaulting to a run control."""
    payload: dict[str, Any] = {
        "request": "operation.submit",
        "operation_id": "op-1",
        "method": METHOD_STOP_AFTER_CURRENT,
        "target": {"plan_key": PLAN_KEY, "run_key": RUN_KEY, "run_id": RUN_ID},
        "bridge_token": BRIDGE_TOKEN,
    }
    payload.update(overrides)
    return payload


def reply_body(**overrides: Any) -> dict[str, Any]:
    """A submission whose method needs prose fetched out of band."""
    return submit_body(
        method=METHOD_QUESTION_REPLY,
        reply_ref=REPLY_REF,
        action_token=ACTION_TOKEN,
        **overrides,
    )


def submission(
    request_id: str = "m1", deadline: float = NOW + 30.0, **overrides: Any
) -> dict[str, Any]:
    """One downward request carrying a command for this connector."""
    return request_envelope(
        payload=submit_body(**overrides),
        connector_id=CONNECTOR_ID,
        target_id=TARGET_ID,
        sent_at=NOW,
        request_id=request_id,
        deadline=deadline,
    )


def detail_request(request_id: str = "r1") -> dict[str, Any]:
    """A downward ``detail.read``: the lane the mutation lane runs beside."""
    return request_envelope(
        payload={
            "request": "detail.read",
            "detail": "logs",
            "stream": "stdout",
            "target": {"plan_key": PLAN_KEY},
        },
        connector_id=CONNECTOR_ID,
        target_id=TARGET_ID,
        sent_at=NOW,
        request_id=request_id,
        deadline=NOW + 30.0,
    )


def make_request(**overrides: Any) -> remote_actions.MutationRequest:
    """One parsed command, without going through an envelope to get it."""
    fields: dict[str, Any] = {
        "operation_id": "op-1",
        "method": METHOD_STOP_AFTER_CURRENT,
        "plan_key": PLAN_KEY,
        "requested_at": NOW,
        "deliver_by": NOW + 30.0,
        "run_id": RUN_ID,
        "bridge_token": BRIDGE_TOKEN,
    }
    fields.update(overrides)
    return remote_actions.MutationRequest(**fields)


def make_connector(**overrides: Any) -> portable.ObservationConnector:
    """A connector wired for the mutation lane, with inline workers."""
    fields: dict[str, Any] = {
        "connector_id": CONNECTOR_ID,
        "target_id": TARGET_ID,
        "source": StubSource(),
        "channel": LinkChannel(),
        # Long enough that the heartbeat is not the lane under test, except in
        # the tests that ask for it by name.
        "heartbeat_interval": 60.0,
        "observation_interval": 3600.0,
        "responder": StubResponder(),
        "performer": StubPerformer(),
        "body_source": StubBodySource(),
        "run_observation": inline,
        "run_answer": inline,
        "run_mutation": inline,
    }
    fields.update(overrides)
    return portable.ObservationConnector(**fields)


def drive(connector: Any, steps: int, *, start: float = NOW) -> list[str]:
    """Step the connector ``steps`` times just after ``start``."""
    lanes: list[str] = []
    for index in range(steps):
        lane = connector.step(start + 0.01 * (index + 1))
        if lane is not None:
            lanes.append(lane)
    return lanes


# --------------------------------------------------------------------------
# Reading a submission
# --------------------------------------------------------------------------


class SubmissionTests(unittest.TestCase):
    """What this connector will agree to hold, decided before it holds it."""

    def parse(self, **overrides: Any) -> remote_actions.MutationRequest:
        return remote_actions.parse_submission(
            submit_body(**overrides), now=NOW, deadline=NOW + 30.0
        )

    def refusal(self, **overrides: Any) -> remote_actions.RemoteActionError:
        with self.assertRaises(remote_actions.RemoteActionError) as caught:
            self.parse(**overrides)
        return caught.exception

    def test_a_submission_becomes_a_command_with_a_bounded_window(self) -> None:
        request = self.parse()

        self.assertEqual("op-1", request.operation_id)
        self.assertEqual(METHOD_STOP_AFTER_CURRENT, request.method)
        self.assertEqual(PLAN_KEY, request.plan_key)
        self.assertEqual(NOW + 30.0, request.deliver_by)
        self.assertEqual(RUN_ID, request.run_id)
        # The operation id doubles as the command id, which is what lets a
        # restart tell a delivered mutation from a browser's own.
        self.assertEqual(request.operation_id, request.command_id)

    def test_the_connectors_ceiling_beats_a_generous_service(self) -> None:
        """A service may ask for an hour; it gets the window this end honours."""
        far = remote_actions.parse_submission(
            submit_body(), now=NOW, deadline=NOW + 3600.0
        )

        self.assertEqual(
            NOW + remote_actions.MAX_DELIVERY_WINDOW_SECONDS, far.deliver_by
        )

    def test_a_tighter_service_deadline_wins_over_the_ceiling(self) -> None:
        near = remote_actions.parse_submission(
            submit_body(), now=NOW, deadline=NOW + 5.0
        )

        self.assertEqual(NOW + 5.0, near.deliver_by)

    def test_a_window_that_closed_in_flight_is_expired_not_refused(self) -> None:
        """``expired`` is the word that means *it certainly did not run*."""
        with self.assertRaises(remote_actions.RemoteActionError) as caught:
            remote_actions.parse_submission(submit_body(), now=NOW, deadline=NOW - 1.0)

        self.assertEqual(RECEIPT_EXPIRED, caught.exception.outcome)
        self.assertEqual(ERROR_DEADLINE, caught.exception.detail_code)

    def test_a_method_this_connector_will_not_deliver_is_refused(self) -> None:
        error = self.refusal(method="detail.read")

        self.assertEqual(RECEIPT_REFUSED, error.outcome)
        self.assertEqual(ERROR_FORBIDDEN_METHOD, error.detail_code)

    def test_a_reply_without_a_way_to_fetch_its_prose_is_refused(self) -> None:
        """Carrying it out would answer the question with an empty string."""
        error = self.refusal(method=METHOD_QUESTION_REPLY, reply_ref=REPLY_REF)

        self.assertEqual(ERROR_FORBIDDEN_METHOD, error.detail_code)

    def test_a_reply_reference_without_a_token_is_refused_too(self) -> None:
        error = self.refusal(method=METHOD_REPLY, action_token=ACTION_TOKEN)

        self.assertEqual(ERROR_FORBIDDEN_METHOD, error.detail_code)

    def test_acting_on_a_live_run_needs_the_proof_asl_issued(self) -> None:
        payload = submit_body(method=METHOD_ABORT)
        payload.pop("bridge_token")

        with self.assertRaises(remote_actions.RemoteActionError) as caught:
            remote_actions.parse_submission(payload, now=NOW, deadline=NOW + 30.0)

        self.assertEqual(ERROR_UNAUTHORIZED, caught.exception.detail_code)

    def test_the_methods_not_decided_against_a_live_run_need_no_proof(self) -> None:
        """No run yet, a run already gone, a plan not running, or a *future* turn.

        Selecting a harness belongs here rather than with the other two controls
        because ASL gives it no action to validate a proof against: demanding
        one would refuse every submission instead of checking anything.
        """
        for method in (
            METHOD_LAUNCH,
            METHOD_UNLOCK,
            METHOD_PLAN_ARCHIVE,
            METHOD_SELECT_HARNESS,
        ):
            with self.subTest(method=method):
                payload = submit_body(method=method)
                payload.pop("bridge_token")
                request = remote_actions.parse_submission(
                    payload, now=NOW, deadline=NOW + 30.0
                )
                self.assertEqual(method, request.method)

    def test_a_submission_without_a_plan_key_names_no_target(self) -> None:
        error = self.refusal(target={"run_key": RUN_KEY})

        self.assertEqual(ERROR_WRONG_TARGET, error.detail_code)

    def test_every_refusal_word_is_one_a_receipt_may_carry(self) -> None:
        """The receipt vocabulary is narrower than the error vocabulary.

        ``invalid-field`` and ``missing-field`` are transport errors and are
        *not* receipt detail codes, so a refusal reaching for one would raise
        inside the exception rather than reach the far end.
        """
        cases: list[dict[str, Any]] = [
            {"method": "detail.read"},
            {"operation_id": "not a valid id"},
            {"target": {"run_key": RUN_KEY}},
            {"method": METHOD_QUESTION_REPLY},
            {"harness": ""},
        ]
        for overrides in cases:
            with self.subTest(overrides=overrides):
                with self.assertRaises(remote_actions.RemoteActionError) as caught:
                    self.parse(**overrides)
                self.assertIn(caught.exception.detail_code, RECEIPT_DETAIL_CODES)

    def test_a_request_from_the_other_lane_is_not_a_submission(self) -> None:
        with self.assertRaises(remote_actions.RemoteActionError) as caught:
            remote_actions.parse_submission(
                {"request": "detail.read", "detail": "logs"}, now=NOW
            )

        self.assertEqual(ERROR_FORBIDDEN_METHOD, caught.exception.detail_code)

    def test_a_result_may_only_speak_words_the_transport_knows(self) -> None:
        with self.assertRaises(ValueError):
            remote_actions.MutationResult(status=OPERATION_PENDING)
        with self.assertRaises(ValueError):
            remote_actions.MutationResult(
                status=OPERATION_REJECTED, detail_code="made-up"
            )

    def test_a_queue_only_status_is_folded_onto_one_that_may_be_reported(
        self,
    ) -> None:
        self.assertEqual(
            OPERATION_DISPATCHED, remote_actions.reportable_status(OPERATION_PENDING)
        )
        self.assertEqual(
            OPERATION_REJECTED, remote_actions.reportable_status("cancelled")
        )
        self.assertEqual(
            OPERATION_CONSUMED, remote_actions.reportable_status(OPERATION_CONSUMED)
        )


# --------------------------------------------------------------------------
# Holding it
# --------------------------------------------------------------------------


class QueueTests(unittest.TestCase):
    """Bounded, cancellable up to the attempt, and forgetful on every exit."""

    def setUp(self) -> None:
        self.queue = remote_actions.MutationQueue()

    def fill(self, count: int) -> list[remote_actions.MutationRequest]:
        held = []
        for index in range(count):
            request = make_request(operation_id=f"op-{index}")
            self.assertTrue(self.queue.accept(request, now=NOW))
            held.append(request)
        return held

    def test_the_queue_is_bounded_while_the_service_can_still_react(self) -> None:
        self.fill(remote_actions.MAX_PENDING_MUTATIONS)

        with self.assertRaises(remote_actions.RemoteActionError) as caught:
            self.queue.accept(make_request(operation_id="op-extra"), now=NOW)

        self.assertEqual(RECEIPT_REFUSED, caught.exception.outcome)
        self.assertEqual(DETAIL_BRIDGE_UNAVAILABLE, caught.exception.detail_code)

    def test_a_resubmission_of_a_command_in_hand_is_dropped_silently(self) -> None:
        """The answer the service waits for is the outcome, and it is coming."""
        self.fill(1)
        duplicate = make_request(operation_id="op-0", reply_ref=REPLY_REF)

        self.assertFalse(self.queue.accept(duplicate, now=NOW))
        self.assertEqual(1, self.queue.pending)
        # Dropped *and* disarmed: the copy that lost cannot redeem anything.
        self.assertIsNone(duplicate.reply_ref)
        self.assertEqual("", duplicate.action_token)

    def test_a_command_whose_window_closed_before_it_was_queued_is_expired(
        self,
    ) -> None:
        with self.assertRaises(remote_actions.RemoteActionError) as caught:
            self.queue.accept(make_request(deliver_by=NOW - 1.0), now=NOW)

        self.assertEqual(RECEIPT_EXPIRED, caught.exception.outcome)
        self.assertEqual(DETAIL_DEADLINE_PASSED, caught.exception.detail_code)

    def test_cancellation_reaches_a_waiting_command(self) -> None:
        (request,) = self.fill(1)

        cancelled = self.queue.cancel("op-0")

        self.assertIs(request, cancelled)
        self.assertEqual(0, self.queue.pending)

    def test_cancellation_does_not_reach_one_being_carried_out(self) -> None:
        """The attempt is running; its result is the only honest answer left."""
        self.fill(1)
        self.assertIsNotNone(self.queue.next_attempt(NOW))

        self.assertIsNone(self.queue.cancel("op-0"))
        self.assertEqual(1, self.queue.attempting)

    def test_a_command_is_handed_out_once_and_only_once(self) -> None:
        self.fill(2)

        first = self.queue.next_attempt(NOW)
        second = self.queue.next_attempt(NOW)
        third = self.queue.next_attempt(NOW)

        assert first is not None and second is not None
        self.assertEqual(["op-0", "op-1"], [first.operation_id, second.operation_id])
        self.assertIsNone(third)

    def test_expiry_reaches_only_what_is_still_waiting(self) -> None:
        self.fill(2)
        self.queue.next_attempt(NOW)

        expired = self.queue.expire(NOW + 31.0)

        self.assertEqual(["op-1"], [request.operation_id for request in expired])
        self.assertEqual(1, self.queue.attempting)

    def test_abandoning_drops_what_waits_and_leaves_what_runs(self) -> None:
        self.fill(3)
        self.queue.next_attempt(NOW)

        abandoned = self.queue.abandon()

        self.assertEqual(
            ["op-1", "op-2"], [request.operation_id for request in abandoned]
        )
        self.assertEqual(0, self.queue.pending)
        self.assertEqual(1, self.queue.attempting)

    def test_every_exit_takes_the_body_reference_and_both_tokens(self) -> None:
        exits = {
            "cancel": lambda queue: queue.cancel("op-0"),
            "expire": lambda queue: queue.expire(NOW + 31.0),
            "abandon": lambda queue: queue.abandon(),
            "settle": lambda queue: queue.settle("op-0"),
            "clear": lambda queue: queue.clear(),
        }
        for name, leave in exits.items():
            with self.subTest(exit=name):
                queue = remote_actions.MutationQueue()
                request = make_request(
                    operation_id="op-0",
                    method=METHOD_QUESTION_REPLY,
                    reply_ref=REPLY_REF,
                    action_token=ACTION_TOKEN,
                )
                queue.accept(request, now=NOW)

                leave(queue)

                self.assertIsNone(request.reply_ref)
                self.assertEqual("", request.action_token)
                self.assertEqual("", request.bridge_token)


# --------------------------------------------------------------------------
# The lane
# --------------------------------------------------------------------------


class LaneTests(unittest.TestCase):
    """Mutation delivery beside the reads, never inside them and never above."""

    def test_a_performer_makes_the_connector_stop_being_observation_only(
        self,
    ) -> None:
        connector = make_connector()

        self.assertIn(CAPABILITY_MUTATE, connector.capabilities)
        self.assertFalse(connector.observation_only)
        self.assertFalse(connector.hello()["observation_only"])

    def test_without_a_performer_the_connector_declares_observation_only(
        self,
    ) -> None:
        connector = make_connector(performer=None, body_source=None)

        self.assertNotIn(CAPABILITY_MUTATE, connector.capabilities)
        self.assertTrue(connector.observation_only)
        self.assertIn(CAPABILITY_DETAIL, connector.capabilities)

    def test_a_performer_without_a_responder_offers_mutate_and_not_detail(
        self,
    ) -> None:
        """Derived from what was wired, so neither implies the other."""
        connector = make_connector(responder=None)

        self.assertIn(CAPABILITY_MUTATE, connector.capabilities)
        self.assertNotIn(CAPABILITY_DETAIL, connector.capabilities)

    def test_a_carried_submission_is_carried_out_and_reported(self) -> None:
        channel = LinkChannel()
        performer = StubPerformer()
        connector = make_connector(channel=channel, performer=performer)
        channel.carry(submission("m1"))

        drive(connector, 6)

        (report,) = channel.operations()
        self.assertEqual(KIND_OPERATION, report["kind"])
        self.assertEqual(CAPABILITY_MUTATE, report["capability"])
        self.assertEqual("op-1", report["payload"]["operation_id"])
        self.assertEqual(OPERATION_ACCEPTED, report["payload"]["status"])
        self.assertEqual(METHOD_STOP_AFTER_CURRENT, report["payload"]["method"])
        self.assertEqual(1, len(performer.calls))

    def test_an_accepted_mutation_is_reported_again_when_asl_consumes_it(self) -> None:
        channel = LinkChannel()
        performer = SettlingPerformer()
        connector = make_connector(channel=channel, performer=performer)
        channel.carry(submission("m1"))

        drive(connector, 6)
        drive(connector, 6, start=NOW + 2.0)

        reports = channel.operations()
        self.assertEqual(
            [OPERATION_ACCEPTED, OPERATION_CONSUMED],
            [report["payload"]["status"] for report in reports],
        )
        self.assertEqual(["op-1"], performer.reconciliations)
        self.assertEqual(0, connector.pending_mutations())

    def test_the_report_is_uncorrelated_because_the_receipt_is_the_outcome(
        self,
    ) -> None:
        """An operation frame answers by ``operation_id``, not ``request_id``."""
        channel = LinkChannel()
        connector = make_connector(channel=channel)
        channel.carry(submission("m1"))

        drive(connector, 6)

        (report,) = channel.operations()
        self.assertIsNone(report.get("request_id"))
        self.assertEqual([], channel.receipts())

    def test_a_submission_to_a_connector_that_does_not_act_is_refused(self) -> None:
        channel = LinkChannel()
        connector = make_connector(channel=channel, performer=None, body_source=None)
        channel.carry(submission("m1"))

        drive(connector, 6)

        (receipt,) = channel.receipts()
        self.assertEqual(RECEIPT_REFUSED, receipt["payload"]["outcome"])
        self.assertEqual(
            ERROR_UNSUPPORTED_CAPABILITY, receipt["payload"]["detail_code"]
        )

    def test_a_refused_submission_names_the_capability_it_belongs_to(self) -> None:
        """So the far end can route the refusal rather than guess at it."""
        channel = LinkChannel()
        connector = make_connector(channel=channel, performer=None, body_source=None)
        channel.carry(submission("m1"))

        drive(connector, 6)

        (receipt,) = channel.receipts()
        self.assertEqual(CAPABILITY_MUTATE, receipt["capability"])

    def test_a_submission_that_arrives_past_its_deadline_is_never_journalled(
        self,
    ) -> None:
        channel = LinkChannel()
        performer = StubPerformer()
        connector = make_connector(channel=channel, performer=performer)
        channel.carry(submission("m1", deadline=NOW + 0.001))

        drive(connector, 6, start=NOW + 10.0)

        (receipt,) = channel.receipts()
        self.assertEqual(RECEIPT_EXPIRED, receipt["payload"]["outcome"])
        self.assertEqual(ERROR_DEADLINE, receipt["payload"]["detail_code"])
        self.assertEqual([], performer.calls)

    def test_a_refused_submission_is_refused_before_it_is_queued(self) -> None:
        channel = LinkChannel()
        performer = StubPerformer()
        connector = make_connector(channel=channel, performer=performer)
        channel.carry(submission("m1", method=METHOD_QUESTION_REPLY))

        drive(connector, 6)

        (receipt,) = channel.receipts()
        self.assertEqual(ERROR_FORBIDDEN_METHOD, receipt["payload"]["detail_code"])
        self.assertEqual([], performer.calls)
        self.assertEqual(0, connector.pending_mutations())

    def test_a_resent_submission_is_carried_out_once(self) -> None:
        channel = LinkChannel()
        performer = StubPerformer()
        worker = HeldWorker()
        connector = make_connector(
            channel=channel, performer=performer, run_mutation=worker
        )
        envelope = submission("m1")
        channel.carry(envelope)
        channel.carry(envelope)

        drive(connector, 6)
        worker.release()
        drive(connector, 4)

        self.assertEqual(1, len(performer.calls))
        self.assertEqual(1, len(channel.operations()))

    def test_mutations_are_carried_out_one_at_a_time(self) -> None:
        """Two actions at once are two decisions against state neither saw."""
        channel = LinkChannel()
        performer = StubPerformer()
        worker = HeldWorker()
        connector = make_connector(
            channel=channel, performer=performer, run_mutation=worker
        )
        channel.carry(submission("m1", operation_id="op-1"))
        channel.carry(submission("m2", operation_id="op-2"))

        drive(connector, 8)

        self.assertEqual(1, len(worker.pending))
        worker.release()
        drive(connector, 8)
        self.assertEqual(1, len(worker.pending))

    def test_a_read_and_a_mutation_do_not_wait_on_each_other(self) -> None:
        channel = LinkChannel()
        responder = StubResponder()
        reads = HeldWorker()
        connector = make_connector(
            channel=channel,
            responder=responder,
            run_answer=reads,
        )
        channel.carry(detail_request("r1"))
        channel.carry(submission("m1"))

        drive(connector, 8)

        # The read is stuck on its worker; the mutation went out anyway.
        self.assertEqual(1, len(reads.pending))
        self.assertEqual(1, len(channel.operations()))

    def test_the_heartbeat_outranks_a_finished_mutation(self) -> None:
        """A connector the receiver has written off cannot report anything."""
        channel = LinkChannel()
        connector = make_connector(channel=channel, heartbeat_interval=0.001)
        channel.carry(submission("m1"))

        lanes = drive(connector, 3)

        self.assertEqual(VIEW_HEARTBEAT, lanes[0])

    def test_a_waiting_command_whose_window_closes_is_reported_expired(self) -> None:
        channel = LinkChannel()
        performer = StubPerformer()
        worker = HeldWorker()
        connector = make_connector(
            channel=channel, performer=performer, run_mutation=worker
        )
        channel.carry(submission("m1", operation_id="op-1"))
        channel.carry(submission("m2", operation_id="op-2"))
        drive(connector, 6)

        drive(connector, 4, start=NOW + 31.0)

        reported = {
            frame["payload"]["operation_id"]: frame["payload"]
            for frame in channel.operations()
        }
        self.assertEqual(OPERATION_EXPIRED, reported["op-2"]["status"])
        self.assertEqual(DETAIL_DEADLINE_PASSED, reported["op-2"]["detail_code"])
        # ``op-1`` was handed to the worker before the window closed, so it is
        # past the point where "it did not run" could still be true.
        self.assertNotIn("op-1", reported)

    def test_stopping_drops_every_command_still_in_hand(self) -> None:
        channel = LinkChannel()
        worker = HeldWorker()
        connector = make_connector(channel=channel, run_mutation=worker)
        channel.carry(submission("m1"))
        drive(connector, 2)

        connector.stop()

        self.assertEqual(0, connector.pending_mutations())
        self.assertIsNone(connector.step(NOW + 1.0))


# --------------------------------------------------------------------------
# The body
# --------------------------------------------------------------------------


class BodyTests(unittest.TestCase):
    """Prose is fetched at the moment of use, and nowhere else, ever."""

    def test_the_body_is_redeemed_inside_the_attempt_and_handed_straight_on(
        self,
    ) -> None:
        channel = LinkChannel()
        performer = StubPerformer()
        source = StubBodySource()
        connector = make_connector(
            channel=channel, performer=performer, body_source=source
        )
        channel.carry(
            request_envelope(
                payload=reply_body(),
                connector_id=CONNECTOR_ID,
                target_id=TARGET_ID,
                sent_at=NOW,
                request_id="m1",
                deadline=NOW + 30.0,
            )
        )

        drive(connector, 6)

        self.assertEqual(["op-1"], source.fetches)
        (request, body) = performer.calls[0]
        self.assertEqual(REPLY_TEXT, body)
        self.assertEqual(METHOD_QUESTION_REPLY, request.method)

    def test_a_command_that_never_ran_never_redeemed_anything(self) -> None:
        """The window closes while it waits; the prose stayed on the Pi."""
        channel = LinkChannel()
        source = StubBodySource()
        worker = HeldWorker()
        connector = make_connector(
            channel=channel, body_source=source, run_mutation=worker
        )
        channel.carry(
            request_envelope(
                payload=reply_body(operation_id="op-2"),
                connector_id=CONNECTOR_ID,
                target_id=TARGET_ID,
                sent_at=NOW,
                request_id="m2",
                deadline=NOW + 30.0,
            )
        )
        channel.carry(submission("m1", operation_id="op-1"))
        drive(connector, 6)

        drive(connector, 4, start=NOW + 31.0)

        self.assertEqual([], source.fetches)

    def test_a_refused_redemption_settles_expired_rather_than_retrying(self) -> None:
        channel = LinkChannel()
        performer = StubPerformer()
        source = StubBodySource(
            error=remote_actions.BodyUnavailable(
                "the token was spent", detail_code="action-token-refused"
            )
        )
        connector = make_connector(
            channel=channel, performer=performer, body_source=source
        )
        channel.carry(
            request_envelope(
                payload=reply_body(),
                connector_id=CONNECTOR_ID,
                target_id=TARGET_ID,
                sent_at=NOW,
                request_id="m1",
                deadline=NOW + 30.0,
            )
        )

        drive(connector, 6)

        (report,) = channel.operations()
        self.assertEqual(OPERATION_EXPIRED, report["payload"]["status"])
        self.assertEqual("action-token-refused", report["payload"]["detail_code"])
        self.assertEqual([], performer.calls)
        assert connector.mutation_fault is not None
        self.assertIn("token was spent", connector.mutation_fault)

    def test_a_body_bearing_command_with_no_redemption_route_is_expired(self) -> None:
        channel = LinkChannel()
        performer = StubPerformer()
        connector = make_connector(
            channel=channel, performer=performer, body_source=None
        )
        channel.carry(
            request_envelope(
                payload=reply_body(),
                connector_id=CONNECTOR_ID,
                target_id=TARGET_ID,
                sent_at=NOW,
                request_id="m1",
                deadline=NOW + 30.0,
            )
        )

        drive(connector, 6)

        (report,) = channel.operations()
        self.assertEqual(OPERATION_EXPIRED, report["payload"]["status"])
        self.assertEqual(DETAIL_BRIDGE_UNAVAILABLE, report["payload"]["detail_code"])
        self.assertEqual([], performer.calls)

    def test_no_frame_the_lane_produces_carries_prose(self) -> None:
        channel = LinkChannel()
        connector = make_connector(channel=channel)
        channel.carry(
            request_envelope(
                payload=reply_body(),
                connector_id=CONNECTOR_ID,
                target_id=TARGET_ID,
                sent_at=NOW,
                request_id="m1",
                deadline=NOW + 30.0,
            )
        )

        drive(connector, 6)

        rendered = json.dumps(channel.frames)
        self.assertNotIn(REPLY_TEXT, rendered)
        self.assertNotIn(ACTION_TOKEN, rendered)
        self.assertNotIn(BRIDGE_TOKEN, rendered)


# --------------------------------------------------------------------------
# Losing the answer
# --------------------------------------------------------------------------


class LostResponseTests(unittest.TestCase):
    """The two ways an attempt can end without anybody being able to say what."""

    def test_a_performer_that_raises_is_unconfirmed_rather_than_failed(self) -> None:
        """It may have acted before it raised, and nobody can say which."""
        channel = LinkChannel()
        performer = StubPerformer(error=RuntimeError("the runner vanished"))
        connector = make_connector(channel=channel, performer=performer)
        channel.carry(submission("m1"))

        drive(connector, 6)

        (report,) = channel.operations()
        self.assertEqual(OPERATION_UNCONFIRMED, report["payload"]["status"])
        self.assertEqual(DETAIL_NO_RESULT, report["payload"]["detail_code"])
        assert connector.mutation_fault is not None
        self.assertIn("RuntimeError", connector.mutation_fault)

    def test_a_dropped_link_abandons_what_waits_and_says_so(self) -> None:
        channel = LinkChannel()
        worker = HeldWorker()
        connector = make_connector(channel=channel, run_mutation=worker)
        channel.carry(submission("m1", operation_id="op-1"))
        channel.carry(submission("m2", operation_id="op-2"))
        drive(connector, 4)
        channel.failing = True

        drive(connector, 2, start=NOW + 1.0)
        channel.failing = False
        drive(connector, 8, start=NOW + 120.0)

        reported = {
            frame["payload"]["operation_id"]: frame["payload"]
            for frame in channel.operations()
        }
        self.assertEqual(OPERATION_EXPIRED, reported["op-2"]["status"])
        self.assertEqual(DETAIL_BRIDGE_UNAVAILABLE, reported["op-2"]["detail_code"])

    def test_a_report_of_what_was_carried_out_survives_the_link_dropping(
        self,
    ) -> None:
        """It is the only record this end has of what ASL actually did."""
        channel = LinkChannel()
        connector = make_connector(channel=channel)
        channel.carry(submission("m1"))
        drive(connector, 4)
        channel.failing = True
        drive(connector, 2, start=NOW + 1.0)

        channel.failing = False
        drive(connector, 8, start=NOW + 120.0)

        (report,) = channel.operations()
        self.assertEqual(OPERATION_ACCEPTED, report["payload"]["status"])


# --------------------------------------------------------------------------
# Restarting
# --------------------------------------------------------------------------


class RestartSettlementTests(unittest.TestCase):
    """Two words, and the distinction between them is the whole point."""

    def test_a_row_that_never_reached_dispatch_certainly_did_not_run(self) -> None:
        settlement = remote_actions.restart_settlement(
            {
                "operation_id": "op-1",
                "status": OPERATION_PENDING,
                "method": "plan.abort",
            },
            now=NOW,
        )

        assert settlement is not None
        self.assertEqual(OPERATION_EXPIRED, settlement.status)
        self.assertEqual(DETAIL_CONNECTOR_RESTARTED, settlement.detail_code)
        self.assertEqual("plan.abort", settlement.method)

    def test_a_row_that_reached_dispatch_may_have_run(self) -> None:
        for status in (OPERATION_DISPATCHED, OPERATION_ACCEPTED):
            with self.subTest(status=status):
                settlement = remote_actions.restart_settlement(
                    {"operation_id": "op-1", "status": status}, now=NOW
                )
                assert settlement is not None
                self.assertEqual(OPERATION_UNCONFIRMED, settlement.status)

    def test_a_row_that_already_says_everything_true_is_left_alone(self) -> None:
        for status in (OPERATION_UNCONFIRMED, OPERATION_CONSUMED, OPERATION_REJECTED):
            with self.subTest(status=status):
                self.assertIsNone(
                    remote_actions.restart_settlement(
                        {"operation_id": "op-1", "status": status}, now=NOW
                    )
                )

    def test_a_settlement_frame_names_the_plan_by_key(self) -> None:
        payload = remote_actions.settlement_payload(
            remote_actions.Settlement(
                operation_id="op-1",
                status=OPERATION_UNCONFIRMED,
                detail_code=DETAIL_CONNECTOR_RESTARTED,
                method=METHOD_ABORT,
            ),
            observed_at=NOW,
            target={"plan_key": PLAN_KEY},
        )

        self.assertEqual(PLAN_KEY, payload["target"]["plan_key"])
        self.assertEqual(OPERATION_UNCONFIRMED, payload["status"])

    def test_recovery_runs_once_and_reports_what_it_closed(self) -> None:
        channel = LinkChannel()
        performer = StubPerformer(
            settlements=[
                {
                    "operation_id": "op-old",
                    "status": OPERATION_UNCONFIRMED,
                    "observed_at": NOW,
                    "detail_code": DETAIL_CONNECTOR_RESTARTED,
                }
            ]
        )
        connector = make_connector(channel=channel, performer=performer)

        self.assertEqual(1, connector.recover(NOW))
        self.assertEqual(0, connector.recover(NOW))
        drive(connector, 4)

        (report,) = channel.operations()
        self.assertEqual("op-old", report["payload"]["operation_id"])
        self.assertEqual(OPERATION_UNCONFIRMED, report["payload"]["status"])
        self.assertEqual(1, performer.recovered)

    def test_a_connector_that_cannot_act_recovers_nothing(self) -> None:
        connector = make_connector(performer=None, body_source=None)

        self.assertEqual(0, connector.recover(NOW))

    def test_a_performer_that_raises_during_recovery_is_recorded_not_fatal(
        self,
    ) -> None:
        class Broken(StubPerformer):
            def recover(self, *, now: float) -> list[dict[str, Any]]:
                raise OSError("the journal is unreadable")

        connector = make_connector(performer=Broken())

        self.assertEqual(0, connector.recover(NOW))
        assert connector.mutation_fault is not None
        self.assertIn("OSError", connector.mutation_fault)


# --------------------------------------------------------------------------
# A real corpus
# --------------------------------------------------------------------------


class PerformerTestCase(unittest.TestCase):
    """A plan on disk, acted on the way the wire acts on it."""

    def setUp(self) -> None:
        self._temporary = TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name).resolve()
        self.state_dir = self.root / "state"
        self.state_dir.mkdir()
        self.plans_root = self.root / "plans"
        self.plans_root.mkdir()
        self.registry_path = self.root / "workbench.json"
        self.plan_dir = self.make_plan("plan-alpha")
        self.plan_key = catalog.plan_key(str(self.plan_dir))
        self.performer = workbench_remote_actions.AslMutationPerformer(
            state_dir=self.state_dir,
            plan_roots=(str(self.plans_root),),
            include_default_dirs=False,
            registry_path=self.registry_path,
        )

    def make_plan(self, name: str, statuses: tuple[str, ...] = ("pending",)) -> Path:
        plan_dir = self.plans_root / name
        write_plan(plan_dir, statuses)
        return plans.canonicalize_path(plan_dir)

    def write_state(self, target: Path | None = None, **overrides: Any) -> None:
        target = target or self.plan_dir
        payload: dict[str, Any] = {
            "version": 3,
            "plan_dir": str(target),
            "harness": "claude",
            "paused": False,
            "run_id": RUN_ID,
            "run_started_at": iso(-3600),
            "last_activity_at": iso(-60),
            "active_turn": {
                "run_id": RUN_ID,
                "session_id": SESSION_ID,
                "turn_index": 1,
                "session_kind": "implementation",
                "harness": "claude",
            },
        }
        payload.update(overrides)
        _write_state_payload(target, self.state_dir, payload)

    def live_run(self, **overrides: Any) -> None:
        """A run that is live and holds a turn, so strict controls authorize."""
        self.write_state(**overrides)
        _write_live_lock_file(self.plan_dir, self.state_dir)

    def make_paused(self) -> None:
        """A plan stopped on a question, which is what a reply is about."""
        runs = state.plan_state_dir(self.plan_dir, self.state_dir) / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        last_message = runs / "last-message.txt"
        last_message.write_text("which branch should this land on?", encoding="utf-8")
        _write_state_payload(
            self.plan_dir,
            self.state_dir,
            {
                "version": 3,
                "plan_dir": str(self.plan_dir),
                "ok": False,
                "paused": True,
                "reason": "agent-question",
                "run_id": RUN_ID,
                "harness": "claude",
                "turns": [
                    {
                        "session_id": SESSION_ID,
                        "turn_index": 1,
                        "last_message_path": str(last_message),
                        "harness": "claude",
                    }
                ],
            },
        )

    def launch_result(self, *, reply: bool = False) -> workbench.LaunchResult:
        """What a spawn would have returned, without a spawn happening."""
        return workbench.LaunchResult(
            pid=4242,
            argv=("python3", "-m", "agent_session_loop", "run"),
            plan_dir=self.plan_dir,
            profile_payload=workbench.LaunchProfile().to_payload(),
            reply_provided=reply,
            reply_length=len(REPLY_TEXT) if reply else None,
            reply_origin="stdin" if reply else None,
            state_dir=self.state_dir,
        )

    def token(self) -> str:
        facts = intervention.inspect_run(self.plan_dir, state_dir=self.state_dir)
        return intervention.action_token(facts)

    def request(self, **overrides: Any) -> remote_actions.MutationRequest:
        fields: dict[str, Any] = {
            "operation_id": "op-1",
            "method": METHOD_STOP_AFTER_CURRENT,
            "plan_key": self.plan_key,
            "requested_at": NOW,
            "deliver_by": NOW + 30.0,
            "run_id": RUN_ID,
        }
        fields.update(overrides)
        return remote_actions.MutationRequest(**fields)

    def perform(
        self, body: str | None = None, **overrides: Any
    ) -> remote_actions.MutationResult:
        return self.performer.perform(self.request(**overrides), body=body, now=NOW)

    def rows(self, plan_dir: Path | None = None) -> list[dict[str, Any]]:
        return workbench_operations.read_operations(
            plan_dir or self.plan_dir, state_dir=self.state_dir
        )

    def row(self, operation_id: str = "op-1") -> dict[str, Any]:
        for record in reversed(self.rows()):
            if record.get("operation_id") == operation_id:
                return record
        raise AssertionError(f"no journal row for {operation_id}")

    def queued(self) -> list[str]:
        drained = control_queue.drain_queue(self.plan_dir, state_dir=self.state_dir)
        return [entry.command for entry in drained.accepted]


class PerformerTests(PerformerTestCase):
    """Every method reaches the code the browser's own route reaches."""

    def test_a_key_becomes_the_plan_discovery_already_publishes(self) -> None:
        self.live_run()

        result = self.perform(bridge_token=self.token())

        self.assertEqual(OPERATION_ACCEPTED, result.status)
        self.assertEqual(
            [control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT], self.queued()
        )

    def test_an_unknown_key_is_rejected_rather_than_guessed_at(self) -> None:
        result = self.perform(plan_key="f" * 16)

        self.assertEqual(OPERATION_REJECTED, result.status)
        self.assertIsNotNone(result.reason)
        self.assertEqual([], self.rows())

    def test_all_three_retained_controls_route_to_the_queue(self) -> None:
        cases = {
            METHOD_STOP_AFTER_CURRENT: control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT,
            METHOD_ABORT: control_queue.COMMAND_REQUEST_ABORT_TURN,
            METHOD_SELECT_HARNESS: control_queue.COMMAND_SELECT_FUTURE_HARNESS,
        }
        for index, (method, command) in enumerate(cases.items()):
            with self.subTest(method=method):
                self.live_run()
                result = self.performer.perform(
                    self.request(
                        operation_id=f"op-{index}",
                        method=method,
                        bridge_token=self.token(),
                        harness="codex" if method == METHOD_SELECT_HARNESS else None,
                    ),
                    body=None,
                    now=NOW,
                )
                self.assertEqual(OPERATION_ACCEPTED, result.status)
                self.assertIn(command, self.queued())

    def test_a_control_without_the_proof_is_refused_by_asl_itself(self) -> None:
        """The same strict authorization the browser's own route applies."""
        self.live_run()

        result = self.perform(bridge_token="stale-token")

        self.assertEqual(OPERATION_REJECTED, result.status)
        self.assertIsNotNone(result.reason)
        self.assertEqual([], self.queued())

    def test_a_refused_control_leaves_a_settled_row_not_an_open_one(self) -> None:
        """An open row would block the next command against the same plan."""
        self.live_run()

        self.perform(bridge_token="stale-token")

        self.assertEqual(OPERATION_REJECTED, self.row()["status"])
        self.assertEqual(
            [],
            workbench_operations.open_operations(
                self.plan_dir, state_dir=self.state_dir
            ),
        )

    def test_intent_is_on_disk_before_the_side_effect(self) -> None:
        """Dispatch-before-effect is the only thing that makes a restart honest."""
        self.live_run()
        seen: list[str] = []
        original = workbench_remote_actions.workbench_controls.dispatch_control

        def watched(*args: Any, **kwargs: Any) -> Any:
            seen.append(self.row()["status"])
            return original(*args, **kwargs)

        workbench_remote_actions.workbench_controls.dispatch_control = watched  # type: ignore[assignment]
        self.addCleanup(
            setattr,
            workbench_remote_actions.workbench_controls,
            "dispatch_control",
            original,
        )

        self.perform(bridge_token=self.token())

        self.assertEqual([OPERATION_DISPATCHED], seen)

    def test_a_replayed_operation_is_never_carried_out_twice(self) -> None:
        self.live_run()
        token = self.token()

        first = self.perform(bridge_token=token)
        second = self.perform(bridge_token=token)

        self.assertEqual(OPERATION_ACCEPTED, first.status)
        self.assertEqual(OPERATION_ACCEPTED, second.status)
        self.assertEqual(
            [control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT], self.queued()
        )

    def test_a_second_plan_is_not_blocked_by_the_first_plans_operation(self) -> None:
        """The one-at-a-time rule is per plan, exactly as it is locally."""
        other = self.make_plan("plan-beta")
        self.write_state(other)
        self.live_run()

        self.perform(bridge_token=self.token())
        result = self.performer.perform(
            self.request(
                operation_id="op-2",
                method=METHOD_UNLOCK,
                plan_key=catalog.plan_key(str(other)),
            ),
            body=None,
            now=NOW,
        )

        self.assertIn(result.status, (OPERATION_CONSUMED, OPERATION_REJECTED))

    def test_unlock_is_terminal_and_never_forced(self) -> None:
        self.live_run()
        lock_path = state.plan_state_dir(self.plan_dir, self.state_dir) / (
            state.LOCK_FILE_NAME
        )
        self.assertTrue(lock_path.exists())

        result = self.performer.perform(
            self.request(operation_id="op-u", method=METHOD_UNLOCK),
            body=None,
            now=NOW,
        )

        # A live lock is not taken away from a process that may still hold it.
        self.assertEqual(OPERATION_REJECTED, result.status)
        self.assertTrue(lock_path.exists())

    def test_unlock_releases_a_lock_whose_owner_is_gone(self) -> None:
        self.write_state()
        _write_lock_file(
            self.plan_dir,
            self.state_dir,
            pid=_WORKBENCH_DEAD_PID,
            hostname=socket.gethostname(),
        )

        result = self.performer.perform(
            self.request(operation_id="op-u", method=METHOD_UNLOCK),
            body=None,
            now=NOW,
        )

        self.assertEqual(OPERATION_CONSUMED, result.status)
        self.assertEqual(OPERATION_CONSUMED, self.row("op-u")["status"])

    def test_a_body_over_the_ceiling_is_refused_whole(self) -> None:
        """A truncated reply is prose a run would act on as if it were complete."""
        self.live_run()

        result = self.performer.perform(
            self.request(
                operation_id="op-r",
                method=METHOD_QUESTION_REPLY,
                reply_ref=REPLY_REF,
                action_token=ACTION_TOKEN,
                bridge_token=self.token(),
            ),
            body="x" * (remote_actions.MAX_BODY_CHARS + 1),
            now=NOW,
        )

        self.assertEqual(OPERATION_REJECTED, result.status)

    def test_a_body_that_was_not_redeemed_is_not_an_empty_reply(self) -> None:
        self.live_run()

        with self.assertRaises(remote_actions.BodyUnavailable):
            self.performer.perform(
                self.request(
                    operation_id="op-r",
                    method=METHOD_QUESTION_REPLY,
                    reply_ref=REPLY_REF,
                    action_token=ACTION_TOKEN,
                    bridge_token=self.token(),
                ),
                body=None,
                now=NOW,
            )

    def test_a_delivered_launch_is_the_launch_the_browser_makes(self) -> None:
        with mock.patch.object(
            workbench, "launch_plan", return_value=self.launch_result()
        ) as launched:
            result = self.performer.perform(
                self.request(operation_id="op-l", method=METHOD_LAUNCH),
                body=None,
                now=NOW,
            )

        launched.assert_called_once()
        # ``consumed``: the process it promised exists, so the effect is done.
        self.assertEqual(OPERATION_CONSUMED, result.status)
        self.assertEqual(OPERATION_CONSUMED, self.row("op-l")["status"])

    def test_a_relaunch_of_the_same_operation_spawns_nothing(self) -> None:
        with mock.patch.object(
            workbench, "launch_plan", return_value=self.launch_result()
        ) as launched:
            first = self.performer.perform(
                self.request(operation_id="op-l", method=METHOD_LAUNCH),
                body=None,
                now=NOW,
            )
            second = self.performer.perform(
                self.request(operation_id="op-l", method=METHOD_LAUNCH),
                body=None,
                now=NOW + 1.0,
            )

        launched.assert_called_once()
        self.assertEqual(first.status, second.status)

    def test_a_delivered_reply_carries_the_redeemed_prose_into_the_run(self) -> None:
        """``accepted``, not ``consumed``: a spawned process is not a reader."""
        self.make_paused()

        with mock.patch.object(
            workbench,
            "resume_plan_with_reply",
            return_value=self.launch_result(reply=True),
        ) as resumed:
            result = self.performer.perform(
                self.request(
                    operation_id="op-p",
                    method=METHOD_REPLY,
                    reply_ref=REPLY_REF,
                    action_token=ACTION_TOKEN,
                ),
                body=REPLY_TEXT,
                now=NOW,
            )

        self.assertEqual(OPERATION_ACCEPTED, result.status)
        self.assertEqual(REPLY_TEXT, resumed.call_args.kwargs["reply_text"])

    def test_a_reply_to_a_plan_with_no_question_open_is_rejected(self) -> None:
        self.live_run()

        result = self.performer.perform(
            self.request(
                operation_id="op-q",
                method=METHOD_QUESTION_REPLY,
                reply_ref=REPLY_REF,
                action_token=ACTION_TOKEN,
                bridge_token=self.token(),
            ),
            body=REPLY_TEXT,
            now=NOW,
        )

        self.assertEqual(OPERATION_REJECTED, result.status)
        self.assertIsNotNone(result.reason)

    def test_an_accepted_live_reply_reconciles_when_the_runner_consumes_it(
        self,
    ) -> None:
        self.live_run(
            active_turn={
                "run_id": RUN_ID,
                "session_id": SESSION_ID,
                "turn_index": 1,
                "session_kind": "implementation",
                "harness": "claude",
                "input_wait_active": True,
            }
        )
        question = mailbox.append_question(
            self.plan_dir,
            "May the fixture continue?",
            state_dir=self.state_dir,
            run_id=RUN_ID,
            session_id=SESSION_ID,
            turn_index=1,
        )
        request = self.request(
            operation_id="op-live",
            method=METHOD_QUESTION_REPLY,
            reply_ref=REPLY_REF,
            action_token=ACTION_TOKEN,
            bridge_token=self.token(),
            question_id=question.id,
        )

        first = self.performer.perform(request, body=REPLY_TEXT, now=NOW)
        self.assertEqual(OPERATION_ACCEPTED, first.status)
        plan_state = state.plan_state_dir(self.plan_dir, self.state_dir)
        dispatched_at = float(self.row("op-live")["dispatched_at"])
        from datetime import UTC, datetime

        event = {
            "created_at": datetime.fromtimestamp(
                dispatched_at + 1.0, tz=UTC
            ).isoformat(),
            "name": events.REPLY_PROVIDED_EVENT,
            "payload": {
                "run_id": RUN_ID,
                "session_id": SESSION_ID,
                "turn_index": 1,
                "reply_length": len(REPLY_TEXT),
                "source": {
                    "origin": "mailbox",
                    "run_id": RUN_ID,
                    "session_id": SESSION_ID,
                    "turn_index": 1,
                    "question_id": question.id,
                    "reply_length": len(REPLY_TEXT),
                },
            },
            "version": 1,
        }
        with (plan_state / events.EVENTS_FILE_NAME).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")

        settled = self.performer.reconcile(request, now=dispatched_at + 2.0)

        assert settled is not None
        self.assertEqual(OPERATION_CONSUMED, settled.status)
        self.assertEqual(OPERATION_CONSUMED, self.row("op-live")["status"])

    def test_a_redirect_with_no_endpoint_to_reach_is_rejected(self) -> None:
        """Redirect carries prose too, and is still decided by ASL, not here."""
        self.live_run()

        result = self.performer.perform(
            self.request(
                operation_id="op-x",
                method=METHOD_REDIRECT,
                reply_ref=REPLY_REF,
                action_token=ACTION_TOKEN,
                bridge_token=self.token(),
            ),
            body="go back to the earlier session",
            now=NOW,
        )

        # The lock this fixture writes names no redirect endpoint, so ASL
        # refuses -- with its own word, reached through its own route.
        self.assertEqual(OPERATION_REJECTED, result.status)
        self.assertIsNotNone(result.reason)

    def test_asls_private_refusal_word_stays_on_this_machine(self) -> None:
        self.live_run()
        request = self.request(bridge_token="stale-token")

        result = self.performer.perform(request, body=None, now=NOW)
        payload = remote_actions.operation_payload(request, result, observed_at=NOW)

        self.assertIsNotNone(result.reason)
        self.assertNotIn(str(result.reason), json.dumps(payload))


# --------------------------------------------------------------------------
# Archiving
# --------------------------------------------------------------------------


class BulkArchiveTests(PerformerTestCase):
    """One operation per plan, reassembled into the bulk answer it came from."""

    def archivable(self, name: str) -> Path:
        plan_dir = self.make_plan(name, ("complete",))
        self.write_state(
            plan_dir,
            run_id=None,
            active_turn=None,
            completed_at=iso(-86400),
        )
        return plan_dir

    def test_a_delivered_archive_takes_the_bulk_route(self) -> None:
        plan_dir = self.archivable("plan-old")

        result = self.performer.perform(
            self.request(
                operation_id="op-a",
                method=METHOD_PLAN_ARCHIVE,
                plan_key=catalog.plan_key(str(plan_dir)),
            ),
            body=None,
            now=NOW,
        )

        self.assertEqual(OPERATION_CONSUMED, result.status)
        self.assertFalse(plan_dir.exists())

    def test_a_blocked_archive_is_a_rejection_that_says_why(self) -> None:
        self.live_run()

        result = self.performer.perform(
            self.request(operation_id="op-a", method=METHOD_PLAN_ARCHIVE),
            body=None,
            now=NOW,
        )

        self.assertEqual(OPERATION_REJECTED, result.status)
        self.assertIsNotNone(result.reason)
        self.assertTrue(self.plan_dir.exists())

    def test_per_entry_results_survive_the_decomposition(self) -> None:
        """A partial success reads the same from either side of the wire."""
        good = self.request(operation_id="op-a", plan_key=PLAN_KEY)
        bad = self.request(operation_id="op-b", plan_key=RUN_KEY)
        outcomes = [
            workbench_remote_actions.bulk_outcome(
                good, remote_actions.MutationResult(status=OPERATION_CONSUMED)
            ),
            workbench_remote_actions.bulk_outcome(
                bad,
                remote_actions.MutationResult(
                    status=OPERATION_REJECTED,
                    reason=portable_archive.REASON_STATE_ERROR,
                ),
            ),
        ]

        bulk = workbench_remote_actions.bulk_archive_result(outcomes)
        payload = bulk.to_payload()

        self.assertEqual(1, payload["archived_count"])
        self.assertEqual(1, payload["failed_count"])
        self.assertEqual(2, len(payload["results"]))
        self.assertEqual(
            portable_archive.REASON_STATE_ERROR, payload["results"][1]["reason"]
        )

    def test_a_batch_report_names_plans_by_key_and_never_by_path(self) -> None:
        outcome = workbench_remote_actions.bulk_outcome(
            self.request(plan_key=self.plan_key),
            remote_actions.MutationResult(status=OPERATION_CONSUMED),
        )
        payload = workbench_remote_actions.bulk_archive_result([outcome]).to_payload()

        self.assertNotIn(str(self.root), json.dumps(payload))
        self.assertEqual(self.plan_key, payload["results"][0]["plan_dir"])

    def test_the_local_bulk_route_and_the_delivered_one_agree_on_shape(self) -> None:
        plan_dir = self.archivable("plan-old")
        local = workbench_archive.archive_many(
            [str(plan_dir)],
            state_dir=self.state_dir,
            registry_path=self.registry_path,
            plan_roots=(str(self.plans_root),),
            include_default_dirs=False,
        )
        delivered = workbench_remote_actions.bulk_archive_result(
            [
                workbench_remote_actions.bulk_outcome(
                    self.request(plan_key=catalog.plan_key(str(plan_dir))),
                    remote_actions.MutationResult(status=OPERATION_CONSUMED),
                )
            ]
        )

        self.assertEqual(sorted(local.to_payload()), sorted(delivered.to_payload()))
        self.assertEqual(
            local.to_payload()["archived_count"],
            delivered.to_payload()["archived_count"],
        )


# --------------------------------------------------------------------------
# Restarting, against a real journal
# --------------------------------------------------------------------------


class RestartTests(PerformerTestCase):
    """What a new connector owes the rows its predecessor left open."""

    _seq = 1

    def journal_path(self) -> Path:
        return workbench_operations.journal_path_for_plan(self.plan_dir, self.state_dir)

    def orphan(self, operation_id: str, status: str, **overrides: Any) -> None:
        """Write one open row the way a connector that then died would have.

        Written straight into the journal rather than through
        :func:`~agent_session_loop.workbench_operations.admit_operation`,
        because admitting records *this* process as the owner and the whole
        point of the row is that its owner is gone.
        """
        record: dict[str, Any] = {
            "operation_id": operation_id,
            "command_id": operation_id,
            "method": METHOD_STOP_AFTER_CURRENT,
            "status": status,
            "plan_key": self.plan_key,
            "run_id": RUN_ID,
            "requested_at": NOW - 10.0,
            "deliver_by": NOW + 30.0,
            "pid": _WORKBENCH_DEAD_PID,
            "hostname": socket.gethostname(),
        }
        record.update(overrides)
        line = json.dumps(
            {
                "version": workbench_operations.OPERATIONS_JOURNAL_VERSION,
                "seq": self._seq,
                "operation": {
                    key: value for key, value in record.items() if value is not None
                },
            },
            sort_keys=True,
        )
        self._seq += 1
        path = self.journal_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def test_a_row_journalled_and_never_dispatched_expires(self) -> None:
        self.write_state()
        self.orphan("op-1", OPERATION_PENDING)

        settled = self.performer.recover(now=NOW)

        (payload,) = settled
        self.assertEqual(OPERATION_EXPIRED, payload["status"])
        self.assertEqual(DETAIL_CONNECTOR_RESTARTED, payload["detail_code"])
        self.assertEqual(self.plan_key, payload["target"]["plan_key"])
        self.assertEqual(OPERATION_EXPIRED, self.row("op-1")["status"])

    def test_a_dispatched_row_becomes_unconfirmed_and_is_never_retried(self) -> None:
        self.write_state()
        self.orphan("op-2", OPERATION_DISPATCHED)

        settled = self.performer.recover(now=NOW)

        (payload,) = settled
        self.assertEqual(OPERATION_UNCONFIRMED, payload["status"])
        self.assertEqual(DETAIL_CONNECTOR_RESTARTED, payload["detail_code"])
        # Nothing was queued: recovery settles, it does not act.
        self.assertEqual([], self.queued())

    def test_recovery_reports_a_consumed_in_turn_mailbox_reply(self) -> None:
        """A receipt is stronger evidence than the dead connector process."""
        self.write_state()
        question = mailbox.append_question(
            self.plan_dir,
            "May the fixture continue?",
            state_dir=self.state_dir,
            run_id=RUN_ID,
            session_id=SESSION_ID,
            turn_index=1,
        )
        reply = mailbox.append_reply(
            self.plan_dir,
            question.id,
            REPLY_TEXT,
            state_dir=self.state_dir,
        )
        mailbox.append_receipt(
            self.plan_dir,
            question.id,
            reply.id,
            state_dir=self.state_dir,
        )
        self.orphan(
            "op-reply",
            OPERATION_ACCEPTED,
            method=METHOD_QUESTION_REPLY,
            question_id=question.id,
            dispatched_at=NOW - 5.0,
        )

        settled = self.performer.recover(now=NOW)

        (payload,) = settled
        self.assertEqual(OPERATION_CONSUMED, payload["status"])
        self.assertEqual(DETAIL_REPLY_WITNESSED, payload["detail_code"])
        self.assertEqual(OPERATION_CONSUMED, self.row("op-reply")["status"])

    def test_a_row_the_local_surface_submitted_is_not_the_connectors_to_settle(
        self,
    ) -> None:
        """No ``command_id`` means a browser asked for it, not the far end."""
        self.write_state()
        self.orphan("op-3", OPERATION_PENDING, command_id=None)

        self.assertEqual([], self.performer.recover(now=NOW))
        self.assertEqual(OPERATION_PENDING, self.row("op-3")["status"])

    def test_a_row_whose_owner_is_alive_is_happening_not_lost(self) -> None:
        self.write_state()
        self.orphan("op-4", OPERATION_DISPATCHED, pid=os.getpid())

        self.assertEqual([], self.performer.recover(now=NOW))
        self.assertEqual(OPERATION_DISPATCHED, self.row("op-4")["status"])

    def test_a_row_on_another_host_is_left_to_that_host(self) -> None:
        self.write_state()
        self.orphan("op-5", OPERATION_PENDING, hostname="some-other-mac")

        self.assertEqual([], self.performer.recover(now=NOW))

    def test_recovery_reports_a_settlement_the_connector_can_send(self) -> None:
        self.write_state()
        self.orphan("op-6", OPERATION_DISPATCHED)
        channel = LinkChannel()
        connector = make_connector(channel=channel, performer=self.performer)

        self.assertEqual(1, connector.recover(NOW))
        drive(connector, 4)

        (report,) = channel.operations()
        self.assertEqual("op-6", report["payload"]["operation_id"])
        self.assertEqual(OPERATION_UNCONFIRMED, report["payload"]["status"])
        self.assertNotIn(str(self.root), json.dumps(report))


# --------------------------------------------------------------------------
# Privacy
# --------------------------------------------------------------------------


class PrivacyTests(PerformerTestCase):
    """The far end may act on a plan and must not learn where it lives."""

    def test_no_frame_the_mutation_lane_builds_names_this_disk(self) -> None:
        self.live_run()
        channel = LinkChannel()
        connector = make_connector(channel=channel, performer=self.performer)
        channel.carry(
            request_envelope(
                payload=submit_body(
                    target={"plan_key": self.plan_key, "run_id": RUN_ID},
                    bridge_token=self.token(),
                ),
                connector_id=CONNECTOR_ID,
                target_id=TARGET_ID,
                sent_at=NOW,
                request_id="m1",
                deadline=NOW + 30.0,
            )
        )

        drive(connector, 8)

        rendered = json.dumps(channel.frames)
        self.assertNotIn(str(self.root), rendered)
        self.assertNotIn("plan-alpha", rendered)
        self.assertTrue(channel.operations())

    def test_a_delivered_operation_frame_carries_only_the_closed_vocabulary(
        self,
    ) -> None:
        request = self.request(plan_key=self.plan_key)
        payload = remote_actions.operation_payload(
            request,
            remote_actions.MutationResult(
                status=OPERATION_REJECTED,
                detail_code=DETAIL_NO_RESULT,
                reason="asl-private-word",
            ),
            observed_at=NOW,
            dispatched_at=NOW,
            settled_at=NOW + 1.0,
        )

        self.assertEqual(OPERATION_REJECTED, payload["status"])
        self.assertEqual(DETAIL_NO_RESULT, payload["detail_code"])
        self.assertNotIn("asl-private-word", json.dumps(payload))
        self.assertEqual(self.plan_key, payload["target"]["plan_key"])


if __name__ == "__main__":
    unittest.main()
