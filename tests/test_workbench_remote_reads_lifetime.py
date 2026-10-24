"""Test workbench remote reads lifetime."""

from __future__ import annotations

import unittest
from typing import Any

from asl_workbench import details, remote_reads
from asl_workbench.transport import (
    CAPABILITY_DETAIL,
    ERROR_UNSUPPORTED_CAPABILITY,
    RECEIPT_REFUSED,
)
from tests import support_workbench_remote_reads as fixtures

ASL_TEST_PORTABLE = True


class DeadlineTests(unittest.TestCase):
    """One slow read is one slow read."""

    def test_a_deadline_settles_one_read_and_leaves_the_others(self) -> None:
        client = fixtures.make_client(timeout=30.0)
        client.read(
            request_id="slow",
            detail="logs",
            plan_key=fixtures.PLAN_KEY,
            now=fixtures.NOW,
        )
        client.read(
            request_id="patient",
            detail="state",
            plan_key=fixtures.PLAN_KEY,
            now=fixtures.NOW,
            timeout=90.0,
        )
        timed_out = client.expire(fixtures.NOW + 31.0)
        self.assertEqual([answer.request_id for answer in timed_out], ["slow"])
        self.assertEqual(timed_out[0].outcome, remote_reads.OUTCOME_TIMEOUT)
        self.assertEqual(client.pending(), ("patient",))

    def test_the_clock_starts_when_the_request_leaves_not_when_it_is_built(
        self,
    ) -> None:
        client = fixtures.make_client(timeout=10.0)
        client.read(
            request_id="r1",
            detail="logs",
            plan_key=fixtures.PLAN_KEY,
            now=fixtures.NOW,
        )
        # It sat in the outbound queue for nine seconds waiting for a carrier.
        client.sent("r1", now=fixtures.NOW + 9.0)
        self.assertEqual(client.expire(fixtures.NOW + 10.5), ())
        self.assertEqual(len(client.expire(fixtures.NOW + 19.5)), 1)

    def test_a_late_answer_to_a_timed_out_read_is_dropped(self) -> None:
        client = fixtures.make_client(timeout=5.0)
        client.read(
            request_id="r1",
            detail="logs",
            plan_key=fixtures.PLAN_KEY,
            now=fixtures.NOW,
        )
        client.expire(fixtures.NOW + 6.0)
        self.assertIsNone(
            client.accept(
                fixtures.detail_frame("r1"),
                now=fixtures.NOW + 7.0,
            )
        )
        self.assertEqual(len(client.bodies), 0)

    def test_a_cancelled_read_stops_waiting_and_never_holds_a_late_body(self) -> None:
        client = fixtures.make_client()
        client.read(
            request_id="r1",
            detail="logs",
            plan_key=fixtures.PLAN_KEY,
            now=fixtures.NOW,
        )
        cancelled = client.cancel("r1", now=fixtures.NOW + 1.0)
        assert cancelled is not None
        self.assertEqual(cancelled.outcome, remote_reads.OUTCOME_CANCELLED)
        self.assertFalse(client.outstanding("r1"))
        # The Mac finishes the read it started; the frame is dropped here.
        self.assertIsNone(
            client.accept(
                fixtures.detail_frame("r1"),
                now=fixtures.NOW + 2.0,
            )
        )
        self.assertEqual(len(client.bodies), 0)

    def test_cancelling_an_unknown_read_is_not_an_error(self) -> None:
        client = fixtures.make_client()
        self.assertIsNone(client.cancel("nobody", now=fixtures.NOW))


class ReconnectTests(unittest.TestCase):
    """A link that dropped is a promise nobody is going to keep."""

    def test_a_dropped_link_abandons_every_waiter_as_offline(self) -> None:
        client = fixtures.make_client()
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
        abandoned = client.link_down(now=fixtures.NOW + 1.0)
        self.assertEqual(
            {answer.outcome for answer in abandoned},
            {remote_reads.OUTCOME_OFFLINE},
        )
        self.assertEqual(client.pending(), ())
        self.assertFalse(client.linked)

    def test_a_dropped_link_drops_every_held_body(self) -> None:
        client = fixtures.make_client()
        client.read(
            request_id="r1",
            detail="logs",
            plan_key=fixtures.PLAN_KEY,
            now=fixtures.NOW,
        )
        answer = client.accept(
            fixtures.detail_frame("r1"),
            now=fixtures.NOW + 1.0,
        )
        assert answer is not None
        self.assertEqual(len(client.bodies), 1)
        client.link_down(now=fixtures.NOW + 2.0)
        self.assertEqual(len(client.bodies), 0)
        self.assertEqual(client.bodies.held(now=fixtures.NOW + 2.0), ())

    def test_a_reconnect_starts_a_generation_and_abandons_the_old_one(self) -> None:
        client = fixtures.make_client()
        first = client.generation
        client.read(
            request_id="r1",
            detail="logs",
            plan_key=fixtures.PLAN_KEY,
            now=fixtures.NOW,
        )
        client.link_up((CAPABILITY_DETAIL,), now=fixtures.NOW + 5.0)
        self.assertEqual(client.generation, first + 1)
        self.assertEqual(client.pending(), ())
        # And a frame minted before the reconnect correlates to nothing.
        self.assertIsNone(
            client.accept(
                fixtures.detail_frame("r1"),
                now=fixtures.NOW + 6.0,
            )
        )

    def test_a_read_after_a_reconnect_is_asked_again(self) -> None:
        client = fixtures.make_client()
        client.link_down(now=fixtures.NOW + 1.0)
        offline = client.read(
            request_id="r1",
            detail="logs",
            plan_key=fixtures.PLAN_KEY,
            now=fixtures.NOW + 2.0,
        )
        self.assertFalse(offline.outstanding)
        client.link_up((CAPABILITY_DETAIL,), now=fixtures.NOW + 3.0)
        again = client.read(
            request_id="r2",
            detail="logs",
            plan_key=fixtures.PLAN_KEY,
            now=fixtures.NOW + 4.0,
        )
        self.assertTrue(again.outstanding)


class BodyLifetimeTests(unittest.TestCase):
    """Material somebody is reading right now, and nothing older than that."""

    def key(self, offset: int = 0, plan_key: str = fixtures.PLAN_KEY) -> Any:
        return (plan_key, "", "logs", "", offset)

    def answer(self, request_id: str = "r1", **overrides: Any) -> Any:
        fields: dict[str, Any] = {
            "request_id": request_id,
            "outcome": remote_reads.OUTCOME_ANSWERED,
            "detail": "logs",
            "plan_key": fixtures.PLAN_KEY,
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
            plan_key=fixtures.PLAN_KEY,
            reason=details.REASON_NOT_FOUND,
        )
        with self.assertRaises(remote_reads.RemoteReadError):
            cache.store(self.key(), refused, now=fixtures.NOW)

    def test_the_cache_is_bounded_by_count_and_drops_the_oldest(self) -> None:
        cache = remote_reads.BodyCache(capacity=3)
        for index in range(5):
            cache.store(
                self.key(index),
                self.answer(f"r{index}"),
                now=fixtures.NOW + index,
            )
        self.assertEqual(len(cache), 3)
        self.assertIsNone(cache.get(self.key(0), now=fixtures.NOW + 5))
        self.assertIsNotNone(cache.get(self.key(4), now=fixtures.NOW + 5))

    def test_a_body_is_dropped_once_it_is_older_than_the_ttl(self) -> None:
        cache = remote_reads.BodyCache(ttl_seconds=30.0)
        cache.store(self.key(), self.answer(), now=fixtures.NOW)
        self.assertIsNotNone(cache.get(self.key(), now=fixtures.NOW + 29.0))
        self.assertIsNone(cache.get(self.key(), now=fixtures.NOW + 31.0))
        self.assertEqual(len(cache), 0)

    def test_a_forgotten_body_is_blanked_rather_than_merely_unreferenced(self) -> None:
        cache = remote_reads.BodyCache(capacity=1)
        cache.store(self.key(0), self.answer("r0"), now=fixtures.NOW)
        held = cache.get(self.key(0), now=fixtures.NOW)
        assert held is not None
        self.assertEqual(held.content, "a plan's text")
        cache.clear()
        self.assertIsNone(cache.get(self.key(0), now=fixtures.NOW))
        self.assertEqual(cache.held(now=fixtures.NOW), ())

    def test_holding_a_diagnostic_does_not_mean_holding_the_text(self) -> None:
        cache = remote_reads.BodyCache()
        cache.store(self.key(), self.answer(), now=fixtures.NOW)
        (identity,) = cache.held(now=fixtures.NOW)
        self.assertNotIn("content", identity)
        self.assertNotIn("entries", identity)
        self.assertEqual(identity["body_bytes"], len("a plan's text"))

    def test_a_plan_can_be_forgotten_without_forgetting_the_others(self) -> None:
        cache = remote_reads.BodyCache()
        cache.store(
            self.key(0, fixtures.PLAN_KEY),
            self.answer("r0"),
            now=fixtures.NOW,
        )
        cache.store(
            self.key(0, fixtures.OTHER_KEY),
            self.answer("r1", plan_key=fixtures.OTHER_KEY),
            now=fixtures.NOW,
        )
        self.assertEqual(cache.drop(fixtures.PLAN_KEY), 1)
        self.assertIsNone(
            cache.get(
                self.key(0, fixtures.PLAN_KEY),
                now=fixtures.NOW,
            )
        )
        self.assertIsNotNone(
            cache.get(
                self.key(0, fixtures.OTHER_KEY),
                now=fixtures.NOW,
            )
        )

    def test_a_refetched_window_counts_as_a_new_arrival(self) -> None:
        cache = remote_reads.BodyCache(capacity=2)
        cache.store(self.key(0), self.answer("r0"), now=fixtures.NOW)
        cache.store(self.key(1), self.answer("r1"), now=fixtures.NOW + 1)
        cache.store(self.key(0), self.answer("r2"), now=fixtures.NOW + 2)
        cache.store(self.key(2), self.answer("r3"), now=fixtures.NOW + 3)
        # The refetched window survives; the one nobody came back to does not.
        self.assertIsNotNone(cache.get(self.key(0), now=fixtures.NOW + 4))
        self.assertIsNone(cache.get(self.key(1), now=fixtures.NOW + 4))

    def test_an_answered_read_is_held_and_a_refused_one_is_not(self) -> None:
        client = fixtures.make_client()
        client.read(
            request_id="r1",
            detail="logs",
            plan_key=fixtures.PLAN_KEY,
            now=fixtures.NOW,
        )
        client.accept(
            fixtures.detail_frame("r1"),
            now=fixtures.NOW + 1.0,
        )
        self.assertEqual(len(client.bodies), 1)
        client.read(
            request_id="r2",
            detail="state",
            plan_key=fixtures.PLAN_KEY,
            now=fixtures.NOW + 2.0,
        )
        client.accept(
            fixtures.receipt_frame("r2", RECEIPT_REFUSED, ERROR_UNSUPPORTED_CAPABILITY),
            now=fixtures.NOW + 3.0,
        )
        self.assertEqual(len(client.bodies), 1)

    def test_a_held_window_is_found_again_by_the_read_that_asked_for_it(self) -> None:
        client = fixtures.make_client()
        ticket = client.read(
            request_id="r1",
            detail="logs",
            plan_key=fixtures.PLAN_KEY,
            now=fixtures.NOW,
        )
        client.accept(
            fixtures.detail_frame("r1"),
            now=fixtures.NOW + 1.0,
        )
        held = client.cached(ticket.request, now=fixtures.NOW + 2.0)
        assert held is not None
        self.assertEqual(held.content, "the log text")

    def test_expiring_the_reads_also_ages_the_bodies(self) -> None:
        client = fixtures.make_client(ttl_seconds=10.0)
        client.read(
            request_id="r1",
            detail="logs",
            plan_key=fixtures.PLAN_KEY,
            now=fixtures.NOW,
        )
        client.accept(
            fixtures.detail_frame("r1"),
            now=fixtures.NOW,
        )
        client.expire(fixtures.NOW + 11.0)
        self.assertEqual(len(client.bodies), 0)


if __name__ == "__main__":
    unittest.main()
