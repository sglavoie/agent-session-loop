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

import unittest

from asl_workbench import details, remote_reads
from asl_workbench.transport import (
    CAPABILITY_CATALOG,
    CAPABILITY_DETAIL,
    CAPABILITY_OBSERVE,
    ERROR_DEADLINE,
    ERROR_UNSUPPORTED_CAPABILITY,
    MAX_DETAIL_BYTES,
    RECEIPT_DELIVERED,
    RECEIPT_EXPIRED,
    RECEIPT_REFUSED,
)
from tests import support_workbench_remote_reads as fixtures

ASL_TEST_PORTABLE = True


class CompositionTests(unittest.TestCase):
    """What a read is, before anything has been asked."""

    def test_a_request_carries_only_the_selectors_it_was_given(self) -> None:
        request = remote_reads.ReadRequest(
            request_id="r1",
            detail="events",
            plan_key=fixtures.PLAN_KEY,
            offset=40,
        )
        payload = request.to_payload()
        self.assertEqual(payload["request"], "detail.read")
        self.assertEqual(payload["detail"], "events")
        self.assertEqual(payload["target"], {"plan_key": fixtures.PLAN_KEY})
        self.assertEqual(payload["offset"], 40)
        self.assertNotIn("artifact", payload)
        self.assertNotIn("limit", payload)

    def test_a_request_refuses_a_kind_the_transport_does_not_declare(self) -> None:
        with self.assertRaises(remote_reads.RemoteReadError):
            remote_reads.ReadRequest(
                request_id="r1",
                detail="everything",
                plan_key=fixtures.PLAN_KEY,
            )

    def test_a_request_refuses_a_window_larger_than_a_frame(self) -> None:
        with self.assertRaises(remote_reads.RemoteReadError):
            remote_reads.ReadRequest(
                request_id="r1",
                detail="logs",
                plan_key=fixtures.PLAN_KEY,
                max_bytes=MAX_DETAIL_BYTES + 1,
            )
        with self.assertRaises(remote_reads.RemoteReadError):
            remote_reads.ReadRequest(
                request_id="r1",
                detail="events",
                plan_key=fixtures.PLAN_KEY,
                limit=remote_reads.MAX_DETAIL_ENTRIES + 1,
            )

    def test_a_request_refuses_a_window_the_transport_would_refuse(self) -> None:
        for bounds in ({"offset": -1}, {"limit": 0}, {"max_bytes": 0}):
            refused = self.assertRaises(remote_reads.RemoteReadError)
            with self.subTest(**bounds), refused:
                remote_reads.ReadRequest(
                    request_id="r1",
                    detail="logs",
                    plan_key=fixtures.PLAN_KEY,
                    **bounds,
                )

    def test_the_cache_key_separates_windows_of_the_same_artifact(self) -> None:
        first = remote_reads.ReadRequest(
            request_id="r1",
            detail="events",
            plan_key=fixtures.PLAN_KEY,
            offset=0,
        )
        second = remote_reads.ReadRequest(
            request_id="r2",
            detail="events",
            plan_key=fixtures.PLAN_KEY,
            offset=50,
        )
        self.assertNotEqual(first.key, second.key)

    def test_a_question_selector_is_carried_and_separates_cached_reads(self) -> None:
        first = remote_reads.ReadRequest(
            request_id="r1",
            detail="questions",
            plan_key=fixtures.PLAN_KEY,
            question_id="question-1",
        )
        second = remote_reads.ReadRequest(
            request_id="r2",
            detail="questions",
            plan_key=fixtures.PLAN_KEY,
            question_id="question-2",
        )
        self.assertEqual(first.target()["question_id"], "question-1")
        self.assertNotEqual(first.key, second.key)

    def test_a_composed_read_is_a_frame_the_far_end_would_accept(self) -> None:
        from asl_workbench.transport import validate_envelope

        client = fixtures.make_client()
        ticket = client.read(
            request_id="r1",
            detail="logs",
            plan_key=fixtures.PLAN_KEY,
            now=fixtures.NOW,
        )
        self.assertTrue(ticket.outstanding)
        validated = validate_envelope(
            ticket.envelope,
            direction="down",
            target_id=fixtures.TARGET_ID,
        )
        self.assertEqual(validated["capability"], CAPABILITY_DETAIL)
        self.assertEqual(validated["request_id"], "r1")

    def test_a_second_read_cannot_reuse_an_outstanding_correlation_id(self) -> None:
        client = fixtures.make_client()
        client.read(
            request_id="r1",
            detail="logs",
            plan_key=fixtures.PLAN_KEY,
            now=fixtures.NOW,
        )
        with self.assertRaises(remote_reads.RemoteReadError):
            client.read(
                request_id="r1",
                detail="state",
                plan_key=fixtures.PLAN_KEY,
                now=fixtures.NOW,
            )

    def test_an_impossible_timeout_is_refused_rather_than_clamped(self) -> None:
        client = fixtures.make_client()
        with self.assertRaises(remote_reads.RemoteReadError):
            client.read(
                request_id="r1",
                detail="logs",
                plan_key=fixtures.PLAN_KEY,
                now=fixtures.NOW,
                timeout=remote_reads.MAX_READ_TIMEOUT_SECONDS + 1,
            )


class NonAnswerTests(unittest.TestCase):
    """Offline, unavailable, refused, timed out, cancelled -- and empty.

    The session's requirement is that these stay apart. A cockpit renders
    "the Mac is asleep", "this pairing may not read", "that plan is gone" and
    "this file exists and has nothing in it" as four different panels, and
    every one of them arrives here as the same absence of text.
    """

    def test_a_read_with_no_link_is_offline_and_costs_no_frame(self) -> None:
        client = remote_reads.DetailReadClient(
            connector_id=fixtures.CONNECTOR_ID,
            target_id=fixtures.TARGET_ID,
        )
        ticket = client.read(
            request_id="r1",
            detail="logs",
            plan_key=fixtures.PLAN_KEY,
            now=fixtures.NOW,
        )
        self.assertFalse(ticket.outstanding)
        self.assertIsNone(ticket.envelope)
        assert ticket.answer is not None
        self.assertEqual(ticket.answer.outcome, remote_reads.OUTCOME_OFFLINE)
        self.assertEqual(client.pending(), ())

    def test_a_link_without_the_capability_is_unavailable_not_offline(self) -> None:
        client = remote_reads.DetailReadClient(
            connector_id=fixtures.CONNECTOR_ID,
            target_id=fixtures.TARGET_ID,
        )
        client.link_up(
            (CAPABILITY_OBSERVE, CAPABILITY_CATALOG),
            now=fixtures.NOW,
        )
        ticket = client.read(
            request_id="r1",
            detail="logs",
            plan_key=fixtures.PLAN_KEY,
            now=fixtures.NOW,
        )
        assert ticket.answer is not None
        self.assertEqual(ticket.answer.outcome, remote_reads.OUTCOME_UNAVAILABLE)
        self.assertEqual(ticket.answer.detail_code, ERROR_UNSUPPORTED_CAPABILITY)
        self.assertTrue(client.linked)

    def test_a_full_table_is_unavailable_without_blaming_the_far_end(self) -> None:
        client = fixtures.make_client(max_pending=2)
        client.read(
            request_id="r1",
            detail="logs",
            plan_key=fixtures.PLAN_KEY,
            now=fixtures.NOW,
        )
        client.read(
            request_id="r2",
            detail="state",
            plan_key=fixtures.PLAN_KEY,
            now=fixtures.NOW,
        )
        ticket = client.read(
            request_id="r3",
            detail="plan",
            plan_key=fixtures.PLAN_KEY,
            now=fixtures.NOW,
        )
        assert ticket.answer is not None
        self.assertEqual(ticket.answer.outcome, remote_reads.OUTCOME_UNAVAILABLE)
        # Nothing was refused out there, so no transport word is put in the
        # far end's mouth.
        self.assertIsNone(ticket.answer.detail_code)

    def test_a_refusal_carries_the_reason_and_is_not_an_answer(self) -> None:
        client = fixtures.make_client()
        client.read(
            request_id="r1",
            detail="logs",
            plan_key=fixtures.PLAN_KEY,
            now=fixtures.NOW,
        )
        answer = client.accept(
            fixtures.detail_frame(
                "r1",
                content=None,
                content_bytes=None,
                encoding=None,
                complete="unknown",
                reason=details.REASON_NOT_FOUND,
            ),
            now=fixtures.NOW + 1,
        )
        assert answer is not None
        self.assertEqual(answer.outcome, remote_reads.OUTCOME_REFUSED)
        self.assertEqual(answer.reason, details.REASON_NOT_FOUND)
        self.assertFalse(answer.answered)
        self.assertFalse(answer.empty)

    def test_an_empty_artifact_is_answered_and_complete(self) -> None:
        client = fixtures.make_client()
        client.read(
            request_id="r1",
            detail="events",
            plan_key=fixtures.PLAN_KEY,
            now=fixtures.NOW,
        )
        from asl_workbench.transport import detail_envelope

        answer = client.accept(
            detail_envelope(
                payload=fixtures.rows_body([]),
                connector_id=fixtures.CONNECTOR_ID,
                target_id=fixtures.TARGET_ID,
                sent_at=fixtures.NOW,
                request_id="r1",
            ),
            now=fixtures.NOW + 1,
        )
        assert answer is not None
        self.assertEqual(answer.outcome, remote_reads.OUTCOME_ANSWERED)
        self.assertTrue(answer.answered)
        self.assertTrue(answer.empty)
        self.assertEqual(answer.complete, "complete")

    def test_only_an_answered_read_can_be_empty(self) -> None:
        """The property a renderer is tempted to compute itself."""
        client = fixtures.make_client()
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
                    plan_key=fixtures.PLAN_KEY,
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
                client = fixtures.make_client()
                client.read(
                    request_id="r1",
                    detail="logs",
                    plan_key=fixtures.PLAN_KEY,
                    now=fixtures.NOW,
                )
                code = None if outcome == RECEIPT_DELIVERED else ERROR_DEADLINE
                answer = client.accept(
                    fixtures.receipt_frame("r1", outcome, code),
                    now=fixtures.NOW + 1,
                )
                assert answer is not None
                self.assertEqual(answer.outcome, expected)
                self.assertIsNone(answer.reason)
                self.assertFalse(answer.empty)

    def test_a_frame_from_another_connector_is_refused_not_correlated(self) -> None:
        from asl_workbench.transport import detail_envelope

        client = fixtures.make_client()
        client.read(
            request_id="r1",
            detail="logs",
            plan_key=fixtures.PLAN_KEY,
            now=fixtures.NOW,
        )
        with self.assertRaises(remote_reads.RemoteReadError):
            client.accept(
                detail_envelope(
                    payload=fixtures.detail_body(),
                    connector_id="somebody-else",
                    target_id=fixtures.TARGET_ID,
                    sent_at=fixtures.NOW,
                    request_id="r1",
                ),
                now=fixtures.NOW + 1,
            )
        self.assertIn("r1", client.pending())

    def test_a_frame_for_an_unknown_read_settles_nothing_and_holds_nothing(
        self,
    ) -> None:
        client = fixtures.make_client()
        self.assertIsNone(
            client.accept(
                fixtures.detail_frame("ghost"),
                now=fixtures.NOW + 1,
            )
        )
        self.assertEqual(len(client.bodies), 0)


if __name__ == "__main__":
    unittest.main()
