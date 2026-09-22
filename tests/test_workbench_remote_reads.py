"""On-demand detail reads: asked from the Pi, answered by this Mac, kept nowhere.

Session 12 adds the second direction. Session 11 left a connector that only
ever spoke outward -- heartbeats, snapshots, catalog pages, history batches --
and the question it left open was how a remote cockpit reads a plan's *text*
without either spooling the corpus to disk somewhere or opening a second,
inbound socket into somebody's laptop. The answer is a correlated request that
rides back on the acknowledgement of a frame this Mac was sending anyway, and
the tests are grouped by the claim they hold rather than by the class they
call:

* **a read that could not be asked is not an empty artifact.**
  :class:`NonAnswerTests` walks every way a read ends without material --
  offline, unavailable, refused, timed out, cancelled -- and asserts each one
  is a distinct outcome and that none of them is reported as emptiness. This is
  the session's headline requirement and the one a renderer gets wrong by
  default: ``if not answer.entries: show("nothing here")`` is exactly the bug;
* **bodies are bounded and forgotten.** :class:`BodyLifetimeTests` fills the
  cache past its capacity, ages it past its TTL, drops the link under it and
  restarts the connector on top of it, and asserts at each point that the text
  is gone from both ends rather than merely unreferenced;
* **paging is driven by the cursor the Mac sent**, never by arithmetic done at
  the asking end, so local paging and remote paging agree about where an
  artifact stops. :class:`PagingTests` proves it on both sides of the wire;
* **detail traffic is separated from mutation delivery and from the
  heartbeat.** :class:`LaneTests` is the starvation group: a heartbeat outranks
  every answer, an answer is never buried behind a corpus being paged out, a
  corpus is never buried behind a service reading continuously, a read runs off
  the delivery thread, and ``operation.submit`` is refused on arrival rather
  than queued behind the reads;
* **a real corpus reads correctly.** :class:`ResponderTests` builds a plan tree
  and reads it through
  :class:`~agent_session_loop.workbench_remote_reads.AslDetailResponder`,
  including the failure directions: an unknown key, an unreadable kind, and an
  artifact that exists and is empty;
* **and nothing that crosses the wire names this disk.** :class:`PrivacyTests`
  is the gate: every detail frame the responder builds is searched for the
  temporary directory it was built from, because the far end may render a plan
  and must not learn the layout of the machine holding it.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from agent_session_loop import (
    events,
    plans,
    state,
    workbench_remote_reads,
)
from asl_workbench import catalog, details, remote_reads
from asl_workbench import connector as portable
from asl_workbench.transport import (
    CAPABILITY_CATALOG,
    CAPABILITY_DETAIL,
    CAPABILITY_OBSERVE,
    ERROR_DEADLINE,
    ERROR_FORBIDDEN_METHOD,
    ERROR_UNSUPPORTED_CAPABILITY,
    KIND_DETAIL,
    KIND_RECEIPT,
    MAX_DETAIL_BYTES,
    RECEIPT_DELIVERED,
    RECEIPT_EXPIRED,
    RECEIPT_REFUSED,
    VIEW_HEARTBEAT,
    VIEW_SNAPSHOT,
    encode_frame,
    request_envelope,
)
from tests.support import _write_state_payload, write_plan

#: A fixed moment. Every deadline and every TTL in this module is relative to
#: it, so nothing here depends on how long the suite takes to run.
NOW = 1789574400.0

CONNECTOR_ID = "mac-studio"
TARGET_ID = "pi-1"

#: A digest-shaped key. :func:`asl_workbench.catalog.plan_key` produces sixteen
#: hex characters and the transport refuses anything else, so a fixture using
#: ``"plan-1"`` fails validation rather than the assertion it was written for.
PLAN_KEY = "0123456789abcdef"
OTHER_KEY = "fedcba9876543210"
RUN_KEY = "abcdefabcdef0123"


def iso(offset: float) -> str:
    """An ASL timestamp ``offset`` seconds from :data:`NOW`."""
    from datetime import UTC, datetime

    return datetime.fromtimestamp(NOW + offset, tz=UTC).isoformat()


# -- Doubles ----------------------------------------------------------------


class LinkChannel:
    """A channel that can hand a request back on an acknowledgement.

    The whole downward direction is this return value: there is no inbound
    socket, so a question reaches this process only by riding the response to a
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
        """Queue one request for the next acknowledgement to carry."""
        self.inbound.append(encode_frame(envelope))

    def kinds(self) -> list[str]:
        return [frame["kind"] for frame in self.frames]

    def lanes(self) -> list[str]:
        """What each frame was, with observations named by their view."""
        return [
            frame["payload"]["view"]
            if frame["kind"] == "observation"
            else frame["kind"]
            for frame in self.frames
        ]

    def answers(self) -> list[dict[str, Any]]:
        return [
            frame
            for frame in self.frames
            if frame["kind"] in (KIND_DETAIL, KIND_RECEIPT)
        ]


class StubSource:
    """An observation source with a fixed cycle, and its commits counted."""

    def __init__(self, cycle: portable.ObservationCycle | None = None) -> None:
        self.cycle = cycle
        self.committed: list[portable.ObservationCycle] = []

    def observe(self, now: float) -> portable.ObservationCycle:
        if self.cycle is None:
            return portable.ObservationCycle(observed_at=now)
        return self.cycle

    def commit(self, cycle: portable.ObservationCycle) -> None:
        self.committed.append(cycle)


class StubResponder:
    """A responder whose answer is handed to it, and which records its asks."""

    def __init__(
        self, payload: dict[str, Any] | None = None, error: Exception | None = None
    ) -> None:
        self.payload = payload
        self.error = error
        self.requests: list[dict[str, Any]] = []

    def answer(self, request: Any, *, now: float) -> dict[str, Any]:
        self.requests.append(dict(request))
        if self.error is not None:
            raise self.error
        body = dict(self.payload) if self.payload is not None else detail_body()
        body["observed_at"] = now
        return body


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


def detail_body(**overrides: Any) -> dict[str, Any]:
    """One valid :data:`DETAIL_SHAPE` payload carrying a little material."""
    payload: dict[str, Any] = {
        "detail": "logs",
        "target": {"plan_key": PLAN_KEY},
        "observed_at": NOW,
        "complete": "complete",
        "content": "the log text",
        "content_bytes": len("the log text"),
        "encoding": "utf-8",
    }
    payload.update(overrides)
    return payload


def rows_body(rows: list[dict[str, Any]], **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "detail": "events",
        "target": {"plan_key": PLAN_KEY},
        "observed_at": NOW,
        "complete": "complete",
        "entries": rows,
        "entry_count": len(rows),
    }
    payload.update(overrides)
    return payload


def detail_frame(request_id: str, **overrides: Any) -> dict[str, Any]:
    """One upward detail envelope answering ``request_id``."""
    from asl_workbench.transport import detail_envelope

    return detail_envelope(
        payload=detail_body(**overrides),
        connector_id=CONNECTOR_ID,
        target_id=TARGET_ID,
        sent_at=NOW,
        request_id=request_id,
    )


def receipt_frame(
    request_id: str, outcome: str, detail_code: str | None = None
) -> dict[str, Any]:
    """One upward receipt declining ``request_id``."""
    from asl_workbench.transport import receipt_envelope

    payload: dict[str, Any] = {"outcome": outcome, "observed_at": NOW}
    if detail_code is not None:
        payload["detail_code"] = detail_code
    return receipt_envelope(
        payload=payload,
        capability=CAPABILITY_DETAIL,
        connector_id=CONNECTOR_ID,
        target_id=TARGET_ID,
        sent_at=NOW,
        request_id=request_id,
    )


def mutation_request(request_id: str = "op-req") -> dict[str, Any]:
    """A downward ``operation.submit``: the traffic this link never accepts."""
    return request_envelope(
        payload={
            "request": "operation.submit",
            "operation_id": "op-1",
            "method": "plan.abort",
            "target": {"plan_key": PLAN_KEY},
        },
        connector_id=CONNECTOR_ID,
        target_id=TARGET_ID,
        sent_at=NOW,
        request_id=request_id,
        deadline=NOW + 30.0,
    )


def make_client(**overrides: Any) -> remote_reads.DetailReadClient:
    """A client already paired with a connector that offers detail reads."""
    fields: dict[str, Any] = {
        "connector_id": CONNECTOR_ID,
        "target_id": TARGET_ID,
    }
    fields.update(overrides)
    client = remote_reads.DetailReadClient(**fields)
    client.link_up((CAPABILITY_OBSERVE, CAPABILITY_CATALOG, CAPABILITY_DETAIL), now=NOW)
    return client


def make_connector(**overrides: Any) -> portable.ObservationConnector:
    """A connector wired for the correlated lane, with inline workers."""
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
        "run_observation": inline,
        "run_answer": inline,
    }
    fields.update(overrides)
    return portable.ObservationConnector(**fields)


# --------------------------------------------------------------------------
# Composing a read
# --------------------------------------------------------------------------


class CompositionTests(unittest.TestCase):
    """What a read is, before anything has been asked."""

    def test_a_request_carries_only_the_selectors_it_was_given(self) -> None:
        request = remote_reads.ReadRequest(
            request_id="r1", detail="events", plan_key=PLAN_KEY, offset=40
        )
        payload = request.to_payload()
        self.assertEqual(payload["request"], "detail.read")
        self.assertEqual(payload["detail"], "events")
        self.assertEqual(payload["target"], {"plan_key": PLAN_KEY})
        self.assertEqual(payload["offset"], 40)
        self.assertNotIn("artifact", payload)
        self.assertNotIn("limit", payload)

    def test_a_request_refuses_a_kind_the_transport_does_not_declare(self) -> None:
        with self.assertRaises(remote_reads.RemoteReadError):
            remote_reads.ReadRequest(
                request_id="r1", detail="everything", plan_key=PLAN_KEY
            )

    def test_a_request_refuses_a_window_larger_than_a_frame(self) -> None:
        with self.assertRaises(remote_reads.RemoteReadError):
            remote_reads.ReadRequest(
                request_id="r1",
                detail="logs",
                plan_key=PLAN_KEY,
                max_bytes=MAX_DETAIL_BYTES + 1,
            )
        with self.assertRaises(remote_reads.RemoteReadError):
            remote_reads.ReadRequest(
                request_id="r1",
                detail="events",
                plan_key=PLAN_KEY,
                limit=remote_reads.MAX_DETAIL_ENTRIES + 1,
            )

    def test_the_cache_key_separates_windows_of_the_same_artifact(self) -> None:
        first = remote_reads.ReadRequest(
            request_id="r1", detail="events", plan_key=PLAN_KEY, offset=0
        )
        second = remote_reads.ReadRequest(
            request_id="r2", detail="events", plan_key=PLAN_KEY, offset=50
        )
        self.assertNotEqual(first.key, second.key)

    def test_a_question_selector_is_carried_and_separates_cached_reads(self) -> None:
        first = remote_reads.ReadRequest(
            request_id="r1",
            detail="questions",
            plan_key=PLAN_KEY,
            question_id="question-1",
        )
        second = remote_reads.ReadRequest(
            request_id="r2",
            detail="questions",
            plan_key=PLAN_KEY,
            question_id="question-2",
        )
        self.assertEqual(first.target()["question_id"], "question-1")
        self.assertNotEqual(first.key, second.key)

    def test_a_composed_read_is_a_frame_the_far_end_would_accept(self) -> None:
        from asl_workbench.transport import validate_envelope

        client = make_client()
        ticket = client.read(request_id="r1", detail="logs", plan_key=PLAN_KEY, now=NOW)
        self.assertTrue(ticket.outstanding)
        validated = validate_envelope(
            ticket.envelope, direction="down", target_id=TARGET_ID
        )
        self.assertEqual(validated["capability"], CAPABILITY_DETAIL)
        self.assertEqual(validated["request_id"], "r1")

    def test_a_second_read_cannot_reuse_an_outstanding_correlation_id(self) -> None:
        client = make_client()
        client.read(request_id="r1", detail="logs", plan_key=PLAN_KEY, now=NOW)
        with self.assertRaises(remote_reads.RemoteReadError):
            client.read(request_id="r1", detail="state", plan_key=PLAN_KEY, now=NOW)

    def test_an_impossible_timeout_is_refused_rather_than_clamped(self) -> None:
        client = make_client()
        with self.assertRaises(remote_reads.RemoteReadError):
            client.read(
                request_id="r1",
                detail="logs",
                plan_key=PLAN_KEY,
                now=NOW,
                timeout=remote_reads.MAX_READ_TIMEOUT_SECONDS + 1,
            )


# --------------------------------------------------------------------------
# The ways a read ends without material
# --------------------------------------------------------------------------


class NonAnswerTests(unittest.TestCase):
    """Offline, unavailable, refused, timed out, cancelled -- and empty.

    The session's requirement is that these stay apart. A cockpit renders
    "the Mac is asleep", "this pairing may not read", "that plan is gone" and
    "this file exists and has nothing in it" as four different panels, and
    every one of them arrives here as the same absence of text.
    """

    def test_a_read_with_no_link_is_offline_and_costs_no_frame(self) -> None:
        client = remote_reads.DetailReadClient(
            connector_id=CONNECTOR_ID, target_id=TARGET_ID
        )
        ticket = client.read(request_id="r1", detail="logs", plan_key=PLAN_KEY, now=NOW)
        self.assertFalse(ticket.outstanding)
        self.assertIsNone(ticket.envelope)
        assert ticket.answer is not None
        self.assertEqual(ticket.answer.outcome, remote_reads.OUTCOME_OFFLINE)
        self.assertEqual(client.pending(), ())

    def test_a_link_without_the_capability_is_unavailable_not_offline(self) -> None:
        client = remote_reads.DetailReadClient(
            connector_id=CONNECTOR_ID, target_id=TARGET_ID
        )
        client.link_up((CAPABILITY_OBSERVE, CAPABILITY_CATALOG), now=NOW)
        ticket = client.read(request_id="r1", detail="logs", plan_key=PLAN_KEY, now=NOW)
        assert ticket.answer is not None
        self.assertEqual(ticket.answer.outcome, remote_reads.OUTCOME_UNAVAILABLE)
        self.assertEqual(ticket.answer.detail_code, ERROR_UNSUPPORTED_CAPABILITY)
        self.assertTrue(client.linked)

    def test_a_full_table_is_unavailable_without_blaming_the_far_end(self) -> None:
        client = make_client(max_pending=2)
        client.read(request_id="r1", detail="logs", plan_key=PLAN_KEY, now=NOW)
        client.read(request_id="r2", detail="state", plan_key=PLAN_KEY, now=NOW)
        ticket = client.read(request_id="r3", detail="plan", plan_key=PLAN_KEY, now=NOW)
        assert ticket.answer is not None
        self.assertEqual(ticket.answer.outcome, remote_reads.OUTCOME_UNAVAILABLE)
        # Nothing was refused out there, so no transport word is put in the
        # far end's mouth.
        self.assertIsNone(ticket.answer.detail_code)

    def test_a_refusal_carries_the_reason_and_is_not_an_answer(self) -> None:
        client = make_client()
        client.read(request_id="r1", detail="logs", plan_key=PLAN_KEY, now=NOW)
        answer = client.accept(
            detail_frame(
                "r1",
                content=None,
                content_bytes=None,
                encoding=None,
                complete="unknown",
                reason=details.REASON_NOT_FOUND,
            ),
            now=NOW + 1,
        )
        assert answer is not None
        self.assertEqual(answer.outcome, remote_reads.OUTCOME_REFUSED)
        self.assertEqual(answer.reason, details.REASON_NOT_FOUND)
        self.assertFalse(answer.answered)
        self.assertFalse(answer.empty)

    def test_an_empty_artifact_is_answered_and_complete(self) -> None:
        client = make_client()
        client.read(request_id="r1", detail="events", plan_key=PLAN_KEY, now=NOW)
        from asl_workbench.transport import detail_envelope

        answer = client.accept(
            detail_envelope(
                payload=rows_body([]),
                connector_id=CONNECTOR_ID,
                target_id=TARGET_ID,
                sent_at=NOW,
                request_id="r1",
            ),
            now=NOW + 1,
        )
        assert answer is not None
        self.assertEqual(answer.outcome, remote_reads.OUTCOME_ANSWERED)
        self.assertTrue(answer.answered)
        self.assertTrue(answer.empty)
        self.assertEqual(answer.complete, "complete")

    def test_only_an_answered_read_can_be_empty(self) -> None:
        """The property a renderer is tempted to compute itself."""
        client = make_client()
        unanswered = [
            remote_reads.OUTCOME_OFFLINE,
            remote_reads.OUTCOME_UNAVAILABLE,
            remote_reads.OUTCOME_REFUSED,
            remote_reads.OUTCOME_TIMEOUT,
            remote_reads.OUTCOME_CANCELLED,
        ]
        self.assertEqual(
            set(unanswered) | {remote_reads.OUTCOME_ANSWERED},
            set(remote_reads.READ_OUTCOMES),
        )
        for outcome in unanswered:
            with self.subTest(outcome=outcome):
                answer = remote_reads.ReadAnswer(
                    request_id="r1",
                    outcome=outcome,
                    detail="logs",
                    plan_key=PLAN_KEY,
                )
                self.assertFalse(answer.empty)
                self.assertFalse(answer.answered)
        del client

    def test_a_receipt_maps_to_the_link_word_rather_than_to_a_plan_word(self) -> None:
        cases = {
            RECEIPT_EXPIRED: remote_reads.OUTCOME_TIMEOUT,
            RECEIPT_REFUSED: remote_reads.OUTCOME_UNAVAILABLE,
            RECEIPT_DELIVERED: remote_reads.OUTCOME_UNAVAILABLE,
        }
        for outcome, expected in cases.items():
            with self.subTest(outcome=outcome):
                client = make_client()
                client.read(request_id="r1", detail="logs", plan_key=PLAN_KEY, now=NOW)
                code = None if outcome == RECEIPT_DELIVERED else ERROR_DEADLINE
                answer = client.accept(receipt_frame("r1", outcome, code), now=NOW + 1)
                assert answer is not None
                self.assertEqual(answer.outcome, expected)
                self.assertIsNone(answer.reason)
                self.assertFalse(answer.empty)

    def test_a_frame_from_another_connector_is_refused_not_correlated(self) -> None:
        from asl_workbench.transport import detail_envelope

        client = make_client()
        client.read(request_id="r1", detail="logs", plan_key=PLAN_KEY, now=NOW)
        with self.assertRaises(remote_reads.RemoteReadError):
            client.accept(
                detail_envelope(
                    payload=detail_body(),
                    connector_id="somebody-else",
                    target_id=TARGET_ID,
                    sent_at=NOW,
                    request_id="r1",
                ),
                now=NOW + 1,
            )
        self.assertIn("r1", client.pending())

    def test_a_frame_for_an_unknown_read_settles_nothing_and_holds_nothing(
        self,
    ) -> None:
        client = make_client()
        self.assertIsNone(client.accept(detail_frame("ghost"), now=NOW + 1))
        self.assertEqual(len(client.bodies), 0)


# --------------------------------------------------------------------------
# Timeout, cancellation, reconnect
# --------------------------------------------------------------------------


class DeadlineTests(unittest.TestCase):
    """One slow read is one slow read."""

    def test_a_deadline_settles_one_read_and_leaves_the_others(self) -> None:
        client = make_client(timeout=30.0)
        client.read(request_id="slow", detail="logs", plan_key=PLAN_KEY, now=NOW)
        client.read(
            request_id="patient",
            detail="state",
            plan_key=PLAN_KEY,
            now=NOW,
            timeout=90.0,
        )
        timed_out = client.expire(NOW + 31.0)
        self.assertEqual([answer.request_id for answer in timed_out], ["slow"])
        self.assertEqual(timed_out[0].outcome, remote_reads.OUTCOME_TIMEOUT)
        self.assertEqual(client.pending(), ("patient",))

    def test_the_clock_starts_when_the_request_leaves_not_when_it_is_built(
        self,
    ) -> None:
        client = make_client(timeout=10.0)
        client.read(request_id="r1", detail="logs", plan_key=PLAN_KEY, now=NOW)
        # It sat in the outbound queue for nine seconds waiting for a carrier.
        client.sent("r1", now=NOW + 9.0)
        self.assertEqual(client.expire(NOW + 10.5), ())
        self.assertEqual(len(client.expire(NOW + 19.5)), 1)

    def test_a_late_answer_to_a_timed_out_read_is_dropped(self) -> None:
        client = make_client(timeout=5.0)
        client.read(request_id="r1", detail="logs", plan_key=PLAN_KEY, now=NOW)
        client.expire(NOW + 6.0)
        self.assertIsNone(client.accept(detail_frame("r1"), now=NOW + 7.0))
        self.assertEqual(len(client.bodies), 0)

    def test_a_cancelled_read_stops_waiting_and_never_holds_a_late_body(self) -> None:
        client = make_client()
        client.read(request_id="r1", detail="logs", plan_key=PLAN_KEY, now=NOW)
        cancelled = client.cancel("r1", now=NOW + 1.0)
        assert cancelled is not None
        self.assertEqual(cancelled.outcome, remote_reads.OUTCOME_CANCELLED)
        self.assertFalse(client.outstanding("r1"))
        # The Mac finishes the read it started; the frame is dropped here.
        self.assertIsNone(client.accept(detail_frame("r1"), now=NOW + 2.0))
        self.assertEqual(len(client.bodies), 0)

    def test_cancelling_an_unknown_read_is_not_an_error(self) -> None:
        client = make_client()
        self.assertIsNone(client.cancel("nobody", now=NOW))


class ReconnectTests(unittest.TestCase):
    """A link that dropped is a promise nobody is going to keep."""

    def test_a_dropped_link_abandons_every_waiter_as_offline(self) -> None:
        client = make_client()
        client.read(request_id="r1", detail="logs", plan_key=PLAN_KEY, now=NOW)
        client.read(request_id="r2", detail="state", plan_key=PLAN_KEY, now=NOW)
        abandoned = client.link_down(now=NOW + 1.0)
        self.assertEqual(
            {answer.outcome for answer in abandoned},
            {remote_reads.OUTCOME_OFFLINE},
        )
        self.assertEqual(client.pending(), ())
        self.assertFalse(client.linked)

    def test_a_dropped_link_drops_every_held_body(self) -> None:
        client = make_client()
        client.read(request_id="r1", detail="logs", plan_key=PLAN_KEY, now=NOW)
        answer = client.accept(detail_frame("r1"), now=NOW + 1.0)
        assert answer is not None
        self.assertEqual(len(client.bodies), 1)
        client.link_down(now=NOW + 2.0)
        self.assertEqual(len(client.bodies), 0)
        self.assertEqual(client.bodies.held(now=NOW + 2.0), ())

    def test_a_reconnect_starts_a_generation_and_abandons_the_old_one(self) -> None:
        client = make_client()
        first = client.generation
        client.read(request_id="r1", detail="logs", plan_key=PLAN_KEY, now=NOW)
        client.link_up((CAPABILITY_DETAIL,), now=NOW + 5.0)
        self.assertEqual(client.generation, first + 1)
        self.assertEqual(client.pending(), ())
        # And a frame minted before the reconnect correlates to nothing.
        self.assertIsNone(client.accept(detail_frame("r1"), now=NOW + 6.0))

    def test_a_read_after_a_reconnect_is_asked_again(self) -> None:
        client = make_client()
        client.link_down(now=NOW + 1.0)
        offline = client.read(
            request_id="r1", detail="logs", plan_key=PLAN_KEY, now=NOW + 2.0
        )
        self.assertFalse(offline.outstanding)
        client.link_up((CAPABILITY_DETAIL,), now=NOW + 3.0)
        again = client.read(
            request_id="r2", detail="logs", plan_key=PLAN_KEY, now=NOW + 4.0
        )
        self.assertTrue(again.outstanding)


# --------------------------------------------------------------------------
# Bodies
# --------------------------------------------------------------------------


class BodyLifetimeTests(unittest.TestCase):
    """Material somebody is reading right now, and nothing older than that."""

    def key(self, offset: int = 0, plan_key: str = PLAN_KEY) -> Any:
        return (plan_key, "", "logs", "", offset)

    def answer(self, request_id: str = "r1", **overrides: Any) -> Any:
        fields: dict[str, Any] = {
            "request_id": request_id,
            "outcome": remote_reads.OUTCOME_ANSWERED,
            "detail": "logs",
            "plan_key": PLAN_KEY,
            "complete": "complete",
            "content": "a plan's text",
        }
        fields.update(overrides)
        return remote_reads.ReadAnswer(**fields)

    def test_an_unanswered_read_has_no_body_to_hold(self) -> None:
        cache = remote_reads.BodyCache()
        refused = remote_reads.ReadAnswer(
            request_id="r1",
            outcome=remote_reads.OUTCOME_REFUSED,
            detail="logs",
            plan_key=PLAN_KEY,
            reason=details.REASON_NOT_FOUND,
        )
        with self.assertRaises(remote_reads.RemoteReadError):
            cache.store(self.key(), refused, now=NOW)

    def test_the_cache_is_bounded_by_count_and_drops_the_oldest(self) -> None:
        cache = remote_reads.BodyCache(capacity=3)
        for index in range(5):
            cache.store(self.key(index), self.answer(f"r{index}"), now=NOW + index)
        self.assertEqual(len(cache), 3)
        self.assertIsNone(cache.get(self.key(0), now=NOW + 5))
        self.assertIsNotNone(cache.get(self.key(4), now=NOW + 5))

    def test_a_body_is_dropped_once_it_is_older_than_the_ttl(self) -> None:
        cache = remote_reads.BodyCache(ttl_seconds=30.0)
        cache.store(self.key(), self.answer(), now=NOW)
        self.assertIsNotNone(cache.get(self.key(), now=NOW + 29.0))
        self.assertIsNone(cache.get(self.key(), now=NOW + 31.0))
        self.assertEqual(len(cache), 0)

    def test_a_forgotten_body_is_blanked_rather_than_merely_unreferenced(self) -> None:
        cache = remote_reads.BodyCache(capacity=1)
        cache.store(self.key(0), self.answer("r0"), now=NOW)
        held = cache.get(self.key(0), now=NOW)
        assert held is not None
        self.assertEqual(held.content, "a plan's text")
        cache.clear()
        self.assertIsNone(cache.get(self.key(0), now=NOW))
        self.assertEqual(cache.held(now=NOW), ())

    def test_holding_a_diagnostic_does_not_mean_holding_the_text(self) -> None:
        cache = remote_reads.BodyCache()
        cache.store(self.key(), self.answer(), now=NOW)
        (identity,) = cache.held(now=NOW)
        self.assertNotIn("content", identity)
        self.assertNotIn("entries", identity)
        self.assertEqual(identity["body_bytes"], len("a plan's text"))

    def test_a_plan_can_be_forgotten_without_forgetting_the_others(self) -> None:
        cache = remote_reads.BodyCache()
        cache.store(self.key(0, PLAN_KEY), self.answer("r0"), now=NOW)
        cache.store(
            self.key(0, OTHER_KEY),
            self.answer("r1", plan_key=OTHER_KEY),
            now=NOW,
        )
        self.assertEqual(cache.drop(PLAN_KEY), 1)
        self.assertIsNone(cache.get(self.key(0, PLAN_KEY), now=NOW))
        self.assertIsNotNone(cache.get(self.key(0, OTHER_KEY), now=NOW))

    def test_a_refetched_window_counts_as_a_new_arrival(self) -> None:
        cache = remote_reads.BodyCache(capacity=2)
        cache.store(self.key(0), self.answer("r0"), now=NOW)
        cache.store(self.key(1), self.answer("r1"), now=NOW + 1)
        cache.store(self.key(0), self.answer("r2"), now=NOW + 2)
        cache.store(self.key(2), self.answer("r3"), now=NOW + 3)
        # The refetched window survives; the one nobody came back to does not.
        self.assertIsNotNone(cache.get(self.key(0), now=NOW + 4))
        self.assertIsNone(cache.get(self.key(1), now=NOW + 4))

    def test_an_answered_read_is_held_and_a_refused_one_is_not(self) -> None:
        client = make_client()
        client.read(request_id="r1", detail="logs", plan_key=PLAN_KEY, now=NOW)
        client.accept(detail_frame("r1"), now=NOW + 1.0)
        self.assertEqual(len(client.bodies), 1)
        client.read(request_id="r2", detail="state", plan_key=PLAN_KEY, now=NOW + 2.0)
        client.accept(
            receipt_frame("r2", RECEIPT_REFUSED, ERROR_UNSUPPORTED_CAPABILITY),
            now=NOW + 3.0,
        )
        self.assertEqual(len(client.bodies), 1)

    def test_a_held_window_is_found_again_by_the_read_that_asked_for_it(self) -> None:
        client = make_client()
        ticket = client.read(request_id="r1", detail="logs", plan_key=PLAN_KEY, now=NOW)
        client.accept(detail_frame("r1"), now=NOW + 1.0)
        held = client.cached(ticket.request, now=NOW + 2.0)
        assert held is not None
        self.assertEqual(held.content, "the log text")

    def test_expiring_the_reads_also_ages_the_bodies(self) -> None:
        client = make_client(ttl_seconds=10.0)
        client.read(request_id="r1", detail="logs", plan_key=PLAN_KEY, now=NOW)
        client.accept(detail_frame("r1"), now=NOW)
        client.expire(NOW + 11.0)
        self.assertEqual(len(client.bodies), 0)


# --------------------------------------------------------------------------
# Paging
# --------------------------------------------------------------------------


class PagingTests(unittest.TestCase):
    """The cursor the Mac sent is the cursor the next read uses."""

    def page(self, request_id: str, **overrides: Any) -> Any:
        from asl_workbench.transport import detail_envelope

        return detail_envelope(
            payload=rows_body(
                [{"name": "run.started", "created_at": iso(-10)}], **overrides
            ),
            connector_id=CONNECTOR_ID,
            target_id=TARGET_ID,
            sent_at=NOW,
            request_id=request_id,
        )

    def test_a_cut_page_reports_more_and_the_next_read_starts_there(self) -> None:
        client = make_client()
        client.read(request_id="r1", detail="events", plan_key=PLAN_KEY, now=NOW)
        answer = client.accept(
            self.page("r1", offset=0, next_offset=25, total=90, complete="partial"),
            now=NOW + 1.0,
        )
        assert answer is not None
        self.assertTrue(answer.has_more)
        self.assertEqual(answer.complete, "partial")
        follow = client.next_page(answer, request_id="r2", now=NOW + 2.0)
        assert follow is not None
        self.assertEqual(follow.request.offset, 25)
        self.assertEqual(follow.request.detail, "events")
        self.assertTrue(follow.outstanding)

    def test_the_last_page_has_no_follow_up(self) -> None:
        client = make_client()
        client.read(request_id="r1", detail="events", plan_key=PLAN_KEY, now=NOW)
        answer = client.accept(self.page("r1", offset=25, total=26), now=NOW + 1.0)
        assert answer is not None
        self.assertFalse(answer.has_more)
        self.assertIsNone(client.next_page(answer, request_id="r2", now=NOW + 2.0))

    def test_each_page_is_held_under_its_own_window(self) -> None:
        client = make_client()
        first = client.read(
            request_id="r1", detail="events", plan_key=PLAN_KEY, now=NOW
        )
        answer = client.accept(
            self.page("r1", offset=0, next_offset=25, complete="partial"),
            now=NOW + 1.0,
        )
        assert answer is not None
        follow = client.next_page(answer, request_id="r2", now=NOW + 2.0)
        assert follow is not None
        client.accept(self.page("r2", offset=25), now=NOW + 3.0)
        self.assertEqual(len(client.bodies), 2)
        self.assertIsNotNone(client.cached(first.request, now=NOW + 4.0))
        self.assertIsNotNone(client.cached(follow.request, now=NOW + 4.0))

    def test_a_page_read_while_offline_is_offline_rather_than_the_end(self) -> None:
        client = make_client()
        client.read(request_id="r1", detail="events", plan_key=PLAN_KEY, now=NOW)
        answer = client.accept(
            self.page("r1", offset=0, next_offset=25, complete="partial"),
            now=NOW + 1.0,
        )
        assert answer is not None
        client.link_down(now=NOW + 2.0)
        follow = client.next_page(answer, request_id="r2", now=NOW + 3.0)
        assert follow is not None and follow.answer is not None
        self.assertEqual(follow.answer.outcome, remote_reads.OUTCOME_OFFLINE)


# --------------------------------------------------------------------------
# The connector's correlated lane
# --------------------------------------------------------------------------


class LaneTests(unittest.TestCase):
    """Detail traffic beside the cycle, never inside it and never above it."""

    def read_request(self, request_id: str = "r1", **overrides: Any) -> Any:
        client = make_client()
        fields: dict[str, Any] = {
            "request_id": request_id,
            "detail": "logs",
            "plan_key": PLAN_KEY,
        }
        fields.update(overrides)
        ticket = client.read(now=NOW, **fields)
        assert ticket.envelope is not None
        return ticket.envelope

    def drive(self, connector: Any, channel: LinkChannel, steps: int) -> list[str]:
        lanes: list[str] = []
        for index in range(steps):
            lane = connector.step(NOW + 0.1 * (index + 1))
            if lane is not None:
                lanes.append(lane)
        del channel
        return lanes

    # -- what the connector says it can do -------------------------------
    def test_a_responder_makes_the_connector_declare_detail(self) -> None:
        connector = make_connector()
        self.assertIn(CAPABILITY_DETAIL, connector.capabilities)
        self.assertIn(CAPABILITY_DETAIL, connector.hello()["capabilities"])

    def test_without_a_responder_detail_is_not_declared(self) -> None:
        connector = make_connector(responder=None)
        self.assertNotIn(CAPABILITY_DETAIL, connector.capabilities)
        self.assertEqual(
            tuple(connector.capabilities), portable.OBSERVATION_CAPABILITIES
        )

    # -- a round trip ----------------------------------------------------
    def test_a_carried_request_is_answered_on_a_later_frame(self) -> None:
        channel = LinkChannel()
        responder = StubResponder()
        connector = make_connector(channel=channel, responder=responder)
        channel.carry(self.read_request("r1"))
        self.drive(connector, channel, 6)
        answers = channel.answers()
        self.assertEqual(len(answers), 1)
        self.assertEqual(answers[0]["kind"], KIND_DETAIL)
        self.assertEqual(answers[0]["request_id"], "r1")
        self.assertEqual(answers[0]["payload"]["content"], "the log text")
        self.assertEqual(len(responder.requests), 1)
        self.assertEqual(responder.requests[0]["detail"], "logs")

    def test_a_resent_request_is_answered_once(self) -> None:
        channel = LinkChannel()
        responder = StubResponder()
        connector = make_connector(channel=channel, responder=responder)
        envelope = self.read_request("r1")
        channel.carry(envelope)
        channel.carry(envelope)
        self.drive(connector, channel, 8)
        self.assertEqual(len(channel.answers()), 1)
        self.assertEqual(len(responder.requests), 1)

    def test_a_request_for_another_connector_is_not_taken_in(self) -> None:
        from asl_workbench.transport import request_envelope as compose

        channel = LinkChannel()
        connector = make_connector(channel=channel)
        channel.carry(
            compose(
                payload={
                    "request": "detail.read",
                    "detail": "logs",
                    "target": {"plan_key": PLAN_KEY},
                },
                connector_id="another-mac",
                target_id=TARGET_ID,
                sent_at=NOW,
                request_id="r1",
                deadline=NOW + 30.0,
            )
        )
        self.drive(connector, channel, 4)
        self.assertEqual(channel.answers(), [])
        self.assertEqual(
            connector.request_fault, "request addressed to another connector"
        )

    def test_an_undecodable_request_is_recorded_and_does_not_degrade_the_cycle(
        self,
    ) -> None:
        channel = LinkChannel()
        connector = make_connector(channel=channel)
        channel.inbound.append(b"{not json")
        self.drive(connector, channel, 3)
        assert connector.request_fault is not None
        self.assertIn("undecodable request", connector.request_fault)
        self.assertIsNone(connector.degraded_reason)

    # -- separation from mutation ----------------------------------------
    def test_a_mutation_is_refused_on_arrival_and_never_reaches_the_responder(
        self,
    ) -> None:
        channel = LinkChannel()
        responder = StubResponder()
        connector = make_connector(channel=channel, responder=responder)
        channel.carry(mutation_request("op-req"))
        self.drive(connector, channel, 6)
        (answer,) = channel.answers()
        self.assertEqual(answer["kind"], KIND_RECEIPT)
        self.assertEqual(answer["payload"]["outcome"], RECEIPT_REFUSED)
        self.assertEqual(answer["payload"]["detail_code"], ERROR_UNSUPPORTED_CAPABILITY)
        self.assertEqual(responder.requests, [])

    def test_an_offered_capability_with_the_wrong_method_is_refused(self) -> None:
        from asl_workbench.transport import request_envelope as compose

        channel = LinkChannel()
        responder = StubResponder()
        connector = make_connector(channel=channel, responder=responder)
        # ``observation.refresh`` maps to a capability this connector does
        # offer, but the correlated lane performs detail reads and nothing else.
        channel.carry(
            compose(
                payload={"request": "observation.refresh"},
                connector_id=CONNECTOR_ID,
                target_id=TARGET_ID,
                sent_at=NOW,
                request_id="r1",
                deadline=NOW + 30.0,
            )
        )
        self.drive(connector, channel, 6)
        (answer,) = channel.answers()
        self.assertEqual(answer["payload"]["detail_code"], ERROR_FORBIDDEN_METHOD)
        self.assertEqual(responder.requests, [])

    def test_a_connector_without_a_responder_refuses_rather_than_ignores(self) -> None:
        channel = LinkChannel()
        connector = make_connector(channel=channel, responder=None)
        channel.carry(self.read_request("r1"))
        self.drive(connector, channel, 6)
        (answer,) = channel.answers()
        self.assertEqual(answer["kind"], KIND_RECEIPT)
        self.assertEqual(answer["payload"]["detail_code"], ERROR_UNSUPPORTED_CAPABILITY)

    # -- separation from the heartbeat -----------------------------------
    def test_the_heartbeat_goes_first_however_many_answers_are_waiting(self) -> None:
        channel = LinkChannel()
        connector = make_connector(channel=channel, heartbeat_interval=1.0)
        for index in range(3):
            channel.carry(self.read_request(f"r{index}"))
        lanes = self.drive(connector, channel, 12)
        self.assertEqual(lanes[0], VIEW_HEARTBEAT)
        # Every heartbeat that came due was sent before the answers waiting
        # beside it, and the reads still got out.
        self.assertIn(KIND_DETAIL, lanes)

    def test_a_read_that_never_finishes_does_not_stop_the_heartbeat(self) -> None:
        worker = HeldWorker()
        channel = LinkChannel()
        connector = make_connector(
            channel=channel, heartbeat_interval=1.0, run_answer=worker
        )
        channel.carry(self.read_request("r1"))
        beats = 0
        for index in range(30):
            if connector.step(NOW + 0.5 * index) == VIEW_HEARTBEAT:
                beats += 1
        self.assertGreaterEqual(beats, 5)
        self.assertTrue(connector.answering)
        self.assertEqual(channel.answers(), [])
        self.assertEqual(worker.release(), 1)

    def test_the_carrier_tightens_while_a_service_is_reading(self) -> None:
        channel = LinkChannel()
        connector = make_connector(channel=channel, heartbeat_interval=60.0)
        # Nothing has been asked: one beat a minute, whatever else goes out.
        idle = [connector.step(NOW + index) for index in range(11)]
        self.assertEqual(idle.count(VIEW_HEARTBEAT), 1)
        # The beat that comes due next carries a request back with it, and from
        # then on the carrier runs at the reading cadence rather than the idle
        # one -- including the very next beat, which was scheduled before
        # anybody was asking.
        channel.carry(self.read_request("r1"))
        self.assertEqual(connector.step(NOW + 60.0), VIEW_HEARTBEAT)
        active = [connector.step(NOW + 60.0 + index) for index in range(1, 11)]
        self.assertGreaterEqual(active.count(VIEW_HEARTBEAT), 5)

    # -- separation from the cycle ---------------------------------------
    def test_an_answer_is_not_buried_behind_a_corpus_being_paged_out(self) -> None:
        channel = LinkChannel()
        connector = make_connector(
            channel=channel,
            source=StubSource(self.big_cycle()),
            observation_interval=3600.0,
            heartbeat_interval=600.0,
        )
        channel.carry(self.read_request("r1"))
        lanes = self.drive(connector, channel, 12)
        self.assertIn(KIND_DETAIL, lanes)
        # It did not wait for the whole catalog to drain first.
        self.assertLess(lanes.index(KIND_DETAIL), len(lanes) - 1)

    def test_a_reader_asking_continuously_does_not_starve_the_corpus(self) -> None:
        channel = LinkChannel()
        connector = make_connector(
            channel=channel,
            source=StubSource(self.big_cycle()),
            observation_interval=3600.0,
            heartbeat_interval=600.0,
        )
        for index in range(6):
            channel.carry(self.read_request(f"r{index}"))
        lanes = self.drive(connector, channel, 20)
        self.assertIn(KIND_DETAIL, lanes)
        self.assertIn(VIEW_SNAPSHOT, lanes)
        # The two lanes take turns while both have something to send, so the
        # corpus is not left until the reader stops asking. Six frames in,
        # neither lane has had them all.
        opening = lanes[:6]
        self.assertGreaterEqual(opening.count(KIND_DETAIL), 2)
        self.assertGreaterEqual(
            len([lane for lane in opening if lane not in (KIND_DETAIL, KIND_RECEIPT)]),
            2,
        )

    def big_cycle(self) -> portable.ObservationCycle:
        """A cycle with a snapshot and several pages waiting behind it."""
        records = [
            catalog.record_from_summary(self.summary(f"/tmp/plans/plan-{index}"))
            for index in range(4)
        ]
        built = catalog.build_catalog(records)
        return portable.ObservationCycle(
            observed_at=NOW,
            catalog_pages=tuple(
                built.page(offset=index, limit=1) for index in range(4)
            ),
        )

    def summary(self, plan_dir: str) -> dict[str, Any]:
        payload: dict[str, Any] = {name: None for name in catalog.PRIVATE_FIELDS}
        payload.update({name: None for name in catalog.CACHE_SAFE_FIELDS})
        payload.update(
            {
                "plan_dir": plan_dir,
                "exists": True,
                "primary_state": "ready",
                "recommended_action": "run",
                "urgency_rank": 80,
                "urgency_group": "ready",
                "activity_label": "Ready to run",
                "counts": {"pending": 1, "complete": 0, "empty": 0, "unknown": 0},
                "sources": ["state"],
                "repositories": [],
                "repository_count": 0,
                "warnings": [],
            }
        )
        return payload

    # -- bounds ----------------------------------------------------------
    def test_more_reads_than_the_connector_will_hold_are_refused_not_dropped(
        self,
    ) -> None:
        worker = HeldWorker()
        channel = LinkChannel()
        # A frequent carrier, because a request only arrives on the
        # acknowledgement of a frame going out: a silent connector is one
        # nobody can queue work on.
        connector = make_connector(
            channel=channel, run_answer=worker, heartbeat_interval=0.25
        )
        for index in range(portable.MAX_PENDING_REQUESTS + 3):
            channel.carry(self.read_request(f"r{index}"))
        self.drive(connector, channel, 40)
        refusals = [
            frame for frame in channel.answers() if frame["kind"] == KIND_RECEIPT
        ]
        self.assertGreaterEqual(len(refusals), 1)
        self.assertEqual(refusals[0]["payload"]["outcome"], RECEIPT_REFUSED)

    def test_a_responder_that_raises_costs_one_read_and_not_the_deadline(self) -> None:
        channel = LinkChannel()
        connector = make_connector(
            channel=channel, responder=StubResponder(error=RuntimeError("disk gone"))
        )
        channel.carry(self.read_request("r1"))
        self.drive(connector, channel, 6)
        (answer,) = channel.answers()
        self.assertEqual(answer["kind"], KIND_RECEIPT)
        self.assertEqual(answer["payload"]["outcome"], RECEIPT_REFUSED)
        self.assertEqual(answer["payload"]["detail_code"], "bridge-unavailable")
        assert connector.answer_fault is not None
        self.assertIn("RuntimeError", connector.answer_fault)

    def test_an_unsendable_answer_does_not_make_a_healthy_mac_look_degraded(
        self,
    ) -> None:
        channel = LinkChannel()
        connector = make_connector(
            channel=channel,
            responder=StubResponder(payload={"detail": "logs"}),
        )
        channel.carry(self.read_request("r1"))
        self.drive(connector, channel, 6)
        self.assertEqual(channel.answers(), [])
        assert connector.answer_fault is not None
        self.assertIn("unsendable", connector.answer_fault)
        self.assertIsNone(connector.degraded_reason)

    # -- expiry, disconnect, restart -------------------------------------
    def test_a_request_past_its_deadline_is_expired_without_being_performed(
        self,
    ) -> None:
        channel = LinkChannel()
        responder = StubResponder()
        connector = make_connector(channel=channel, responder=responder)
        channel.carry(self.read_request("r1", timeout=0.05))
        self.drive(connector, channel, 6)
        (answer,) = channel.answers()
        self.assertEqual(answer["kind"], KIND_RECEIPT)
        self.assertEqual(answer["payload"]["outcome"], RECEIPT_EXPIRED)
        self.assertEqual(answer["payload"]["detail_code"], ERROR_DEADLINE)
        self.assertEqual(responder.requests, [])

    def test_an_answer_whose_deadline_passed_while_queued_is_forgotten(self) -> None:
        worker = HeldWorker()
        channel = LinkChannel()
        connector = make_connector(channel=channel, run_answer=worker)
        channel.carry(self.read_request("r1", timeout=20.0))
        connector.step(NOW)
        connector.step(NOW + 1.0)
        self.assertEqual(worker.release(), 1)
        # The answer is finished and waiting, and then the deadline passes.
        connector.step(NOW + 40.0)
        self.assertEqual(channel.answers(), [])
        self.assertEqual(connector.pending_reads(), 0)

    def held_answer(self, channel: LinkChannel, **overrides: Any) -> Any:
        """A connector with one finished answer waiting for the channel.

        The heartbeat is due on every step, so it outranks the answer and the
        answer stays queued -- which is the state these two tests are about.
        """
        connector = make_connector(channel=channel, heartbeat_interval=0.5, **overrides)
        channel.carry(self.read_request("r1"))
        for index in range(3):
            connector.step(NOW + 1.0 * index)
        self.assertEqual(connector.pending_reads(), 1)
        return connector

    def test_a_dropped_link_clears_the_answers_it_was_holding(self) -> None:
        channel = LinkChannel()
        connector = self.held_answer(channel)
        channel.failing = True
        connector.step(NOW + 3.0)
        self.assertEqual(connector.pending_reads(), 0)
        self.assertEqual(connector.state, portable.STATE_BACKOFF)
        self.assertEqual(channel.answers(), [])

    def test_a_stopped_connector_holds_no_material(self) -> None:
        channel = LinkChannel()
        connector = self.held_answer(channel)
        connector.stop()
        self.assertEqual(connector.pending_reads(), 0)
        self.assertEqual(connector.pending(), 0)
        self.assertEqual(connector.state, portable.STATE_STOPPED)
        self.assertIsNone(connector.step(NOW + 10.0))
        self.assertEqual(channel.answers(), [])

    def test_a_read_in_flight_when_the_connector_stops_is_let_go(self) -> None:
        worker = HeldWorker()
        channel = LinkChannel()
        connector = make_connector(channel=channel, run_answer=worker)
        channel.carry(self.read_request("r1"))
        connector.step(NOW)
        connector.step(NOW + 0.1)
        self.assertTrue(connector.answering)
        connector.stop()
        self.assertFalse(connector.answering)
        self.assertEqual(connector.pending_reads(), 0)
        # The worker finishing afterwards answers nobody.
        worker.release()
        self.assertIsNone(connector.step(NOW + 1.0))
        self.assertEqual(channel.answers(), [])


# --------------------------------------------------------------------------
# A real corpus
# --------------------------------------------------------------------------


class ResponderTestCase(unittest.TestCase):
    """A temporary ASL corpus, read the way the wire reads it."""

    def setUp(self) -> None:
        self._temporary = TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.state_root = self.root / "state"
        self.plans_root = self.root / "plans"
        self.plans_root.mkdir()
        self.plan = self.make_plan(
            "paused-plan",
            ("complete", "pending"),
            {
                "run_id": "run-paused",
                "run_started_at": iso(-7200),
                "last_activity_at": iso(-1800),
                "harness": "claude",
                "current_session": "2",
                "sessions": {"1": {"status": "complete"}, "2": {"status": "pending"}},
                "paused": True,
                "pause_reason": "blocked",
            },
        )
        self.plan_key = catalog.plan_key(str(self.plan))
        self.responder = workbench_remote_reads.AslDetailResponder(
            state_dir=self.state_root,
            plan_roots=(str(self.plans_root),),
            include_default_dirs=False,
        )

    def make_plan(
        self, name: str, statuses: tuple[str, ...], payload: dict[str, Any]
    ) -> Path:
        plan_dir = self.plans_root / name
        write_plan(plan_dir, statuses)
        (plan_dir / "manifest.md").write_text("# A plan\n", encoding="utf-8")
        canonical = plans.canonicalize_path(plan_dir)
        _write_state_payload(
            canonical, self.state_root, dict(payload, plan_dir=str(canonical))
        )
        return canonical

    def write_events(
        self, plan_dir: Path, count: int, *, payload_size: int = 0
    ) -> None:
        plan_state = state.plan_state_dir(plan_dir, self.state_root)
        plan_state.mkdir(parents=True, exist_ok=True)
        path = plan_state / events.EVENTS_FILE_NAME
        with path.open("a", encoding="utf-8") as handle:
            for index in range(count):
                record: dict[str, Any] = {
                    "created_at": iso(-3600 + index),
                    "name": events.TURN_FINISHED_EVENT,
                    "version": 2,
                    "payload": {"run_id": "run-paused", "session_id": "1"},
                }
                if payload_size:
                    record["payload"]["note"] = "x" * payload_size
                handle.write(json.dumps(record) + "\n")

    def ask(self, detail: str, **overrides: Any) -> dict[str, Any]:
        request: dict[str, Any] = {
            "request": "detail.read",
            "detail": detail,
            "target": {"plan_key": self.plan_key},
        }
        if detail == "logs":
            # A log read selects a stream or an artifact by name; asking for
            # "the logs" is an incomplete request rather than an empty one.
            request["stream"] = "stdout"
        request.update(overrides)
        return self.responder.answer(request, now=NOW)


class ResponderTests(ResponderTestCase):
    """The reading half: a key becomes a directory, a failure becomes a word."""

    def test_a_key_resolves_to_the_plan_discovery_already_publishes(self) -> None:
        payload = self.ask("plan")
        self.assertEqual(payload["detail"], "plan")
        self.assertEqual(payload["target"]["plan_key"], self.plan_key)
        self.assertNotIn("reason", payload)
        (entry,) = payload["entries"]
        self.assertEqual(entry["plan"]["primary_state"], "paused-for-reply")
        self.assertEqual(entry["plan"]["counts"]["pending"], 1)

    def test_an_unknown_key_is_not_found_rather_than_an_empty_plan(self) -> None:
        payload = self.responder.answer(
            {
                "request": "detail.read",
                "detail": "plan",
                "target": {"plan_key": OTHER_KEY},
            },
            now=NOW,
        )
        self.assertEqual(payload["reason"], details.REASON_NOT_FOUND)
        self.assertEqual(payload["complete"], "unknown")
        self.assertNotIn("entries", payload)

    def test_a_log_read_that_names_nothing_is_an_invalid_request(self) -> None:
        payload = self.ask("logs", stream=None)
        self.assertEqual(payload["reason"], details.REASON_INVALID_REQUEST)

    def test_a_reason_from_another_vocabulary_becomes_a_declared_word(self) -> None:
        """``read_questions`` answers in the Q&A vocabulary, not this one."""
        payload = self.ask("questions")
        self.assertEqual(payload["reason"], details.REASON_NOT_FOUND)
        self.assertIn(details.REASON_NOT_FOUND, remote_reads.DETAIL_REFUSAL_REASONS)
        # The word the reader actually used is not lost, only demoted.
        self.assertTrue(
            any("no-paused-run" in warning for warning in payload["warnings"])
        )

    def test_an_undeclared_kind_is_an_invalid_request(self) -> None:
        payload = self.responder.answer(
            {
                "request": "detail.read",
                "detail": "everything",
                "target": {"plan_key": self.plan_key},
            },
            now=NOW,
        )
        self.assertEqual(payload["reason"], details.REASON_INVALID_REQUEST)

    def test_every_declared_kind_is_answered_rather_than_unhandled(self) -> None:
        self.write_events(self.plan, 3)
        for kind in details.DETAIL_KINDS:
            with self.subTest(detail=kind):
                payload = self.ask(kind)
                self.assertEqual(payload["detail"], kind)
                self.assertNotEqual(
                    payload.get("reason"), details.REASON_INVALID_REQUEST
                )

    def test_an_empty_artifact_is_complete_and_empty_not_refused(self) -> None:
        payload = self.ask("events")
        self.assertNotIn("reason", payload)
        self.assertEqual(payload["complete"], "complete")
        self.assertEqual(payload["entries"], [])
        self.assertEqual(payload["entry_count"], 0)

    def test_a_paged_read_carries_the_cursor_it_stopped_at(self) -> None:
        self.write_events(self.plan, 6)
        first = self.ask("events", limit=2)
        self.assertEqual(first["entry_count"], 2)
        self.assertEqual(first["complete"], "partial")
        self.assertEqual(first["next_offset"], 2)
        second = self.ask("events", offset=first["next_offset"], limit=2)
        self.assertEqual(second["offset"], 2)
        self.assertNotEqual(first["entries"], second["entries"])

    def test_a_read_is_bounded_by_bytes_and_not_only_by_rows(self) -> None:
        self.write_events(self.plan, 4, payload_size=150_000)
        payload = self.ask("events", limit=4)
        self.assertLess(payload["entry_count"], 4)
        self.assertEqual(payload["complete"], "partial")
        # The cursor moved back to exactly where the sending stopped.
        self.assertEqual(payload["next_offset"], payload["entry_count"])

    def test_text_is_cut_to_what_one_frame_may_carry(self) -> None:
        payload = self.ask("last-message")
        if "content" in payload:
            self.assertLessEqual(payload["content_bytes"], MAX_DETAIL_BYTES)
            self.assertEqual(payload["encoding"], "utf-8")

    def test_the_selected_run_travels_back_in_the_target(self) -> None:
        self.write_events(self.plan, 2)
        payload = self.ask("events")
        run_id = payload["target"].get("run_id")
        if run_id is not None:
            self.assertEqual(
                payload["target"]["run_key"],
                catalog.run_key(self.plan_key, run_id),
            )

    def test_every_answer_is_a_frame_the_transport_accepts(self) -> None:
        from asl_workbench.transport import detail_envelope

        self.write_events(self.plan, 3)
        for kind in details.DETAIL_KINDS:
            with self.subTest(detail=kind):
                payload = self.ask(kind)
                encode_frame(
                    detail_envelope(
                        payload=payload,
                        connector_id=CONNECTOR_ID,
                        target_id=TARGET_ID,
                        sent_at=NOW,
                        request_id="r1",
                    )
                )

    def test_reading_changes_nothing(self) -> None:
        self.write_events(self.plan, 3)
        before = self.tree_digest()
        for kind in details.DETAIL_KINDS:
            self.ask(kind)
        self.assertEqual(self.tree_digest(), before)

    def tree_digest(self) -> str:
        import hashlib

        digest = hashlib.sha256()
        for path in sorted(self.root.rglob("*")):
            if path.is_file():
                digest.update(str(path.relative_to(self.root)).encode("utf-8"))
                digest.update(path.read_bytes())
        return digest.hexdigest()


class PrivacyTests(ResponderTestCase):
    """What crosses the wire never names this disk."""

    def test_no_answer_carries_a_local_path(self) -> None:
        self.write_events(self.plan, 3)
        needles = (str(self.root), str(self.plans_root), str(self.state_root))
        for kind in details.DETAIL_KINDS:
            with self.subTest(detail=kind):
                rendered = json.dumps(self.ask(kind), default=str)
                for needle in needles:
                    self.assertNotIn(needle, rendered)

    def test_the_scrubber_drops_a_path_column_nobody_declared(self) -> None:
        scrubbed = workbench_remote_reads._scrub(
            {
                "plan_dir": "/Users/somebody/plans/x",
                "events_path": "/Users/somebody/state/events.jsonl",
                "nested": [{"state_root": "/var/asl", "title": "keep me"}],
                "title": "keep me too",
            }
        )
        self.assertEqual(
            scrubbed, {"nested": [{"title": "keep me"}], "title": "keep me too"}
        )

    def test_a_refusal_does_not_leak_the_directory_it_failed_to_find(self) -> None:
        payload = self.responder.answer(
            {
                "request": "detail.read",
                "detail": "logs",
                "target": {"plan_key": OTHER_KEY},
            },
            now=NOW,
        )
        self.assertNotIn(str(self.root), json.dumps(payload, default=str))


# --------------------------------------------------------------------------
# Both halves at once
# --------------------------------------------------------------------------


class RoundTripTests(ResponderTestCase):
    """One real read, asked from the far end and answered off this disk."""

    def test_a_service_reads_a_real_plan_without_anything_touching_disk(self) -> None:
        self.write_events(self.plan, 5)
        channel = LinkChannel()
        connector = make_connector(channel=channel, responder=self.responder)
        client = make_client()

        ticket = client.read(
            request_id="r1",
            detail="events",
            plan_key=self.plan_key,
            limit=2,
            now=NOW,
        )
        assert ticket.envelope is not None
        channel.carry(ticket.envelope)
        client.sent("r1", now=NOW)

        answer = self.pump(connector, channel, client)
        assert answer is not None
        self.assertEqual(answer.outcome, remote_reads.OUTCOME_ANSWERED)
        self.assertEqual(len(answer.entries), 2)
        self.assertTrue(answer.has_more)
        self.assertEqual(answer.complete, "partial")
        self.assertEqual(len(client.bodies), 1)

        follow = client.next_page(answer, request_id="r2", now=NOW + 1.0)
        assert follow is not None and follow.envelope is not None
        channel.carry(follow.envelope)
        second = self.pump(connector, channel, client, start=1.0)
        assert second is not None
        self.assertEqual(second.offset, 2)
        self.assertNotEqual(second.entries, answer.entries)

        # And a disconnect takes every body with it.
        client.link_down(now=NOW + 5.0)
        self.assertEqual(len(client.bodies), 0)

    def test_a_read_for_a_plan_this_mac_does_not_have_is_refused_not_empty(
        self,
    ) -> None:
        channel = LinkChannel()
        connector = make_connector(channel=channel, responder=self.responder)
        client = make_client()
        ticket = client.read(
            request_id="r1", detail="events", plan_key=OTHER_KEY, now=NOW
        )
        assert ticket.envelope is not None
        channel.carry(ticket.envelope)
        answer = self.pump(connector, channel, client)
        assert answer is not None
        self.assertEqual(answer.outcome, remote_reads.OUTCOME_REFUSED)
        self.assertEqual(answer.reason, details.REASON_NOT_FOUND)
        self.assertFalse(answer.empty)
        self.assertEqual(len(client.bodies), 0)

    def pump(
        self,
        connector: Any,
        channel: LinkChannel,
        client: Any,
        *,
        start: float = 0.0,
        steps: int = 10,
    ) -> Any:
        """Step the connector, feeding every answer it sends to the client."""
        seen = len(channel.frames)
        for index in range(steps):
            connector.step(NOW + start + 0.1 * (index + 1))
            for frame in channel.frames[seen:]:
                seen += 1
                if frame["kind"] in (KIND_DETAIL, KIND_RECEIPT):
                    answer = client.accept(frame, now=NOW + start + 0.1 * (index + 1))
                    if answer is not None:
                        return answer
        return None


if __name__ == "__main__":  # pragma: no cover - parity with the other modules
    unittest.main()
